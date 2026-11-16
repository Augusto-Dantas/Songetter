"""Federated metadata and track search providers for Songetter.

Provides a pluggable SearchProvider interface, six built-in httpx-backed search
providers (Deezer, TIDAL, iTunes, MusicBrainz, Qobuz, Spotify), a thread-safe
provider registry, federated multi-source orchestration with error isolation,
ISRC/metadata deduplication, and TrackRecord/CSV export helpers.
"""

from __future__ import annotations

import csv
import inspect
import io
import os
import re
import threading
import time
import weakref
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    runtime_checkable,
)
from urllib.parse import quote, urlparse

import httpx

from songetter.config import SUPPORTED_SEARCH_SOURCES
from songetter.csv_processor import TrackRecord
from songetter.deduplicator import normalize_text

__all__ = [
    "SearchError",
    "SearchProviderError",
    "UnsupportedSearchSourceError",
    "SearchResult",
    "SearchProvider",
    "SearchProviderProtocol",
    "DeezerSearchProvider",
    "TidalSearchProvider",
    "ItunesSearchProvider",
    "MusicBrainzSearchProvider",
    "QobuzSearchProvider",
    "SpotifySearchProvider",
    "register_search_provider",
    "get_search_provider",
    "list_search_sources",
    "search_tracks",
    "search_tracks_with_errors",
    "search_results_to_csv",
]

_VALID_SEARCH_TYPES = ("track", "album", "artist")
_USER_AGENT = "Songetter/0.1.0"


class SearchError(Exception):
    """Base exception for search operations."""


class SearchProviderError(SearchError):
    """Raised when a search provider fails to query or parse results."""

    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class UnsupportedSearchSourceError(SearchError):
    """Raised when an unknown or unsupported search source is requested."""


@dataclass
class SearchResult:
    """Normalized track, album, or artist search result from a provider."""

    source: str
    track_id: str
    title: str
    artist: str
    album: Optional[str] = None
    duration_seconds: Optional[int] = None
    url: str = ""
    preview_url: Optional[str] = None
    cover_url: Optional[str] = None
    isrc: Optional[str] = None
    quality_hint: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_track_record(self, use_preview_if_no_url: bool = False) -> TrackRecord:
        """Convert this search result into a TrackRecord for download ingestion."""
        target_url = (self.url or "").strip()
        if not target_url and use_preview_if_no_url:
            target_url = (self.preview_url or "").strip()

        raw_row: Dict[str, str] = {
            "source": self.source,
            "track_id": str(self.track_id),
            "url": target_url,
            "artist": self.artist,
            "title": self.title,
        }
        if self.album:
            raw_row["album"] = self.album
        if self.isrc:
            raw_row["isrc"] = self.isrc
        if self.duration_seconds is not None:
            raw_row["duration_seconds"] = str(self.duration_seconds)
        if self.preview_url:
            raw_row["preview_url"] = self.preview_url
        if self.cover_url:
            raw_row["cover_url"] = self.cover_url
        if self.quality_hint:
            raw_row["quality_hint"] = self.quality_hint

        return TrackRecord(
            url=target_url,
            artist=self.artist.strip() or None,
            title=self.title.strip() or None,
            raw_row=raw_row,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this search result into a plain JSON-safe dictionary."""
        return {
            "source": self.source,
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_seconds": self.duration_seconds,
            "url": self.url,
            "preview_url": self.preview_url,
            "cover_url": self.cover_url,
            "isrc": self.isrc,
            "quality_hint": self.quality_hint,
            "extra": dict(self.extra),
        }


@runtime_checkable
class SearchProviderProtocol(Protocol):
    """Structural protocol for pluggable music search providers."""

    name: str
    display_name: str
    requires_auth: bool
    description: str

    def is_configured(self) -> bool:
        """Return True when the provider has all required configuration."""
        ...

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        """Execute a search query against the provider."""
        ...


class SearchProvider(ABC):
    """Abstract base class for pluggable music search providers."""

    name: str = ""
    display_name: str = ""
    requires_auth: bool = False
    description: str = ""

    def is_configured(self) -> bool:
        """Return True when required credentials or settings are present."""
        return not self.requires_auth

    @abstractmethod
    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        """Query the provider and return normalized SearchResult objects."""


def _safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_duration_seconds(value: Any) -> Optional[int]:
    """Parse integer seconds or ISO-8601 duration strings (e.g., PT3M45S)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        match = re.fullmatch(
            r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2) or 0)
            seconds = int(float(match.group(3) or 0))
            return hours * 3600 + minutes * 60 + seconds
    return None


def _first_non_empty(mapping: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _parse_mb_artist_credit(artist_credit: Any, fallback: str = "") -> str:
    """Join MusicBrainz artist-credit segments into a single display string."""
    if not isinstance(artist_credit, list):
        return fallback
    parts: List[str] = []
    for entry in artist_credit:
        if isinstance(entry, dict):
            name = entry.get("name") or (entry.get("artist") or {}).get("name") or ""
            if name:
                parts.append(str(name))
                parts.append(str(entry.get("joinphrase") or ""))
        elif isinstance(entry, str):
            parts.append(entry)
    joined = "".join(parts).strip()
    return joined or fallback


class _OAuth2TokenManager:
    """Thread-safe OAuth2 client_credentials token fetcher and cache."""

    def __init__(self, token_url: str, provider_name: str) -> None:
        self._token_url = token_url
        self._provider_name = provider_name
        self._lock = threading.Lock()
        self._default_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
        self._client_caches: weakref.WeakKeyDictionary[
            httpx.Client, Dict[Tuple[str, str], Tuple[str, float]]
        ] = weakref.WeakKeyDictionary()

    def _cache_for_client(
        self, client: Optional[httpx.Client]
    ) -> Dict[Tuple[str, str], Tuple[str, float]]:
        if client is None:
            return self._default_cache
        cache = self._client_caches.get(client)
        if cache is None:
            cache = {}
            self._client_caches[client] = cache
        return cache

    def get_token(
        self,
        client: httpx.Client,
        client_id: str,
        client_secret: str,
        *,
        is_injected_client: bool,
    ) -> str:
        cache_key = (client_id, client_secret)
        now = time.monotonic()
        with self._lock:
            cache = self._cache_for_client(client if is_injected_client else None)
            cached = cache.get(cache_key)
            if cached and now < cached[1] - 30.0:
                return cached[0]

        try:
            response = client.post(
                self._token_url,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise SearchProviderError(
                f"{self._provider_name} OAuth2 token request failed with HTTP {exc.response.status_code}.",
                provider=self._provider_name,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise SearchProviderError(
                f"{self._provider_name} OAuth2 token request error: {exc}",
                provider=self._provider_name,
            ) from exc
        except ValueError as exc:
            raise SearchProviderError(
                f"{self._provider_name} OAuth2 returned invalid JSON.",
                provider=self._provider_name,
            ) from exc

        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(access_token, str) or not access_token.strip():
            raise SearchProviderError(
                f"{self._provider_name} OAuth2 response did not contain an access_token.",
                provider=self._provider_name,
            )

        expires_in = float(payload.get("expires_in") or 3600)
        with self._lock:
            cache = self._cache_for_client(client if is_injected_client else None)
            cache[cache_key] = (access_token.strip(), time.monotonic() + max(60.0, expires_in))
        return access_token.strip()


class _BaseHttpSearchProvider(SearchProvider):
    """Shared HTTP client lifecycle, argument validation, and error wrapping."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    @contextmanager
    def _use_client(self, client: Optional[httpx.Client]) -> Iterator[httpx.Client]:
        if client is not None:
            yield client
            return
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as created:
            yield created

    def _validate_args(
        self,
        query: str,
        limit: int,
        search_type: str,
        supported_types: Sequence[str] = _VALID_SEARCH_TYPES,
    ) -> Optional[Tuple[str, int, str]]:
        normalized_type = (search_type or "track").strip().lower()
        if normalized_type not in supported_types:
            raise SearchProviderError(
                f"Unsupported search_type '{search_type}' for {self.name}. "
                f"Expected one of: {', '.join(supported_types)}.",
                provider=self.name,
            )
        cleaned_query = (query or "").strip()
        if not cleaned_query or limit <= 0:
            return None
        return cleaned_query, int(limit), normalized_type

    def _get_json(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        try:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise SearchProviderError(
                f"{self.display_name or self.name} search failed with HTTP {exc.response.status_code}.",
                provider=self.name,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise SearchProviderError(
                f"{self.display_name or self.name} request failed: {exc}",
                provider=self.name,
            ) from exc
        except ValueError as exc:
            raise SearchProviderError(
                f"{self.display_name or self.name} returned invalid JSON.",
                provider=self.name,
            ) from exc

        if not isinstance(payload, dict):
            raise SearchProviderError(
                f"{self.display_name or self.name} returned unexpected JSON root type.",
                provider=self.name,
            )
        return payload


class DeezerSearchProvider(_BaseHttpSearchProvider):
    """Key-free public search provider using the Deezer REST API."""

    name = "deezer"
    display_name = "Deezer"
    requires_auth = False
    description = "Public Deezer catalog search (no API key required)."

    _ENDPOINTS = {
        "track": "https://api.deezer.com/search/track",
        "album": "https://api.deezer.com/search/album",
        "artist": "https://api.deezer.com/search/artist",
        "all": "https://api.deezer.com/search",
    }

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        validated = self._validate_args(
            query, limit, search_type, supported_types=("track", "album", "artist", "all")
        )
        if validated is None:
            return []
        cleaned_query, capped_limit, norm_type = validated

        with self._use_client(client) as http:
            payload = self._get_json(
                http,
                self._ENDPOINTS[norm_type],
                params={"q": cleaned_query, "limit": capped_limit},
            )

        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            raise SearchProviderError(
                f"Deezer API error: {err.get('message') or err.get('type') or 'unknown error'}",
                provider=self.name,
                status_code=_safe_int(err.get("code")),
            )

        items = payload.get("data") or []
        results: List[SearchResult] = []
        for item in items[:capped_limit]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            item_type = str(item.get("type") or norm_type).lower()

            if norm_type == "artist" or item_type == "artist":
                artist_name = str(item.get("name") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=artist_name,
                        artist=artist_name,
                        url=str(item.get("link") or (f"https://www.deezer.com/artist/{item_id}" if item_id else "")),
                        cover_url=_first_non_empty(
                            item, "picture_xl", "picture_big", "picture_medium", "picture"
                        ),
                        extra={"type": "artist", "nb_fan": item.get("nb_fan")},
                    )
                )
            elif norm_type == "album" or item_type == "album":
                album_title = str(item.get("title") or "")
                artist_obj = item.get("artist") if isinstance(item.get("artist"), dict) else {}
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=album_title,
                        artist=str(artist_obj.get("name") or ""),
                        album=album_title or None,
                        url=str(item.get("link") or (f"https://www.deezer.com/album/{item_id}" if item_id else "")),
                        cover_url=_first_non_empty(
                            item, "cover_xl", "cover_big", "cover_medium", "cover"
                        ),
                        extra={"type": "album", "nb_tracks": item.get("nb_tracks")},
                    )
                )
            else:
                artist_obj = item.get("artist") if isinstance(item.get("artist"), dict) else {}
                album_obj = item.get("album") if isinstance(item.get("album"), dict) else {}
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=str(item.get("title") or item.get("title_short") or ""),
                        artist=str(artist_obj.get("name") or ""),
                        album=album_obj.get("title") or None,
                        duration_seconds=_safe_int(item.get("duration")),
                        url=str(item.get("link") or (f"https://www.deezer.com/track/{item_id}" if item_id else "")),
                        preview_url=_first_non_empty(item, "preview"),
                        cover_url=_first_non_empty(
                            album_obj, "cover_xl", "cover_big", "cover_medium", "cover"
                        ),
                        isrc=_first_non_empty(item, "isrc"),
                        extra={
                            "type": "track",
                            "rank": item.get("rank"),
                            "explicit_lyrics": item.get("explicit_lyrics"),
                        },
                    )
                )
        return results


class ItunesSearchProvider(_BaseHttpSearchProvider):
    """Key-free public search provider using the Apple iTunes Search API."""

    name = "itunes"
    display_name = "iTunes / Apple Music"
    requires_auth = False
    description = "Public iTunes Search API (no API key required)."

    _ENDPOINT = "https://itunes.apple.com/search"
    _ENTITIES = {
        "track": "song",
        "album": "album",
        "artist": "musicArtist",
    }

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        validated = self._validate_args(query, limit, search_type)
        if validated is None:
            return []
        cleaned_query, capped_limit, norm_type = validated

        with self._use_client(client) as http:
            payload = self._get_json(
                http,
                self._ENDPOINT,
                params={
                    "term": cleaned_query,
                    "limit": capped_limit,
                    "media": "music",
                    "entity": self._ENTITIES[norm_type],
                },
            )

        items = payload.get("results") or []
        results: List[SearchResult] = []
        for item in items[:capped_limit]:
            if not isinstance(item, dict):
                continue
            item_id = str(
                item.get("trackId") or item.get("collectionId") or item.get("artistId") or ""
            )
            duration_ms = _safe_int(item.get("trackTimeMillis"))
            duration_seconds = duration_ms // 1000 if duration_ms is not None else None
            cover = _first_non_empty(
                item, "artworkUrl600", "artworkUrl100", "artworkUrl60", "artworkUrl30"
            )
            artist_name = str(item.get("artistName") or "")

            if norm_type == "artist":
                title = artist_name
                album = None
                url = _first_non_empty(item, "artistLinkUrl", "artistViewUrl") or ""
            elif norm_type == "album":
                title = str(item.get("collectionName") or "")
                album = title or None
                url = _first_non_empty(item, "collectionViewUrl", "artistViewUrl") or ""
            else:
                title = str(item.get("trackName") or item.get("collectionName") or "")
                album = _first_non_empty(item, "collectionName")
                url = _first_non_empty(item, "trackViewUrl", "collectionViewUrl") or ""

            results.append(
                SearchResult(
                    source=self.name,
                    track_id=item_id,
                    title=title,
                    artist=artist_name,
                    album=album,
                    duration_seconds=duration_seconds,
                    url=url,
                    preview_url=_first_non_empty(item, "previewUrl"),
                    cover_url=cover,
                    extra={
                        "genre": item.get("primaryGenreName"),
                        "release_date": item.get("releaseDate"),
                        "country": item.get("country"),
                        "kind": item.get("kind"),
                    },
                )
            )
        return results


class MusicBrainzSearchProvider(_BaseHttpSearchProvider):
    """Key-free public search provider using the MusicBrainz Web Service v2."""

    name = "musicbrainz"
    display_name = "MusicBrainz"
    requires_auth = False
    description = "Open MusicBrainz recording and metadata database."

    _ENDPOINTS = {
        "track": ("https://musicbrainz.org/ws/2/recording", "recordings", "recording"),
        "album": ("https://musicbrainz.org/ws/2/release", "releases", "release"),
        "artist": ("https://musicbrainz.org/ws/2/artist", "artists", "artist"),
    }

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        validated = self._validate_args(query, limit, search_type)
        if validated is None:
            return []
        cleaned_query, capped_limit, norm_type = validated
        endpoint, list_key, entity_path = self._ENDPOINTS[norm_type]

        with self._use_client(client) as http:
            payload = self._get_json(
                http,
                endpoint,
                params={"query": cleaned_query, "limit": capped_limit, "fmt": "json"},
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )

        items = payload.get(list_key) or []
        results: List[SearchResult] = []
        for item in items[:capped_limit]:
            if not isinstance(item, dict):
                continue
            mbid = str(item.get("id") or "")
            url = f"https://musicbrainz.org/{entity_path}/{mbid}" if mbid else ""

            if norm_type == "artist":
                name = str(item.get("name") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=mbid,
                        title=name,
                        artist=name,
                        url=url,
                        extra={"score": item.get("score"), "mbid": mbid, "disambiguation": item.get("disambiguation")},
                    )
                )
            elif norm_type == "album":
                title = str(item.get("title") or "")
                artist_name = _parse_mb_artist_credit(
                    item.get("artist-credit"), fallback=str(item.get("artist") or "")
                )
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=mbid,
                        title=title,
                        artist=artist_name,
                        album=title or None,
                        url=url,
                        extra={"score": item.get("score"), "mbid": mbid, "date": item.get("date")},
                    )
                )
            else:
                releases = item.get("releases") if isinstance(item.get("releases"), list) else []
                first_release = releases[0] if releases and isinstance(releases[0], dict) else {}
                isrcs = item.get("isrcs") if isinstance(item.get("isrcs"), list) else []
                length_ms = _safe_int(item.get("length"))
                duration_seconds = length_ms // 1000 if length_ms is not None else None
                artist_name = _parse_mb_artist_credit(
                    item.get("artist-credit"), fallback=str(item.get("artist") or "")
                )
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=mbid,
                        title=str(item.get("title") or ""),
                        artist=artist_name,
                        album=_first_non_empty(first_release, "title"),
                        duration_seconds=duration_seconds,
                        url=url,
                        isrc=str(isrcs[0]).strip() if isrcs and isrcs[0] else None,
                        extra={"score": item.get("score"), "mbid": mbid},
                    )
                )
        return results


class TidalSearchProvider(_BaseHttpSearchProvider):
    """TIDAL catalog search supporting direct Bearer tokens and OAuth2 client credentials."""

    name = "tidal"
    display_name = "TIDAL"
    requires_auth = True
    description = "TIDAL catalog search (requires SONGETTER_TIDAL_TOKEN or TIDAL_CLIENT_ID + TIDAL_CLIENT_SECRET)."

    _TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
    _DEFAULT_API_URL = "https://openapi.tidal.com"

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        country_code: Optional[str] = None,
        api_url: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._token = token
        self._client_id = client_id
        self._client_secret = client_secret
        self._country_code = country_code
        self._api_url = api_url
        self._oauth = _OAuth2TokenManager(self._TOKEN_URL, self.name)

    def _credentials(self) -> Tuple[str, str, str]:
        token = (self._token if self._token is not None else os.environ.get("SONGETTER_TIDAL_TOKEN", "")).strip()
        client_id = (self._client_id if self._client_id is not None else os.environ.get("TIDAL_CLIENT_ID", "")).strip()
        client_secret = (
            self._client_secret if self._client_secret is not None else os.environ.get("TIDAL_CLIENT_SECRET", "")
        ).strip()
        return token, client_id, client_secret

    def is_configured(self) -> bool:
        token, client_id, client_secret = self._credentials()
        return bool(token) or bool(client_id and client_secret)

    def _resolve_bearer_token(self, client: httpx.Client, *, is_injected_client: bool) -> str:
        token, client_id, client_secret = self._credentials()
        if token:
            return token
        if client_id and client_secret:
            return self._oauth.get_token(
                client, client_id, client_secret, is_injected_client=is_injected_client
            )
        raise SearchProviderError(
            "TIDAL search is not configured. Set SONGETTER_TIDAL_TOKEN or TIDAL_CLIENT_ID and TIDAL_CLIENT_SECRET.",
            provider=self.name,
        )

    @staticmethod
    def _tidal_cover_url(cover_id: Any) -> Optional[str]:
        if not isinstance(cover_id, str) or not cover_id.strip():
            return None
        cleaned = cover_id.strip()
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        return f"https://resources.tidal.com/images/{cleaned.replace('-', '/')}/640x640.jpg"

    def _parse_v1_items(
        self, payload: Dict[str, Any], norm_type: str, limit: int
    ) -> List[SearchResult]:
        segment = f"{norm_type}s"
        items = payload.get("items")
        if items is None and isinstance(payload.get(segment), dict):
            items = payload[segment].get("items")
        if not isinstance(items, list):
            return []

        results: List[SearchResult] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            artists_list = item.get("artists") if isinstance(item.get("artists"), list) else []
            artist_names = [
                str(a.get("name")) for a in artists_list if isinstance(a, dict) and a.get("name")
            ]
            if not artist_names and isinstance(item.get("artist"), dict) and item["artist"].get("name"):
                artist_names = [str(item["artist"]["name"])]
            artist_str = ", ".join(artist_names)

            if norm_type == "artist":
                name = str(item.get("name") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=name,
                        artist=name,
                        url=str(item.get("url") or (f"https://tidal.com/browse/artist/{item_id}" if item_id else "")),
                        cover_url=self._tidal_cover_url(item.get("picture")),
                        extra={"type": "artist"},
                    )
                )
            elif norm_type == "album":
                title = str(item.get("title") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=title,
                        artist=artist_str,
                        album=title or None,
                        duration_seconds=_safe_int(item.get("duration")),
                        url=str(item.get("url") or (f"https://tidal.com/browse/album/{item_id}" if item_id else "")),
                        cover_url=self._tidal_cover_url(item.get("cover")),
                        quality_hint=_first_non_empty(item, "audioQuality"),
                        extra={"type": "album"},
                    )
                )
            else:
                album_obj = item.get("album") if isinstance(item.get("album"), dict) else {}
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=str(item.get("title") or ""),
                        artist=artist_str,
                        album=_first_non_empty(album_obj, "title"),
                        duration_seconds=_safe_int(item.get("duration")),
                        url=str(item.get("url") or (f"https://tidal.com/browse/track/{item_id}" if item_id else "")),
                        cover_url=self._tidal_cover_url(album_obj.get("cover")),
                        isrc=_first_non_empty(item, "isrc"),
                        quality_hint=_first_non_empty(item, "audioQuality"),
                        extra={"type": "track"},
                    )
                )
        return results

    def _parse_v2_payload(
        self, payload: Dict[str, Any], norm_type: str, limit: int
    ) -> List[SearchResult]:
        if isinstance(payload.get("items"), list):
            return self._parse_v1_items(payload, norm_type, limit)

        target_collection = f"{norm_type}s"
        included = payload.get("included") if isinstance(payload.get("included"), list) else []
        included_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for resource in included:
            if isinstance(resource, dict) and resource.get("type") and resource.get("id") is not None:
                included_index[(str(resource["type"]), str(resource["id"]))] = resource

        data = payload.get("data")
        target_refs: List[Dict[str, Any]] = []
        if isinstance(data, list):
            target_refs = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            rels = data.get("relationships") if isinstance(data.get("relationships"), dict) else {}
            rel_data = (rels.get(target_collection) or {}).get("data")
            if isinstance(rel_data, list):
                for ref in rel_data:
                    if isinstance(ref, dict):
                        resolved = included_index.get(
                            (str(ref.get("type") or target_collection), str(ref.get("id") or ""))
                        )
                        target_refs.append(resolved if resolved is not None else ref)
            elif data.get("type") == target_collection:
                target_refs = [data]

        if not target_refs and included:
            target_refs = [
                r
                for r in included
                if isinstance(r, dict) and str(r.get("type")) == target_collection
            ]

        def _extract_image(attrs: Dict[str, Any]) -> Optional[str]:
            links = attrs.get("imageLinks")
            if isinstance(links, list):
                for entry in links:
                    if isinstance(entry, dict) and entry.get("href"):
                        return str(entry["href"])
            return self._tidal_cover_url(attrs.get("cover") or attrs.get("picture"))

        def _extract_external_url(attrs: Dict[str, Any], entity: str, item_id: str) -> str:
            ext_links = attrs.get("externalLinks")
            if isinstance(ext_links, list):
                for entry in ext_links:
                    if isinstance(entry, dict) and entry.get("href"):
                        return str(entry["href"])
            if attrs.get("url"):
                return str(attrs["url"])
            return f"https://tidal.com/browse/{entity}/{item_id}" if item_id else ""

        results: List[SearchResult] = []
        for resource in target_refs[:limit]:
            item_id = str(resource.get("id") or "")
            attrs = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
            rels = resource.get("relationships") if isinstance(resource.get("relationships"), dict) else {}

            artist_names: List[str] = []
            artist_rel = (rels.get("artists") or {}).get("data")
            if isinstance(artist_rel, list):
                for ref in artist_rel:
                    if isinstance(ref, dict):
                        artist_res = included_index.get(
                            (str(ref.get("type") or "artists"), str(ref.get("id") or ""))
                        )
                        if artist_res:
                            name = (artist_res.get("attributes") or {}).get("name")
                            if name:
                                artist_names.append(str(name))
            if not artist_names and attrs.get("artistName"):
                artist_names = [str(attrs["artistName"])]
            artist_str = ", ".join(artist_names)

            media_tags = attrs.get("mediaTags")
            quality_hint = (
                ", ".join(str(t) for t in media_tags if t)
                if isinstance(media_tags, list) and media_tags
                else _first_non_empty(attrs, "audioQuality")
            )

            if norm_type == "artist":
                name = str(attrs.get("name") or attrs.get("title") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=name,
                        artist=name,
                        url=_extract_external_url(attrs, "artist", item_id),
                        cover_url=_extract_image(attrs),
                        extra={"type": "artist"},
                    )
                )
            elif norm_type == "album":
                title = str(attrs.get("title") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=title,
                        artist=artist_str,
                        album=title or None,
                        duration_seconds=_parse_duration_seconds(attrs.get("duration")),
                        url=_extract_external_url(attrs, "album", item_id),
                        cover_url=_extract_image(attrs),
                        quality_hint=quality_hint,
                        extra={"type": "album"},
                    )
                )
            else:
                album_title = _first_non_empty(attrs, "albumTitle")
                cover_url = _extract_image(attrs)
                album_rel = (rels.get("albums") or {}).get("data")
                if isinstance(album_rel, list) and album_rel and isinstance(album_rel[0], dict):
                    album_res = included_index.get(
                        (str(album_rel[0].get("type") or "albums"), str(album_rel[0].get("id") or ""))
                    )
                    if album_res:
                        album_attrs = album_res.get("attributes") or {}
                        album_title = album_title or _first_non_empty(album_attrs, "title")
                        cover_url = cover_url or _extract_image(album_attrs)

                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=str(attrs.get("title") or ""),
                        artist=artist_str,
                        album=album_title,
                        duration_seconds=_parse_duration_seconds(attrs.get("duration")),
                        url=_extract_external_url(attrs, "track", item_id),
                        cover_url=cover_url,
                        isrc=_first_non_empty(attrs, "isrc"),
                        quality_hint=quality_hint,
                        extra={"type": "track", "explicit": attrs.get("explicit")},
                    )
                )
        return results

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        validated = self._validate_args(query, limit, search_type)
        if validated is None:
            return []
        cleaned_query, capped_limit, norm_type = validated

        country_code = (
            self._country_code
            if self._country_code is not None
            else os.environ.get("SONGETTER_TIDAL_COUNTRY", "US")
        ).strip() or "US"
        base_url = (
            self._api_url
            if self._api_url is not None
            else os.environ.get("SONGETTER_TIDAL_API_URL", self._DEFAULT_API_URL)
        ).strip().rstrip("/") or self._DEFAULT_API_URL

        with self._use_client(client) as http:
            access_token = self._resolve_bearer_token(http, is_injected_client=client is not None)
            parsed_base = urlparse(base_url)
            use_v1 = (parsed_base.hostname or "").lower() == "api.tidal.com" or base_url.endswith("/v1")
            if use_v1:
                v1_root = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
                endpoint = f"{v1_root}/search/{norm_type}s"
                payload = self._get_json(
                    http,
                    endpoint,
                    params={
                        "query": cleaned_query,
                        "limit": capped_limit,
                        "countryCode": country_code,
                    },
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
                return self._parse_v1_items(payload, norm_type, capped_limit)

            v2_root = base_url if base_url.endswith("/v2") else f"{base_url}/v2"
            include_map = {
                "track": "tracks,tracks.artists,tracks.albums",
                "album": "albums,albums.artists",
                "artist": "artists",
            }
            endpoint = f"{v2_root}/searchResults/{quote(cleaned_query, safe='')}"
            payload = self._get_json(
                http,
                endpoint,
                params={
                    "countryCode": country_code,
                    "include": include_map[norm_type],
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.api+json, application/json",
                },
            )
            return self._parse_v2_payload(payload, norm_type, capped_limit)


class QobuzSearchProvider(_BaseHttpSearchProvider):
    """Qobuz catalog search using SONGETTER_QOBUZ_APP_ID / SONGETTER_QOBUZ_TOKEN."""

    name = "qobuz"
    display_name = "Qobuz"
    requires_auth = True
    description = "Qobuz Hi-Res catalog search (requires SONGETTER_QOBUZ_APP_ID or SONGETTER_QOBUZ_TOKEN)."

    _BASE_URL = "https://www.qobuz.com/api.json/0.2"

    def __init__(
        self,
        *,
        app_id: Optional[str] = None,
        user_auth_token: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._app_id = app_id
        self._user_auth_token = user_auth_token

    def _credentials(self) -> Tuple[str, str]:
        app_id = (
            self._app_id if self._app_id is not None else os.environ.get("SONGETTER_QOBUZ_APP_ID", "")
        ).strip()
        token = (
            self._user_auth_token
            if self._user_auth_token is not None
            else os.environ.get("SONGETTER_QOBUZ_TOKEN", "")
        ).strip()
        return app_id, token

    def is_configured(self) -> bool:
        app_id, token = self._credentials()
        return bool(app_id or token)

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        validated = self._validate_args(query, limit, search_type)
        if validated is None:
            return []
        cleaned_query, capped_limit, norm_type = validated

        app_id, token = self._credentials()
        if not app_id and not token:
            raise SearchProviderError(
                "Qobuz search is not configured. Set SONGETTER_QOBUZ_APP_ID or SONGETTER_QOBUZ_TOKEN.",
                provider=self.name,
            )

        params: Dict[str, Any] = {"query": cleaned_query, "limit": capped_limit}
        headers: Dict[str, str] = {"Accept": "application/json"}
        if app_id:
            params["app_id"] = app_id
            headers["X-App-Id"] = app_id
        if token:
            headers["X-User-Auth-Token"] = token

        endpoint = f"{self._BASE_URL}/{norm_type}/search"
        with self._use_client(client) as http:
            payload = self._get_json(http, endpoint, params=params, headers=headers)

        container = payload.get(f"{norm_type}s")
        items = container.get("items") if isinstance(container, dict) else payload.get("items")
        if not isinstance(items, list):
            return []

        results: List[SearchResult] = []
        for item in items[:capped_limit]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")

            bit_depth = item.get("maximum_bit_depth")
            sample_rate = item.get("maximum_sampling_rate")
            if bit_depth and sample_rate:
                quality_hint: Optional[str] = f"{bit_depth}-bit/{sample_rate}kHz"
            elif item.get("hires") or item.get("hires_streamable"):
                quality_hint = "Hi-Res"
            else:
                quality_hint = None

            if norm_type == "artist":
                name = str(item.get("name") or "")
                image = item.get("image") if isinstance(item.get("image"), dict) else {}
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=name,
                        artist=name,
                        url=str(item.get("url") or (f"https://open.qobuz.com/artist/{item_id}" if item_id else "")),
                        cover_url=_first_non_empty(image, "large", "medium", "small", "thumbnail"),
                        extra={"type": "artist"},
                    )
                )
            elif norm_type == "album":
                title = str(item.get("title") or "")
                artist_obj = item.get("artist") if isinstance(item.get("artist"), dict) else {}
                image = item.get("image") if isinstance(item.get("image"), dict) else {}
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=title,
                        artist=str(artist_obj.get("name") or ""),
                        album=title or None,
                        duration_seconds=_safe_int(item.get("duration")),
                        url=str(item.get("url") or (f"https://open.qobuz.com/album/{item_id}" if item_id else "")),
                        cover_url=_first_non_empty(image, "large", "small", "thumbnail"),
                        quality_hint=quality_hint,
                        extra={"type": "album", "tracks_count": item.get("tracks_count")},
                    )
                )
            else:
                album_obj = item.get("album") if isinstance(item.get("album"), dict) else {}
                performer = item.get("performer") if isinstance(item.get("performer"), dict) else {}
                album_artist = (
                    album_obj.get("artist") if isinstance(album_obj.get("artist"), dict) else {}
                )
                artist_name = str(performer.get("name") or album_artist.get("name") or "")
                image = album_obj.get("image") if isinstance(album_obj.get("image"), dict) else {}
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=str(item.get("title") or ""),
                        artist=artist_name,
                        album=_first_non_empty(album_obj, "title"),
                        duration_seconds=_safe_int(item.get("duration")),
                        url=str(item.get("url") or (f"https://open.qobuz.com/track/{item_id}" if item_id else "")),
                        preview_url=_first_non_empty(item, "preview_url"),
                        cover_url=_first_non_empty(image, "large", "small", "thumbnail"),
                        isrc=_first_non_empty(item, "isrc"),
                        quality_hint=quality_hint,
                        extra={"type": "track", "hires": bool(item.get("hires"))},
                    )
                )
        return results


class SpotifySearchProvider(_BaseHttpSearchProvider):
    """Spotify Web API search supporting direct tokens and OAuth2 client credentials."""

    name = "spotify"
    display_name = "Spotify"
    requires_auth = True
    description = "Spotify Web API search (requires SONGETTER_SPOTIFY_TOKEN or SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET)."

    _TOKEN_URL = "https://accounts.spotify.com/api/token"
    _SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._token = token
        self._client_id = client_id
        self._client_secret = client_secret
        self._oauth = _OAuth2TokenManager(self._TOKEN_URL, self.name)

    def _credentials(self) -> Tuple[str, str, str]:
        token = (
            self._token if self._token is not None else os.environ.get("SONGETTER_SPOTIFY_TOKEN", "")
        ).strip()
        client_id = (
            self._client_id if self._client_id is not None else os.environ.get("SPOTIFY_CLIENT_ID", "")
        ).strip()
        client_secret = (
            self._client_secret
            if self._client_secret is not None
            else os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        ).strip()
        return token, client_id, client_secret

    def is_configured(self) -> bool:
        token, client_id, client_secret = self._credentials()
        return bool(token) or bool(client_id and client_secret)

    def _resolve_bearer_token(self, client: httpx.Client, *, is_injected_client: bool) -> str:
        token, client_id, client_secret = self._credentials()
        if token:
            return token
        if client_id and client_secret:
            return self._oauth.get_token(
                client, client_id, client_secret, is_injected_client=is_injected_client
            )
        raise SearchProviderError(
            "Spotify search is not configured. Set SONGETTER_SPOTIFY_TOKEN or SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.",
            provider=self.name,
        )

    @staticmethod
    def _first_image(container: Dict[str, Any]) -> Optional[str]:
        images = container.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return _first_non_empty(images[0], "url")
        return None

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        validated = self._validate_args(query, limit, search_type)
        if validated is None:
            return []
        cleaned_query, capped_limit, norm_type = validated

        with self._use_client(client) as http:
            access_token = self._resolve_bearer_token(http, is_injected_client=client is not None)
            payload = self._get_json(
                http,
                self._SEARCH_URL,
                params={"q": cleaned_query, "type": norm_type, "limit": capped_limit},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

        container = payload.get(f"{norm_type}s")
        items = container.get("items") if isinstance(container, dict) else []
        if not isinstance(items, list):
            return []

        results: List[SearchResult] = []
        for item in items[:capped_limit]:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            external_urls = item.get("external_urls") if isinstance(item.get("external_urls"), dict) else {}
            url = str(
                external_urls.get("spotify")
                or (f"https://open.spotify.com/{norm_type}/{item_id}" if item_id else "")
            )

            if norm_type == "artist":
                name = str(item.get("name") or "")
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=name,
                        artist=name,
                        url=url,
                        cover_url=self._first_image(item),
                        extra={"uri": item.get("uri"), "genres": item.get("genres")},
                    )
                )
            elif norm_type == "album":
                title = str(item.get("name") or "")
                artists = item.get("artists") if isinstance(item.get("artists"), list) else []
                artist_name = ", ".join(
                    str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")
                )
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=title,
                        artist=artist_name,
                        album=title or None,
                        url=url,
                        cover_url=self._first_image(item),
                        extra={"uri": item.get("uri"), "release_date": item.get("release_date")},
                    )
                )
            else:
                artists = item.get("artists") if isinstance(item.get("artists"), list) else []
                artist_name = ", ".join(
                    str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")
                )
                album_obj = item.get("album") if isinstance(item.get("album"), dict) else {}
                external_ids = (
                    item.get("external_ids") if isinstance(item.get("external_ids"), dict) else {}
                )
                duration_ms = _safe_int(item.get("duration_ms"))
                duration_seconds = duration_ms // 1000 if duration_ms is not None else None
                results.append(
                    SearchResult(
                        source=self.name,
                        track_id=item_id,
                        title=str(item.get("name") or ""),
                        artist=artist_name,
                        album=_first_non_empty(album_obj, "name"),
                        duration_seconds=duration_seconds,
                        url=url,
                        preview_url=_first_non_empty(item, "preview_url"),
                        cover_url=self._first_image(album_obj),
                        isrc=_first_non_empty(external_ids, "isrc"),
                        extra={
                            "uri": item.get("uri"),
                            "popularity": item.get("popularity"),
                            "explicit": item.get("explicit"),
                        },
                    )
                )
        return results


_REGISTRY_LOCK = threading.RLock()
_PROVIDERS: Dict[str, SearchProviderProtocol] = {}
_ALIASES: Dict[str, str] = {}


def register_search_provider(
    name: str,
    provider: SearchProviderProtocol,
    aliases: Sequence[str] = (),
) -> None:
    """Register or replace a search provider and optional source aliases."""
    canonical = (name or "").strip().lower()
    if not canonical:
        raise ValueError("Provider name cannot be empty.")
    if not callable(getattr(provider, "search", None)):
        raise TypeError("Provider must implement a callable search() method.")

    with _REGISTRY_LOCK:
        _PROVIDERS[canonical] = provider
        # Remove stale aliases previously pointing to this canonical id before adding new ones
        stale = [alias for alias, target in _ALIASES.items() if target == canonical]
        for alias in stale:
            _ALIASES.pop(alias, None)
        for alias in aliases:
            cleaned = (alias or "").strip().lower()
            if cleaned and cleaned != canonical:
                _ALIASES[cleaned] = canonical


def get_search_provider(name: str) -> SearchProviderProtocol:
    """Retrieve a registered search provider by canonical name or alias."""
    key = (name or "").strip().lower()
    if not key:
        raise UnsupportedSearchSourceError("Search source name cannot be empty.")
    with _REGISTRY_LOCK:
        canonical = _ALIASES.get(key, key)
        provider = _PROVIDERS.get(canonical)
    if provider is None:
        raise UnsupportedSearchSourceError(f"Unsupported search source: {name}")
    return provider


def list_search_sources() -> List[Dict[str, Any]]:
    """Return metadata and configuration status for all registered search providers."""
    with _REGISTRY_LOCK:
        providers_snapshot = dict(_PROVIDERS)
        aliases_by_target: Dict[str, List[str]] = {}
        for alias, target in _ALIASES.items():
            aliases_by_target.setdefault(target, []).append(alias)

    ordered_ids: List[str] = [
        source_id for source_id in SUPPORTED_SEARCH_SOURCES if source_id in providers_snapshot
    ]
    for source_id in providers_snapshot:
        if source_id not in ordered_ids:
            ordered_ids.append(source_id)

    sources: List[Dict[str, Any]] = []
    for source_id in ordered_ids:
        provider = providers_snapshot[source_id]
        configured = bool(provider.is_configured()) if hasattr(provider, "is_configured") else True
        sources.append(
            {
                "id": source_id,
                "name": getattr(provider, "display_name", "") or source_id,
                "requires_auth": bool(getattr(provider, "requires_auth", False)),
                "configured": configured,
                "description": str(getattr(provider, "description", "")),
                "aliases": sorted(aliases_by_target.get(source_id, ())),
            }
        )
    return sources


def _resolve_providers(sources: Union[str, Sequence[str]]) -> List[Tuple[str, SearchProviderProtocol]]:
    """Resolve source specifiers ('all', single string, comma-separated, or list) into providers."""
    if isinstance(sources, str):
        raw_tokens = [tok.strip().lower() for tok in sources.split(",") if tok.strip()]
        if not raw_tokens:
            raise UnsupportedSearchSourceError("Search source name cannot be empty.")
    else:
        raw_tokens = [str(tok).strip().lower() for tok in sources if str(tok).strip()]
        if not raw_tokens:
            return []

    resolved: List[Tuple[str, SearchProviderProtocol]] = []
    seen_ids: set[str] = set()

    with _REGISTRY_LOCK:
        providers_snapshot = dict(_PROVIDERS)
        aliases_snapshot = dict(_ALIASES)

    ordered_all_ids = [
        source_id for source_id in SUPPORTED_SEARCH_SOURCES if source_id in providers_snapshot
    ] + [source_id for source_id in providers_snapshot if source_id not in SUPPORTED_SEARCH_SOURCES]

    for token in raw_tokens:
        if token == "all":
            for source_id in ordered_all_ids:
                provider = providers_snapshot[source_id]
                configured = (
                    bool(provider.is_configured()) if hasattr(provider, "is_configured") else True
                )
                if configured and source_id not in seen_ids:
                    seen_ids.add(source_id)
                    resolved.append((source_id, provider))
            continue

        canonical = aliases_snapshot.get(token, token)
        provider = providers_snapshot.get(canonical)
        if provider is None:
            raise UnsupportedSearchSourceError(f"Unsupported search source: {token}")
        if canonical not in seen_ids:
            seen_ids.add(canonical)
            resolved.append((canonical, provider))

    return resolved


def _invoke_provider_search(
    provider: SearchProviderProtocol,
    query: str,
    limit: int,
    search_type: str,
    http_client: Optional[httpx.Client],
) -> List[SearchResult]:
    """Call provider.search, tolerating custom providers that omit the optional client parameter."""
    if http_client is not None:
        return provider.search(query, limit=limit, search_type=search_type, client=http_client)
    try:
        sig = inspect.signature(provider.search)
        accepts_client = "client" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_client = True

    if accepts_client:
        return provider.search(query, limit=limit, search_type=search_type, client=None)
    return provider.search(query, limit=limit, search_type=search_type)


def _deduplicate_results(results: Sequence[SearchResult]) -> List[SearchResult]:
    """Deduplicate search results by normalized ISRC or normalized (artist, title)."""
    deduped: List[SearchResult] = []
    seen_isrcs: set[str] = set()
    seen_tracks: set[Tuple[str, str]] = set()
    seen_ids: set[Tuple[str, str]] = set()

    for item in results:
        norm_isrc = re.sub(r"[^a-z0-9]", "", item.isrc.strip().lower()) if item.isrc else ""
        norm_artist = normalize_text(item.artist)
        norm_title = normalize_text(item.title)
        has_meta = bool(norm_artist and norm_title)
        track_key = (norm_artist, norm_title)
        id_key = (item.source, str(item.track_id))

        if norm_isrc and norm_isrc in seen_isrcs:
            continue
        if has_meta and track_key in seen_tracks:
            continue
        if not has_meta and item.track_id and id_key in seen_ids:
            continue

        deduped.append(item)
        if norm_isrc:
            seen_isrcs.add(norm_isrc)
        if has_meta:
            seen_tracks.add(track_key)
        if item.track_id:
            seen_ids.add(id_key)

    return deduped


def search_tracks_with_errors(
    query: str,
    sources: Union[str, Sequence[str]] = "deezer",
    limit: int = 10,
    search_type: str = "track",
    deduplicate: bool = False,
    timeout: float = 10.0,
    http_client: Optional[httpx.Client] = None,
    raise_on_error: bool = False,
) -> Tuple[List[SearchResult], Dict[str, str]]:
    """Search one or more sources and return (results, per_source_errors).

    When sources='all' (or multiple sources are supplied) and http_client is None,
    configured providers are queried concurrently using a ThreadPoolExecutor.
    When an explicit http_client is provided, providers execute sequentially so a
    single shared client or MockTransport can be reused safely.
    """
    providers = _resolve_providers(sources)
    if not providers or not (query or "").strip() or limit <= 0:
        return [], {}

    def _run_one(source_id: str, provider: SearchProviderProtocol) -> List[SearchResult]:
        if http_client is None and isinstance(provider, _BaseHttpSearchProvider):
            with httpx.Client(timeout=timeout, follow_redirects=True) as per_worker_client:
                return _invoke_provider_search(
                    provider, query, limit, search_type, per_worker_client
                )
        return _invoke_provider_search(provider, query, limit, search_type, http_client)

    collected: List[SearchResult] = []
    errors: Dict[str, str] = {}

    def _collect_result(source_id: str, fetch_fn: Any) -> None:
        try:
            collected.extend(fetch_fn())
        except SearchProviderError as exc:
            if raise_on_error:
                raise
            errors[source_id] = str(exc)
        except Exception as exc:
            if raise_on_error:
                raise SearchProviderError(
                    f"{source_id} search failed: {exc}", provider=source_id
                ) from exc
            errors[source_id] = str(exc)

    if len(providers) > 1 and http_client is None:
        with ThreadPoolExecutor(max_workers=min(len(providers), 6)) as executor:
            futures = [
                (source_id, executor.submit(_run_one, source_id, provider))
                for source_id, provider in providers
            ]
            for source_id, future in futures:
                _collect_result(source_id, future.result)
    else:
        for source_id, provider in providers:
            _collect_result(
                source_id,
                lambda sid=source_id, prov=provider: _run_one(sid, prov),
            )

    if deduplicate:
        collected = _deduplicate_results(collected)
    return collected, errors


def search_tracks(
    query: str,
    sources: Union[str, Sequence[str]] = "deezer",
    limit: int = 10,
    search_type: str = "track",
    deduplicate: bool = False,
    timeout: float = 10.0,
    http_client: Optional[httpx.Client] = None,
    raise_on_error: bool = False,
) -> List[SearchResult]:
    """Search one or more providers and return normalized SearchResult objects."""
    results, _ = search_tracks_with_errors(
        query=query,
        sources=sources,
        limit=limit,
        search_type=search_type,
        deduplicate=deduplicate,
        timeout=timeout,
        http_client=http_client,
        raise_on_error=raise_on_error,
    )
    return results


def search_results_to_csv(results: Sequence[SearchResult]) -> str:
    """Serialize SearchResult items into an RFC 4180 CSV string."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["url", "artist", "title", "album", "source", "isrc", "duration_seconds"])
    for item in results:
        writer.writerow(
            [
                item.url or "",
                item.artist or "",
                item.title or "",
                item.album or "",
                item.source or "",
                item.isrc or "",
                "" if item.duration_seconds is None else str(item.duration_seconds),
            ]
        )
    return buf.getvalue()


def _register_builtin_providers() -> None:
    register_search_provider("deezer", DeezerSearchProvider())
    register_search_provider("tidal", TidalSearchProvider())
    register_search_provider("itunes", ItunesSearchProvider(), aliases=("apple",))
    register_search_provider("musicbrainz", MusicBrainzSearchProvider())
    register_search_provider("qobuz", QobuzSearchProvider())
    register_search_provider("spotify", SpotifySearchProvider())


_register_builtin_providers()
