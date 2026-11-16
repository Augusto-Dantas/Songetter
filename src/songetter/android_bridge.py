"""Pure-Python Hybrid Android bridge for Songetter (Chaquopy + Remote Sync).

Provides:
- Pure-Python Spotify metadata resolution via embed HTML / oEmbed JSON (no Node.js).
- LRCLIB lyrics lookup and metadata-only tag embedding (ID3 USLT, Vorbis, MP4).
- On-device download pipeline wrapping ``songetter.downloader.Downloader``.
- Remote Songetter FastAPI server sync with local pre-check, ``.part`` streaming,
  atomic ``os.replace`` promotion, deduplication, and flat library organization.
- Crash-safe JSON facade entry points invoked from Kotlin via Chaquopy.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote, unquote, urlparse

import httpx
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, USLT
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from songetter.config import (
    AUDIO_SUFFIXES,
    DEFAULT_FALLBACK_ARTIST,
    DEFAULT_FALLBACK_TITLE,
    SUPPORTED_SEARCH_SOURCES,
)
from songetter.core import parse_links
from songetter.csv_processor import (
    EmptyCSVError,
    NoValidUrlsError,
    TrackRecord,
    _is_valid_url,
    parse_csv,
)
from songetter.deduplicator import (
    DuplicateResolutionResult,
    extract_metadata,
    normalize_text,
    resolve_duplicates,
)
from songetter.downloader import (
    Downloader,
    DownloadSummary,
    TrackDownloadResult,
    _is_mornye_url,
    deduplicate_records,
)
from songetter.library import (
    LibraryIndex,
    extract_url_stem,
    validate_library_directory,
)
from songetter.organizer import flatten_and_organize_library
from songetter.search import list_search_sources, search_tracks_with_errors

logger = logging.getLogger(__name__)

_ANDROID_SHARED_MUSIC_DIR = Path("/storage/emulated/0/Music")
_USER_AGENT = "Songetter/0.1.0 (https://github.com/Augusto-Dantas/Songetter)"
_SPOTIFY_URL_PATH_RE = re.compile(
    r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist|artist)/([A-Za-z0-9]+)/?$",
    re.IGNORECASE,
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_INITIAL_STATE_RE = re.compile(
    r'<script[^>]+id=["\']initial-state["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

__all__ = [
    "bridge_get_status",
    "bridge_list_library",
    "bridge_organize_library",
    "bridge_search",
    "bridge_start_job",
    "embed_basic_tags",
    "embed_lyrics_in_audio",
    "fetch_lyrics_lrclib",
    "get_default_android_library_dir",
    "resolve_spotify_url_pure_python",
    "run_on_device_job",
    "run_remote_server_job",
]


def _is_writable_dir(path: Path) -> bool:
    """Checks whether *path* is an existing writable directory using a write probe."""
    try:
        if not path.exists() or not path.is_dir():
            return False
        if not os.access(path, os.W_OK):
            return False
        probe = path / f".songetter_android_probe_{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"ok\n")
            return True
        except OSError:
            return False
        finally:
            try:
                if probe.exists():
                    probe.unlink()
            except OSError:
                pass
    except OSError:
        return False


def get_default_android_library_dir() -> Path:
    """Resolves the default music library directory for Android/desktop environments."""
    env_lib = (os.environ.get("SONGETTER_LIBRARY_DIR") or "").strip()
    if env_lib:
        return Path(env_lib).expanduser()

    if _is_writable_dir(_ANDROID_SHARED_MUSIC_DIR):
        return _ANDROID_SHARED_MUSIC_DIR / "Songetter"

    android_storage = (os.environ.get("ANDROID_STORAGE") or "").strip()
    if android_storage:
        base = Path(android_storage).expanduser()
        emulated_music = base / "emulated" / "0" / "Music"
        if _is_writable_dir(emulated_music):
            return emulated_music / "Songetter"
        direct_music = base / "Music"
        if _is_writable_dir(direct_music):
            return direct_music / "Songetter"
        if _is_writable_dir(base):
            return base / "Music" / "Songetter"

    external_storage = (os.environ.get("EXTERNAL_STORAGE") or "").strip()
    if external_storage:
        base = Path(external_storage).expanduser()
        direct_music = base / "Music"
        if _is_writable_dir(direct_music):
            return direct_music / "Songetter"
        if _is_writable_dir(base):
            return base / "Music" / "Songetter"

    return Path.home() / "Music" / "Songetter"


@contextmanager
def _use_http_client(
    client: Optional[httpx.Client],
    *,
    timeout: float = 15.0,
) -> Iterator[httpx.Client]:
    """Yields an injected ``httpx.Client`` without closing it, or creates a scoped one."""
    if client is not None:
        yield client
    else:
        with httpx.Client(timeout=timeout, follow_redirects=True) as created:
            yield created


def _is_spotify_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() == "spotify":
        return True
    return parsed.scheme.lower() in {"http", "https"} and host in {
        "open.spotify.com",
        "spotify.link",
    }


def _extract_spotify_kind_and_id(canonical_url: str) -> Tuple[str, str]:
    parsed = urlparse(canonical_url)
    match = _SPOTIFY_URL_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError(f"Unsupported Spotify URL path: {canonical_url}")
    return match.group(1).lower(), match.group(2)


def _spotify_uri_to_url(uri: Optional[str], fallback_url: str) -> str:
    if uri and isinstance(uri, str):
        uri_clean = uri.strip()
        if uri_clean.startswith("spotify:"):
            parts = uri_clean.split(":")
            if len(parts) >= 3 and parts[1] in {"track", "album", "playlist", "artist"}:
                return f"https://open.spotify.com/{parts[1]}/{parts[2]}"
        if uri_clean.startswith(("http://", "https://")):
            return uri_clean
    return fallback_url


def _extract_artists_str(item: Dict[str, Any]) -> str:
    artists = item.get("artists")
    if isinstance(artists, list):
        names = []
        for entry in artists:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]).strip())
            elif isinstance(entry, str) and entry.strip():
                names.append(entry.strip())
        if names:
            return ", ".join(names)

    for key in ("subtitle", "artist", "artistName"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            # Spotify embed subtitles occasionally use non-breaking spaces around commas
            return raw.replace("\xa0", " ").strip()
    return ""


def _extract_isrc(item: Dict[str, Any]) -> Optional[str]:
    isrc = item.get("isrc")
    if isinstance(isrc, str) and isrc.strip():
        return isrc.strip()
    ext_ids = item.get("external_ids")
    if isinstance(ext_ids, dict):
        ext_isrc = ext_ids.get("isrc")
        if isinstance(ext_isrc, str) and ext_isrc.strip():
            return ext_isrc.strip()
    return None


def _extract_duration_ms(item: Dict[str, Any]) -> Optional[int]:
    for key in ("duration_ms", "durationMs", "duration"):
        val = item.get(key)
        if val is None or val == "":
            continue
        try:
            num = int(float(val))
            if num > 0:
                return num
        except (TypeError, ValueError):
            continue
    return None


def _build_spotify_track_record(
    *,
    track_url: str,
    artist: str,
    title: str,
    duration_ms: Optional[int],
    isrc: Optional[str],
    spotify_kind: str,
    spotify_id: str,
    source: str,
) -> TrackRecord:
    raw_row: Dict[str, str] = {
        "url": track_url,
        "spotify_kind": spotify_kind,
        "spotify_id": spotify_id,
        "source": source,
    }
    clean_artist = artist.strip()
    clean_title = title.strip()
    if clean_artist:
        raw_row["artist"] = clean_artist
    if clean_title:
        raw_row["title"] = clean_title
    if duration_ms is not None and duration_ms > 0:
        raw_row["duration_ms"] = str(duration_ms)
        raw_row["duration_seconds"] = str(round(duration_ms / 1000.0, 3))
    if isrc:
        raw_row["isrc"] = isrc
    return TrackRecord(
        url=track_url,
        artist=clean_artist or None,
        title=clean_title or None,
        raw_row=raw_row,
    )


def _find_entity_dict(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    # Standard Spotify __NEXT_DATA__ path: props.pageProps.state.data.entity
    props = payload.get("props")
    if isinstance(props, dict):
        page_props = props.get("pageProps")
        if isinstance(page_props, dict):
            state = page_props.get("state")
            if isinstance(state, dict):
                data = state.get("data")
                if isinstance(data, dict):
                    entity = data.get("entity")
                    if isinstance(entity, dict):
                        return entity
                    return data
    if isinstance(payload.get("entity"), dict):
        return payload["entity"]
    if any(k in payload for k in ("trackList", "tracks", "topTracks", "artists", "title", "name")):
        return payload
    return None


def _extract_records_from_entity(
    entity: Dict[str, Any],
    canonical_url: str,
    kind: str,
    spotify_id: str,
    source: str,
) -> List[TrackRecord]:
    records: List[TrackRecord] = []

    raw_items: List[Dict[str, Any]] = []
    if isinstance(entity.get("trackList"), list):
        raw_items = [x for x in entity["trackList"] if isinstance(x, dict)]
    elif isinstance(entity.get("tracks"), dict) and isinstance(entity["tracks"].get("items"), list):
        for entry in entity["tracks"]["items"]:
            if isinstance(entry, dict):
                inner = entry.get("track") if isinstance(entry.get("track"), dict) else entry
                raw_items.append(inner)
    elif isinstance(entity.get("topTracks"), list):
        raw_items = [x for x in entity["topTracks"] if isinstance(x, dict)]

    if raw_items:
        for idx, item in enumerate(raw_items):
            title = str(item.get("title") or item.get("name") or "").strip()
            artist = _extract_artists_str(item)
            duration_ms = _extract_duration_ms(item)
            isrc = _extract_isrc(item)
            item_id = str(item.get("id") or "").strip()
            fallback_item_url = (
                f"https://open.spotify.com/track/{item_id}"
                if item_id
                else f"{canonical_url}#track={idx + 1}"
            )
            track_url = _spotify_uri_to_url(item.get("uri"), fallback_item_url)
            if title or artist:
                records.append(
                    _build_spotify_track_record(
                        track_url=track_url,
                        artist=artist,
                        title=title,
                        duration_ms=duration_ms,
                        isrc=isrc,
                        spotify_kind=kind,
                        spotify_id=item_id or spotify_id,
                        source=source,
                    )
                )
        return records

    # Single-track entity (or fallback entity without trackList)
    title = str(entity.get("title") or entity.get("name") or "").strip()
    artist = _extract_artists_str(entity)
    duration_ms = _extract_duration_ms(entity)
    isrc = _extract_isrc(entity)
    track_url = _spotify_uri_to_url(entity.get("uri"), canonical_url)
    if title or artist:
        records.append(
            _build_spotify_track_record(
                track_url=track_url,
                artist=artist,
                title=title,
                duration_ms=duration_ms,
                isrc=isrc,
                spotify_kind=kind,
                spotify_id=spotify_id,
                source=source,
            )
        )
    return records


def _extract_meta_content(html_text: str, key: str) -> str:
    pattern = re.compile(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)["\']',
        re.IGNORECASE,
    )
    match = pattern.search(html_text)
    if match:
        return html.unescape(match.group(1)).strip()
    alt_pattern = re.compile(
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(key)}["\']',
        re.IGNORECASE,
    )
    match = alt_pattern.search(html_text)
    if match:
        return html.unescape(match.group(1)).strip()
    return ""


def _split_spotify_page_title(raw_title: str) -> Tuple[str, str]:
    """Splits common Spotify page/oEmbed title formats into (artist, title)."""
    cleaned = html.unescape(raw_title or "").strip()
    cleaned = re.sub(r"\s*\|\s*Spotify\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    # Pattern: "<Title> - song and lyrics by <Artist>" or "<Title> - song by <Artist>"
    song_by = re.match(
        r"^(?P<title>.+?)\s*-\s*(?:song(?:\s+and\s+lyrics)?|single|album)\s+by\s+(?P<artist>.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if song_by:
        return song_by.group("artist").strip(), song_by.group("title").strip()

    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        return right.strip(), left.strip()
    return "", cleaned


def _parse_spotify_embed_html(
    html_text: str,
    canonical_url: str,
    kind: str,
    spotify_id: str,
) -> List[TrackRecord]:
    # 1. __NEXT_DATA__ JSON
    next_match = _NEXT_DATA_RE.search(html_text)
    if next_match:
        try:
            payload = json.loads(next_match.group(1).strip())
            entity = _find_entity_dict(payload)
            if entity is not None:
                recs = _extract_records_from_entity(
                    entity, canonical_url, kind, spotify_id, "spotify_embed"
                )
                if recs:
                    return recs
        except json.JSONDecodeError:
            pass

    # 2. initial-state JSON or base64 JSON
    init_match = _INITIAL_STATE_RE.search(html_text)
    if init_match:
        raw_init = init_match.group(1).strip()
        for candidate in (raw_init,):
            try:
                decoded_text = candidate
                if not candidate.startswith("{"):
                    decoded_text = base64.b64decode(candidate).decode("utf-8", errors="ignore")
                payload = json.loads(decoded_text)
                entity = _find_entity_dict(payload)
                if entity is not None:
                    recs = _extract_records_from_entity(
                        entity, canonical_url, kind, spotify_id, "spotify_embed"
                    )
                    if recs:
                        return recs
            except Exception:
                pass

    # 3. JSON-LD structured data
    for ld_match in _LD_JSON_RE.finditer(html_text):
        try:
            ld_data = json.loads(ld_match.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(ld_data, dict):
            name = str(ld_data.get("name") or "").strip()
            by_artist = ld_data.get("byArtist")
            artist = ""
            if isinstance(by_artist, dict):
                artist = str(by_artist.get("name") or "").strip()
            elif isinstance(by_artist, list):
                artist = ", ".join(
                    str(a.get("name")).strip()
                    for a in by_artist
                    if isinstance(a, dict) and a.get("name")
                )
            isrc = str(ld_data.get("isrcCode") or "").strip() or None
            if name or artist:
                return [
                    _build_spotify_track_record(
                        track_url=canonical_url,
                        artist=artist,
                        title=name,
                        duration_ms=None,
                        isrc=isrc,
                        spotify_kind=kind,
                        spotify_id=spotify_id,
                        source="spotify_embed",
                    )
                ]

    # 4. OpenGraph / <title> fallback
    og_title = _extract_meta_content(html_text, "og:title")
    og_desc = _extract_meta_content(html_text, "og:description")
    musician = _extract_meta_content(html_text, "music:musician_description")
    if not og_title:
        title_match = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        if title_match:
            og_title = html.unescape(title_match.group(1)).strip()

    if og_title:
        artist_from_title, parsed_title = _split_spotify_page_title(og_title)
        artist = musician or artist_from_title
        if not artist and og_desc and " · " in og_desc:
            # e.g. "Listen to Title on Spotify. Artist · Song · 2020."
            after_dot = og_desc.split(".", 1)[-1]
            candidate_artist = after_dot.split("·")[0].strip()
            if candidate_artist:
                artist = candidate_artist
        if parsed_title or artist:
            return [
                _build_spotify_track_record(
                    track_url=canonical_url,
                    artist=artist,
                    title=parsed_title,
                    duration_ms=None,
                    isrc=None,
                    spotify_kind=kind,
                    spotify_id=spotify_id,
                    source="spotify_embed",
                )
            ]

    return []


def _parse_spotify_oembed(
    data: Dict[str, Any],
    canonical_url: str,
    kind: str,
    spotify_id: str,
) -> List[TrackRecord]:
    raw_title = str(data.get("title") or "").strip()
    author_name = str(data.get("author_name") or "").strip()
    if not raw_title and not author_name:
        return []

    parsed_artist, parsed_title = _split_spotify_page_title(raw_title)
    artist = author_name or parsed_artist
    title = parsed_title or raw_title
    if not title and not artist:
        return []

    return [
        _build_spotify_track_record(
            track_url=canonical_url,
            artist=artist,
            title=title,
            duration_ms=None,
            isrc=None,
            spotify_kind=kind,
            spotify_id=spotify_id,
            source="spotify_oembed",
        )
    ]


def resolve_spotify_url_pure_python(
    url: str,
    client: Optional[httpx.Client] = None,
) -> List[TrackRecord]:
    """Resolves Spotify track/album/playlist/artist URLs using pure-Python HTTP scraping.

    Validates links via ``songetter.core.parse_links``, fetches Spotify embed HTML
    and/or oEmbed JSON via ``httpx``, and parses track metadata without Node.js.
    """
    canonical_urls = parse_links(url)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }

    all_records: List[TrackRecord] = []
    last_error: Optional[str] = None

    with _use_http_client(client) as http:
        for raw_canonical in canonical_urls:
            canonical = raw_canonical
            parsed = urlparse(canonical)
            if (parsed.hostname or "").lower() == "spotify.link":
                try:
                    redir_resp = http.get(canonical, headers=headers)
                    final_url = str(redir_resp.url)
                    if "open.spotify.com" in final_url:
                        canonical = parse_links(final_url)[0]
                except Exception as exc:
                    last_error = f"Failed to resolve short Spotify link {canonical}: {exc}"
                    continue

            kind, spotify_id = _extract_spotify_kind_and_id(canonical)
            embed_url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
            records: List[TrackRecord] = []

            try:
                embed_resp = http.get(embed_url, headers=headers)
                if embed_resp.status_code == 200 and embed_resp.text:
                    records = _parse_spotify_embed_html(
                        embed_resp.text, canonical, kind, spotify_id
                    )
                else:
                    last_error = f"Spotify embed HTTP {embed_resp.status_code} for {canonical}"
            except Exception as exc:
                last_error = f"Spotify embed request failed for {canonical}: {exc}"

            needs_oembed = not records or (
                len(records) == 1 and (not records[0].artist or not records[0].title)
            )
            if needs_oembed:
                oembed_url = (
                    f"https://open.spotify.com/oembed?url={quote(canonical, safe='')}"
                )
                try:
                    oembed_resp = http.get(oembed_url, headers=headers)
                    if oembed_resp.status_code == 200:
                        oembed_data = oembed_resp.json()
                        if isinstance(oembed_data, dict):
                            oembed_records = _parse_spotify_oembed(
                                oembed_data, canonical, kind, spotify_id
                            )
                            if oembed_records:
                                if records and len(records) == 1:
                                    merged_artist = records[0].artist or oembed_records[0].artist
                                    merged_title = records[0].title or oembed_records[0].title
                                    raw_row = dict(records[0].raw_row)
                                    if merged_artist:
                                        raw_row["artist"] = merged_artist
                                    if merged_title:
                                        raw_row["title"] = merged_title
                                    records = [
                                        TrackRecord(
                                            url=records[0].url,
                                            artist=merged_artist,
                                            title=merged_title,
                                            raw_row=raw_row,
                                        )
                                    ]
                                else:
                                    records = oembed_records
                    elif not records:
                        last_error = f"Spotify oEmbed HTTP {oembed_resp.status_code} for {canonical}"
                except Exception as exc:
                    if not records:
                        last_error = f"Spotify oEmbed request failed for {canonical}: {exc}"

            all_records.extend(records)

    if not all_records:
        raise RuntimeError(last_error or f"No Spotify metadata found for {url}")

    return all_records


def _pick_lrclib_lyrics(entry: Dict[str, Any]) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in ("syncedLyrics", "plainLyrics"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def fetch_lyrics_lrclib(
    artist: str,
    title: str,
    duration_seconds: Optional[float] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Queries LRCLIB (/api/get with /api/search fallback) for synced or plain lyrics."""
    clean_artist = (artist or "").strip()
    clean_title = (title or "").strip()
    if not clean_artist or not clean_title:
        return None

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    params: Dict[str, Any] = {
        "artist_name": clean_artist,
        "track_name": clean_title,
    }
    if duration_seconds is not None and duration_seconds > 0:
        params["duration"] = int(round(duration_seconds))

    try:
        with _use_http_client(client, timeout=10.0) as http:
            get_resp = http.get("https://lrclib.net/api/get", params=params, headers=headers)
            if get_resp.status_code == 200:
                payload = get_resp.json()
                lyrics = _pick_lrclib_lyrics(payload)
                if lyrics:
                    return lyrics

            search_params = {
                "artist_name": clean_artist,
                "track_name": clean_title,
            }
            search_resp = http.get(
                "https://lrclib.net/api/search", params=search_params, headers=headers
            )
            if search_resp.status_code == 200:
                items = search_resp.json()
                if isinstance(items, list):
                    candidates = [
                        item
                        for item in items
                        if isinstance(item, dict) and _pick_lrclib_lyrics(item)
                    ]
                    if candidates:
                        if duration_seconds is not None and duration_seconds > 0:
                            candidates.sort(
                                key=lambda item: abs(
                                    float(item.get("duration") or 0) - float(duration_seconds)
                                )
                            )
                        return _pick_lrclib_lyrics(candidates[0])
    except Exception as exc:
        logger.debug("LRCLIB lookup failed for %s - %s: %s", clean_artist, clean_title, exc)
        return None

    return None


def embed_lyrics_in_audio(audio_path: Union[str, Path], lyrics_text: str) -> bool:
    """Embeds lyrics strictly inside audio file metadata tags (never creates .lrc files)."""
    if not lyrics_text or not isinstance(lyrics_text, str) or not lyrics_text.strip():
        return False

    path = Path(audio_path)
    if not path.is_file():
        return False

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            try:
                tags = ID3(str(path))
            except ID3NoHeaderError:
                tags = ID3()
            try:
                tags.delall("USLT")
            except (ValueError, KeyError, TypeError):
                pass
            tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics_text))
            tags.save(str(path))
            return True

        if suffix == ".wav":
            wav = WAVE(str(path))
            if wav.tags is None:
                wav.add_tags()
            assert wav.tags is not None
            try:
                wav.tags.delall("USLT")
            except (ValueError, KeyError, TypeError):
                pass
            wav.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics_text))
            wav.save()
            return True

        if suffix == ".flac":
            flac = FLAC(str(path))
            if flac.tags is None:
                flac.add_tags()
            try:
                flac["lyrics"] = [lyrics_text]
            except (ValueError, KeyError, TypeError):
                return False
            flac.save()
            return True

        if suffix in {".ogg", ".opus"}:
            audio: Any = None
            try:
                audio = OggOpus(str(path)) if suffix == ".opus" else OggVorbis(str(path))
            except Exception:
                audio = mutagen.File(str(path))
            if audio is None:
                return False
            if getattr(audio, "tags", None) is None and hasattr(audio, "add_tags"):
                audio.add_tags()
            try:
                audio["lyrics"] = [lyrics_text]
            except (ValueError, KeyError, TypeError):
                return False
            audio.save()
            return True

        if suffix in {".m4a", ".mp4"}:
            mp4 = MP4(str(path))
            if mp4.tags is None:
                mp4.add_tags()
            assert mp4.tags is not None
            try:
                mp4.tags["\xa9lyr"] = [lyrics_text]
            except (ValueError, KeyError, TypeError):
                return False
            mp4.save()
            return True

        generic = mutagen.File(str(path))
        if generic is None:
            return False
        if getattr(generic, "tags", None) is None and hasattr(generic, "add_tags"):
            generic.add_tags()
        if generic.tags is not None:
            try:
                generic.tags["lyrics"] = [lyrics_text]
                generic.save()
                return True
            except (ValueError, KeyError, TypeError):
                return False
        return False
    except Exception as exc:
        logger.debug("Failed to embed lyrics into %s: %s", path, exc)
        return False


def embed_basic_tags(
    audio_path: Union[str, Path],
    artist: Optional[str],
    title: Optional[str],
) -> bool:
    """Embeds artist and title metadata inside audio tags safely."""
    clean_artist = artist.strip() if isinstance(artist, str) and artist.strip() else ""
    clean_title = title.strip() if isinstance(title, str) and title.strip() else ""
    if not clean_artist and not clean_title:
        return False

    path = Path(audio_path)
    if not path.is_file():
        return False

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            try:
                tags = ID3(str(path))
            except ID3NoHeaderError:
                tags = ID3()
            if clean_artist:
                try:
                    tags.delall("TPE1")
                except (ValueError, KeyError, TypeError):
                    pass
                tags.add(TPE1(encoding=3, text=[clean_artist]))
            if clean_title:
                try:
                    tags.delall("TIT2")
                except (ValueError, KeyError, TypeError):
                    pass
                tags.add(TIT2(encoding=3, text=[clean_title]))
            tags.save(str(path))
            return True

        if suffix == ".wav":
            wav = WAVE(str(path))
            if wav.tags is None:
                wav.add_tags()
            assert wav.tags is not None
            if clean_artist:
                try:
                    wav.tags.delall("TPE1")
                except (ValueError, KeyError, TypeError):
                    pass
                wav.tags.add(TPE1(encoding=3, text=[clean_artist]))
            if clean_title:
                try:
                    wav.tags.delall("TIT2")
                except (ValueError, KeyError, TypeError):
                    pass
                wav.tags.add(TIT2(encoding=3, text=[clean_title]))
            wav.save()
            return True

        if suffix == ".flac":
            flac = FLAC(str(path))
            if flac.tags is None:
                flac.add_tags()
            try:
                if clean_artist:
                    flac["artist"] = [clean_artist]
                if clean_title:
                    flac["title"] = [clean_title]
            except (ValueError, KeyError, TypeError):
                return False
            flac.save()
            return True

        if suffix in {".ogg", ".opus"}:
            audio: Any = None
            try:
                audio = OggOpus(str(path)) if suffix == ".opus" else OggVorbis(str(path))
            except Exception:
                audio = mutagen.File(str(path))
            if audio is None:
                return False
            if getattr(audio, "tags", None) is None and hasattr(audio, "add_tags"):
                audio.add_tags()
            try:
                if clean_artist:
                    audio["artist"] = [clean_artist]
                if clean_title:
                    audio["title"] = [clean_title]
            except (ValueError, KeyError, TypeError):
                return False
            audio.save()
            return True

        if suffix in {".m4a", ".mp4"}:
            mp4 = MP4(str(path))
            if mp4.tags is None:
                mp4.add_tags()
            assert mp4.tags is not None
            try:
                if clean_artist:
                    mp4.tags["\xa9ART"] = [clean_artist]
                if clean_title:
                    mp4.tags["\xa9nam"] = [clean_title]
            except (ValueError, KeyError, TypeError):
                return False
            mp4.save()
            return True

        generic = mutagen.File(str(path))
        if generic is None:
            return False
        if getattr(generic, "tags", None) is None and hasattr(generic, "add_tags"):
            generic.add_tags()
        if generic.tags is not None:
            try:
                if clean_artist:
                    generic.tags["artist"] = [clean_artist]
                if clean_title:
                    generic.tags["title"] = [clean_title]
                generic.save()
                return True
            except (ValueError, KeyError, TypeError):
                return False
        return False
    except Exception as exc:
        logger.debug("Failed to embed basic tags into %s: %s", path, exc)
        return False


def _has_real_tags(path: Path) -> Tuple[bool, bool]:
    """Checks safely whether *path* already carries non-empty artist/title tags."""
    try:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:
            audio = mutagen.File(str(path))
    except Exception:
        return False, False
    if audio is None or getattr(audio, "tags", None) is None:
        return False, False

    has_artist = False
    has_title = False
    tags = audio.tags
    for key in ("artist", "ARTIST", "TPE1", "\xa9ART"):
        try:
            val = tags.get(key)
        except (ValueError, KeyError, TypeError):
            continue
        if val:
            has_artist = True
            break
    for key in ("title", "TITLE", "TIT2", "\xa9nam"):
        try:
            val = tags.get(key)
        except (ValueError, KeyError, TypeError):
            continue
        if val:
            has_title = True
            break
    return has_artist, has_title


def _is_search_uri(candidate: str) -> bool:
    return bool(re.match(r"^(?:ytsearch\d*|scsearch\d*):.+$", candidate.strip(), re.IGNORECASE))


def _parse_input_payload(
    input_payload: Union[str, bytes],
    is_csv: bool,
) -> List[TrackRecord]:
    """Parses CSV bytes/text or URL lists into deduplicated TrackRecord objects."""
    if is_csv:
        return deduplicate_records(parse_csv(input_payload))

    if isinstance(input_payload, bytes):
        if b"\x00" in input_payload:
            raise ValueError("Input contains invalid binary data.")
        text = input_payload.decode("utf-8-sig")
    elif isinstance(input_payload, str):
        if "\x00" in input_payload:
            raise ValueError("Input contains invalid binary data.")
        text = input_payload.lstrip("\ufeff")
    else:
        raise ValueError(f"Unsupported input payload type: {type(input_payload)}")

    if not text.strip():
        raise EmptyCSVError("Input payload is empty.")

    # Check if plain tokens are all valid URLs or search URIs
    non_comment_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not non_comment_lines:
        raise NoValidUrlsError("Input contains only comment lines.")

    tokens = [tok for line in non_comment_lines for tok in re.split(r"[\s,;]+", line) if tok]
    if tokens and all(_is_valid_url(tok) or _is_search_uri(tok) for tok in tokens):
        return deduplicate_records([TrackRecord(url=tok) for tok in tokens])

    # Fallback: try RFC 4180 CSV parsing if multi-column/headered CSV was pasted without is_csv=True
    if any(delim in text for delim in (",", ";", "\t", "|")):
        try:
            return deduplicate_records(parse_csv(text))
        except Exception:
            pass

    url_records: List[TrackRecord] = []
    for line in non_comment_lines:
        if _is_search_uri(line):
            url_records.append(TrackRecord(url=line))
            continue
        for tok in re.split(r"[\s,;]+", line):
            if _is_valid_url(tok) or _is_search_uri(tok):
                url_records.append(TrackRecord(url=tok))

    if not url_records:
        raise NoValidUrlsError("No valid URLs found in input.")
    return deduplicate_records(url_records)


def _resolve_audio_url_with_ytdlp(
    query_or_url: str,
    *,
    artist: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    """Resolves a direct audio stream URL via yt-dlp Python module if installed."""
    try:
        import yt_dlp  # type: ignore[import-not-found]
    except ImportError:
        return None

    clean_url = (query_or_url or "").strip()
    if _is_search_uri(clean_url):
        target = clean_url
    elif artist and title and (_is_spotify_url(clean_url) or _is_mornye_url(clean_url)):
        target = f"ytsearch1:{artist.strip()} - {title.strip()}"
    elif clean_url.startswith(("http://", "https://")):
        target = clean_url
    elif artist and title:
        target = f"ytsearch1:{artist.strip()} - {title.strip()}"
    else:
        return None

    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:
        logger.debug("yt-dlp resolution failed for %s: %s", target, exc)
        return None

    if not isinstance(info, dict):
        return None

    if "entries" in info and isinstance(info["entries"], list):
        entries = [entry for entry in info["entries"] if isinstance(entry, dict)]
        if not entries:
            return None
        info = entries[0]

    direct_url = info.get("url")
    if isinstance(direct_url, str) and direct_url.startswith(("http://", "https://")):
        return direct_url

    formats = info.get("formats")
    if isinstance(formats, list):
        audio_candidates = [
            fmt
            for fmt in formats
            if isinstance(fmt, dict)
            and isinstance(fmt.get("url"), str)
            and fmt["url"].startswith(("http://", "https://"))
            and fmt.get("acodec") not in (None, "none")
        ]
        if audio_candidates:
            audio_candidates.sort(
                key=lambda fmt: (
                    1 if fmt.get("vcodec") in (None, "none") else 0,
                    float(fmt.get("abr") or fmt.get("tbr") or 0.0),
                ),
                reverse=True,
            )
            return str(audio_candidates[0]["url"])

    return None


def _serialize_duplicate(dup: DuplicateResolutionResult) -> Dict[str, Any]:
    return {
        "retained": str(dup.retained),
        "deleted": [str(p) for p in dup.deleted],
        "artist": dup.artist,
        "title": dup.title,
        "reason": dup.reason,
    }


def _record_matches_index(record: TrackRecord, index: LibraryIndex) -> bool:
    artist = record.artist.strip() if record.artist else ""
    title = record.title.strip() if record.title else ""
    if (
        artist
        and title
        and artist != DEFAULT_FALLBACK_ARTIST
        and title != DEFAULT_FALLBACK_TITLE
        and index.contains_track(artist, title)
    ):
        return True
    return index.contains_stem(record.url)


def _extract_duration_from_record(record: Optional[TrackRecord]) -> Optional[float]:
    if record is None or not record.raw_row:
        return None
    raw_sec = record.raw_row.get("duration_seconds")
    if raw_sec:
        try:
            val = float(raw_sec)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    raw_ms = record.raw_row.get("duration_ms")
    if raw_ms:
        try:
            val = float(raw_ms) / 1000.0
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return None


def run_on_device_job(
    input_payload: Union[str, bytes],
    library_dir: Union[str, Path, None] = None,
    is_csv: bool = False,
    embed_lyrics: bool = True,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Executes an on-device download, deduplication, tag/lyrics embedding, and flatten job."""
    target_dir = (
        library_dir
        if library_dir is not None and str(library_dir).strip()
        else get_default_android_library_dir()
    )
    lib_dir = validate_library_directory(target_dir, create=True)
    records = _parse_input_payload(input_payload, is_csv=is_csv)

    mornye_available = shutil.which("mornye") is not None
    expanded_records: List[TrackRecord] = []
    pre_failed: List[Dict[str, Any]] = []

    if not mornye_available:
        for rec in records:
            if _is_spotify_url(rec.url):
                try:
                    resolved = resolve_spotify_url_pure_python(rec.url, client=client)
                    for item in resolved:
                        merged_artist = item.artist or rec.artist
                        merged_title = item.title or rec.title
                        raw_row = dict(rec.raw_row)
                        raw_row.update(item.raw_row)
                        expanded_records.append(
                            TrackRecord(
                                url=item.url,
                                artist=merged_artist,
                                title=merged_title,
                                raw_row=raw_row,
                            )
                        )
                except Exception as exc:
                    if rec.artist and rec.title:
                        expanded_records.append(rec)
                    else:
                        pre_failed.append(
                            {
                                "url": rec.url,
                                "artist": rec.artist,
                                "title": rec.title,
                                "status": "failed",
                                "file_path": None,
                                "error": str(exc),
                                "lyrics_embedded": False,
                            }
                        )
            else:
                expanded_records.append(rec)

        pre_index = LibraryIndex.build(lib_dir)
        prepared_records: List[TrackRecord] = []
        for rec in expanded_records:
            if _record_matches_index(rec, pre_index):
                prepared_records.append(rec)
                continue
            if _is_mornye_url(rec.url) or _is_search_uri(rec.url):
                direct_url = _resolve_audio_url_with_ytdlp(
                    rec.url, artist=rec.artist, title=rec.title
                )
                if direct_url:
                    raw_row = dict(rec.raw_row)
                    raw_row.setdefault("source_url", rec.url)
                    prepared_records.append(
                        TrackRecord(
                            url=direct_url,
                            artist=rec.artist,
                            title=rec.title,
                            raw_row=raw_row,
                        )
                    )
                    continue
            prepared_records.append(rec)
        expanded_records = prepared_records
    else:
        expanded_records = records

    record_by_url: Dict[str, TrackRecord] = {r.url: r for r in expanded_records}
    record_by_meta: Dict[Tuple[str, str], TrackRecord] = {
        (normalize_text(r.artist), normalize_text(r.title)): r
        for r in expanded_records
        if r.artist and r.title
    }

    summary = (
        Downloader(lib_dir).download(expanded_records)
        if expanded_records
        else DownloadSummary()
    )

    tracks_out: List[Dict[str, Any]] = []
    lyrics_embedded_count = 0

    for res in summary.tracks:
        lyrics_embedded = False
        if res.status == "downloaded" and res.file_path is not None and res.file_path.exists():
            meta = extract_metadata(res.file_path)
            has_artist, has_title = _has_real_tags(res.file_path)
            target_artist = res.artist or (
                meta.artist if meta.artist != DEFAULT_FALLBACK_ARTIST else None
            )
            target_title = res.title or (
                meta.title if meta.title != DEFAULT_FALLBACK_TITLE else None
            )
            if (not has_artist or not has_title) and (target_artist or target_title):
                embed_basic_tags(
                    res.file_path,
                    target_artist if not has_artist else None,
                    target_title if not has_title else None,
                )

            if embed_lyrics and target_artist and target_title:
                matched_rec = record_by_url.get(res.url) or record_by_meta.get(
                    (normalize_text(target_artist), normalize_text(target_title))
                )
                duration_sec = _extract_duration_from_record(matched_rec)
                if duration_sec is None and meta.duration > 0:
                    duration_sec = meta.duration
                lyrics_text = fetch_lyrics_lrclib(
                    target_artist,
                    target_title,
                    duration_seconds=duration_sec,
                    client=client,
                )
                if lyrics_text:
                    lyrics_embedded = embed_lyrics_in_audio(res.file_path, lyrics_text)
                    if lyrics_embedded:
                        lyrics_embedded_count += 1

        tracks_out.append(
            {
                "url": res.url,
                "artist": res.artist,
                "title": res.title,
                "status": res.status,
                "file_path": str(res.file_path) if res.file_path is not None else None,
                "error": res.error,
                "lyrics_embedded": lyrics_embedded,
            }
        )

    tracks_out.extend(pre_failed)
    failed_total = summary.failed + len(pre_failed)

    return {
        "status": "completed",
        "mode": "on_device",
        "library_dir": str(lib_dir),
        "total_tracks": len(tracks_out),
        "downloaded": summary.downloaded,
        "skipped": summary.skipped,
        "failed": failed_total,
        "lyrics_embedded": lyrics_embedded_count,
        "tracks": tracks_out,
        "duplicates_resolved": [_serialize_duplicate(d) for d in summary.duplicates_resolved],
        "organized_files": [str(p) for p in summary.organized_files],
    }


def _is_safe_remote_filename(filename: str) -> bool:
    if not filename or not isinstance(filename, str):
        return False
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    p = Path(filename)
    return p.name == filename and p.suffix.lower() in AUDIO_SUFFIXES


def _remote_track_exists_locally(
    track_info: Dict[str, Any],
    lib_dir: Path,
    local_index: LibraryIndex,
) -> bool:
    filename = str(track_info.get("filename") or "")
    if filename and (lib_dir / filename).exists():
        return True
    if filename and local_index.contains_stem(Path(filename).stem):
        return True

    artist = str(track_info.get("artist") or "").strip()
    title = str(track_info.get("title") or "").strip()
    if (
        artist
        and title
        and artist != DEFAULT_FALLBACK_ARTIST
        and title != DEFAULT_FALLBACK_TITLE
        and local_index.contains_track(artist, title)
    ):
        return True
    return False


def _stream_remote_file(
    http: httpx.Client,
    stream_url: str,
    dest_path: Path,
) -> Path:
    part_path = dest_path.parent / f"{dest_path.name}.{uuid.uuid4().hex[:8]}.part"
    promoted = False
    try:
        with http.stream("GET", stream_url) as resp:
            if resp.status_code >= 400:
                resp.raise_for_status()

            expected_length: Optional[int] = None
            raw_cl = resp.headers.get("content-length")
            if raw_cl is not None:
                try:
                    expected_length = int(raw_cl.strip())
                except ValueError:
                    expected_length = None

            written = 0
            with open(part_path, "wb") as out_f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    if chunk:
                        out_f.write(chunk)
                        written += len(chunk)
                out_f.flush()

            if expected_length is not None and written < expected_length:
                raise IOError(
                    f"Truncated stream from {stream_url}: received {written} of {expected_length} bytes"
                )
            if written == 0:
                raise IOError(f"Empty audio stream from {stream_url}")

        os.replace(part_path, dest_path)
        promoted = True
        return dest_path
    finally:
        if not promoted and part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass


def run_remote_server_job(
    input_payload: Union[str, bytes],
    remote_base_url: str,
    library_dir: Union[str, Path, None] = None,
    is_csv: bool = False,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Syncs missing tracks through a remote Songetter FastAPI server into local storage."""
    base_url = (remote_base_url or "").strip().rstrip("/")
    parsed_base = urlparse(base_url)
    if parsed_base.scheme.lower() not in {"http", "https"} or not parsed_base.netloc:
        raise ValueError(f"Invalid remote server URL: {remote_base_url}")

    target_dir = (
        library_dir
        if library_dir is not None and str(library_dir).strip()
        else get_default_android_library_dir()
    )
    lib_dir = validate_library_directory(target_dir, create=True)
    records = _parse_input_payload(input_payload, is_csv=is_csv)

    local_index = LibraryIndex.build(lib_dir)
    skipped_tracks: List[Dict[str, Any]] = []
    pending_records: List[TrackRecord] = []

    for rec in records:
        if _record_matches_index(rec, local_index):
            matched_files: List[Path] = []
            if rec.artist and rec.title and local_index.contains_track(rec.artist, rec.title):
                matched_files = local_index.find_track_files(rec.artist, rec.title)
            skipped_tracks.append(
                {
                    "url": rec.url,
                    "artist": rec.artist,
                    "title": rec.title,
                    "status": "skipped",
                    "file_path": str(matched_files[0]) if matched_files else None,
                    "error": None,
                }
            )
        else:
            pending_records.append(rec)

    if not pending_records:
        existing_organized = sorted(
            str(p)
            for p in lib_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )
        return {
            "status": "completed",
            "mode": "remote",
            "remote_base_url": base_url,
            "remote_job_id": None,
            "remote_job_status": "skipped_all_local",
            "library_dir": str(lib_dir),
            "total_tracks": len(skipped_tracks),
            "downloaded": 0,
            "skipped": len(skipped_tracks),
            "failed": 0,
            "tracks": skipped_tracks,
            "duplicates_resolved": [],
            "organized_files": existing_organized,
        }

    remote_job_id: Optional[str] = None
    remote_job_status = "unknown"
    streamed_tracks: List[Dict[str, Any]] = []
    failed_count = 0

    with _use_http_client(client, timeout=60.0) as http:
        job_payload = {
            "tracks": [
                {"url": r.url, "artist": r.artist, "title": r.title}
                for r in pending_records
            ],
            "skip_existing": True,
            "auto_organize": True,
            "auto_deduplicate": True,
        }
        post_resp = http.post(f"{base_url}/api/jobs", json=job_payload)
        if post_resp.status_code >= 400:
            raise RuntimeError(
                f"Remote server POST /api/jobs failed ({post_resp.status_code}): {post_resp.text}"
            )
        job_data = post_resp.json()
        if isinstance(job_data, dict):
            remote_job_id = job_data.get("job_id")
            remote_job_status = str(job_data.get("status") or "completed")

        # Poll remote job if queued/running
        if remote_job_id and remote_job_status in {"queued", "running"}:
            for _ in range(120):
                time.sleep(0.1)
                poll_resp = http.get(f"{base_url}/api/jobs/{quote(str(remote_job_id), safe='')}")
                if poll_resp.status_code >= 400:
                    break
                poll_data = poll_resp.json()
                if isinstance(poll_data, dict):
                    remote_job_status = str(poll_data.get("status") or remote_job_status)
                    if remote_job_status not in {"queued", "running"}:
                        break

        tracks_resp = http.get(f"{base_url}/api/library/tracks")
        if tracks_resp.status_code >= 400:
            raise RuntimeError(
                f"Remote server GET /api/library/tracks failed ({tracks_resp.status_code}): {tracks_resp.text}"
            )
        tracks_payload = tracks_resp.json()
        remote_tracks = (
            tracks_payload.get("tracks", []) if isinstance(tracks_payload, dict) else []
        )

        for item in remote_tracks:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "").strip()
            if not _is_safe_remote_filename(filename):
                continue
            if _remote_track_exists_locally(item, lib_dir, local_index):
                continue

            stream_url = f"{base_url}/api/library/tracks/{quote(filename, safe='')}/stream"
            dest_path = lib_dir / filename
            try:
                final_path = _stream_remote_file(http, stream_url, dest_path)
                streamed_tracks.append(
                    {
                        "url": stream_url,
                        "artist": item.get("artist"),
                        "title": item.get("title"),
                        "status": "downloaded",
                        "file_path": str(final_path),
                        "error": None,
                    }
                )
            except Exception as exc:
                failed_count += 1
                streamed_tracks.append(
                    {
                        "url": stream_url,
                        "artist": item.get("artist"),
                        "title": item.get("title"),
                        "status": "failed",
                        "file_path": None,
                        "error": str(exc),
                    }
                )

    duplicates_resolved: List[DuplicateResolutionResult] = []
    organized_files: List[Path] = []
    if streamed_tracks:
        duplicates_resolved.extend(resolve_duplicates(lib_dir))
        organized_files = flatten_and_organize_library(lib_dir)
        duplicates_resolved.extend(resolve_duplicates(lib_dir))
        organized_files = sorted(
            p
            for p in lib_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )
    else:
        organized_files = sorted(
            p
            for p in lib_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )

    # Refresh streamed file paths after flatten/dedup
    final_by_meta: Dict[Tuple[str, str], Path] = {}
    for p in organized_files:
        meta = extract_metadata(p)
        final_by_meta.setdefault((normalize_text(meta.artist), normalize_text(meta.title)), p)

    for entry in streamed_tracks:
        if entry["status"] == "downloaded":
            artist = str(entry.get("artist") or "").strip()
            title = str(entry.get("title") or "").strip()
            if artist and title:
                matched = final_by_meta.get((normalize_text(artist), normalize_text(title)))
                if matched is not None:
                    entry["file_path"] = str(matched)

    downloaded_count = sum(1 for t in streamed_tracks if t["status"] == "downloaded")
    all_tracks = skipped_tracks + streamed_tracks
    return {
        "status": "completed",
        "mode": "remote",
        "remote_base_url": base_url,
        "remote_job_id": remote_job_id,
        "remote_job_status": remote_job_status,
        "library_dir": str(lib_dir),
        "total_tracks": len(all_tracks),
        "downloaded": downloaded_count,
        "skipped": len(skipped_tracks),
        "failed": failed_count,
        "tracks": all_tracks,
        "duplicates_resolved": [_serialize_duplicate(d) for d in duplicates_resolved],
        "organized_files": [str(p) for p in organized_files],
    }


def _resolve_bridge_library_dir(library_dir: str) -> Path:
    cleaned = (library_dir or "").strip()
    if cleaned:
        return Path(cleaned).expanduser()
    return get_default_android_library_dir()


def bridge_get_status(library_dir: str = "", remote_url: str = "") -> str:
    """Chaquopy JSON facade reporting local library, dependency, and remote status."""
    try:
        target = _resolve_bridge_library_dir(library_dir)
        lib_exists = target.exists() and target.is_dir()
        lib_writable = _is_writable_dir(target) if lib_exists else False
        track_count = 0
        if lib_exists:
            track_count = sum(
                1
                for p in target.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            )

        try:
            import yt_dlp  # type: ignore[import-not-found]  # noqa: F401
            yt_dlp_available = True
        except ImportError:
            yt_dlp_available = False

        remote_clean = (remote_url or "").strip().rstrip("/")
        remote_reachable: Optional[bool] = None
        remote_track_count: Optional[int] = None
        if remote_clean:
            try:
                with httpx.Client(timeout=2.5, follow_redirects=True) as http:
                    resp = http.get(f"{remote_clean}/api/library")
                    remote_reachable = resp.status_code == 200
                    if remote_reachable:
                        payload = resp.json()
                        if isinstance(payload, dict):
                            if isinstance(payload.get("tracks"), list):
                                remote_track_count = len(payload["tracks"])
                            elif payload.get("track_count") is not None:
                                try:
                                    remote_track_count = int(payload["track_count"])
                                except (TypeError, ValueError):
                                    remote_track_count = None
                        if remote_track_count is None:
                            tracks_resp = http.get(f"{remote_clean}/api/library/tracks")
                            if tracks_resp.status_code == 200:
                                tracks_payload = tracks_resp.json()
                                if (
                                    isinstance(tracks_payload, dict)
                                    and isinstance(tracks_payload.get("tracks"), list)
                                ):
                                    remote_track_count = len(tracks_payload["tracks"])
            except Exception:
                remote_reachable = False

        try:
            search_sources = [s["id"] for s in list_search_sources()]
        except Exception:
            search_sources = list(SUPPORTED_SEARCH_SOURCES)

        return json.dumps(
            {
                "ok": True,
                "library_dir": str(target),
                "library_exists": lib_exists,
                "library_writable": lib_writable,
                "track_count": track_count,
                "remote_url": remote_clean,
                "remote_reachable": remote_reachable,
                "remote_track_count": remote_track_count,
                "mornye_available": shutil.which("mornye") is not None,
                "yt_dlp_available": yt_dlp_available,
                "python_version": sys.version.split()[0],
                "search_sources": search_sources,
                "error": None,
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "library_dir": library_dir,
                "library_exists": False,
                "library_writable": False,
                "track_count": 0,
                "remote_url": remote_url,
                "remote_reachable": False if remote_url else None,
                "remote_track_count": None,
                "mornye_available": False,
                "yt_dlp_available": False,
                "python_version": sys.version.split()[0],
                "search_sources": list(SUPPORTED_SEARCH_SOURCES),
                "error": str(exc),
            }
        )


def bridge_list_library(library_dir: str = "") -> str:
    """Chaquopy JSON facade listing flat audio tracks and extracted metadata."""
    try:
        target = _resolve_bridge_library_dir(library_dir)
        if not target.exists():
            return json.dumps(
                {
                    "ok": True,
                    "library_dir": str(target),
                    "exists": False,
                    "count": 0,
                    "tracks": [],
                    "error": None,
                }
            )

        resolved = validate_library_directory(target, create=False)
        audio_paths = sorted(
            (
                p
                for p in resolved.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            ),
            key=lambda p: p.name.lower(),
        )
        tracks: List[Dict[str, Any]] = []
        for audio_path in audio_paths:
            meta = extract_metadata(audio_path)
            tracks.append(
                {
                    "filename": audio_path.name,
                    "artist": meta.artist,
                    "title": meta.title,
                    "bitrate": meta.bitrate,
                    "file_size": meta.file_size,
                    "duration": meta.duration,
                    "format": audio_path.suffix.lstrip(".").lower(),
                    "path": str(meta.path),
                }
            )
        return json.dumps(
            {
                "ok": True,
                "library_dir": str(resolved),
                "exists": True,
                "count": len(tracks),
                "tracks": tracks,
                "error": None,
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "library_dir": library_dir,
                "exists": False,
                "count": 0,
                "tracks": [],
                "error": str(exc),
            }
        )


def bridge_start_job(
    input_text: str,
    library_dir: str = "",
    is_csv: bool = False,
    mode: str = "on_device",
    remote_url: str = "",
) -> str:
    """Chaquopy JSON facade dispatching an on-device or remote sync download job."""
    try:
        norm_mode = (mode or "on_device").strip().lower()
        target = _resolve_bridge_library_dir(library_dir)
        if norm_mode in {"remote", "remote_sync", "server"}:
            result = run_remote_server_job(
                input_payload=input_text,
                remote_base_url=remote_url,
                library_dir=target,
                is_csv=bool(is_csv),
            )
        else:
            result = run_on_device_job(
                input_payload=input_text,
                library_dir=target,
                is_csv=bool(is_csv),
            )
        result["ok"] = True
        result.setdefault("error", None)
        return json.dumps(result)
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "status": "error",
                "mode": mode,
                "library_dir": library_dir,
                "total_tracks": 0,
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
                "tracks": [],
                "duplicates_resolved": [],
                "organized_files": [],
                "error": str(exc),
            }
        )


def bridge_search(query: str, sources_csv: str = "", limit: int = 10) -> str:
    """Chaquopy JSON facade running federated multi-source track search."""
    try:
        clean_query = (query or "").strip()
        if not clean_query:
            return json.dumps(
                {
                    "ok": False,
                    "query": query,
                    "count": 0,
                    "results": [],
                    "errors": {},
                    "error": "Search query cannot be empty.",
                }
            )
        sources_arg = (sources_csv or "").strip() or "all"
        capped_limit = max(1, min(int(limit), 100))
        results, errors = search_tracks_with_errors(
            query=clean_query,
            sources=sources_arg,
            limit=capped_limit,
            deduplicate=True,
        )
        return json.dumps(
            {
                "ok": True,
                "query": clean_query,
                "sources": sources_arg,
                "count": len(results),
                "results": [r.to_dict() for r in results],
                "errors": errors,
                "error": None,
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "query": query,
                "count": 0,
                "results": [],
                "errors": {},
                "error": str(exc),
            }
        )


def bridge_organize_library(library_dir: str = "") -> str:
    """Chaquopy JSON facade executing quality deduplication and flat organization."""
    try:
        target = _resolve_bridge_library_dir(library_dir)
        lib_dir = validate_library_directory(target, create=True)
        duplicates = resolve_duplicates(lib_dir)
        organized = flatten_and_organize_library(lib_dir)
        duplicates.extend(resolve_duplicates(lib_dir))
        organized = sorted(
            p
            for p in lib_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )
        return json.dumps(
            {
                "ok": True,
                "library_dir": str(lib_dir),
                "duplicates_resolved": [_serialize_duplicate(d) for d in duplicates],
                "duplicates_count": len(duplicates),
                "organized_count": len(organized),
                "organized_files": [str(p) for p in organized],
                "error": None,
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "library_dir": library_dir,
                "duplicates_resolved": [],
                "duplicates_count": 0,
                "organized_count": 0,
                "organized_files": [],
                "error": str(exc),
            }
        )
