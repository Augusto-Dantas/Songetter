"""Offline tests for Songetter Hybrid Android bridge, Chaquopy facades, and APK scaffolding."""

from __future__ import annotations

import json
import struct
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import httpx
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from songetter import android_bridge, search as search_mod
from songetter.android_bridge import (
    bridge_get_status,
    bridge_list_library,
    bridge_organize_library,
    bridge_search,
    bridge_start_job,
    embed_basic_tags,
    embed_lyrics_in_audio,
    fetch_lyrics_lrclib,
    get_default_android_library_dir,
    resolve_spotify_url_pure_python,
    run_on_device_job,
    run_remote_server_job,
)
from songetter.deduplicator import extract_metadata
from songetter.search import SearchResult, get_search_provider, register_search_provider
from tests.e2e.conftest import (
    MockAudioServer,
    create_synthetic_audio,
    generate_minimal_mp3_frames,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mock_audio_server():
    server = MockAudioServer()
    try:
        yield server
    finally:
        server.shutdown()


def _create_minimal_m4a(path: Path) -> Path:
    def _box(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + tag + payload

    ftyp = _box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom")
    mvhd = _box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, 1000) + b"\x00" * 80)
    moov = _box(b"moov", mvhd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ftyp + moov)
    return path


def _create_ogg_or_opus(path: Path, *, opus: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 48000 if opus else 44100
    subtype = "OPUS" if opus else "VORBIS"
    samples = np.zeros(sr // 5, dtype=np.float32)
    sf.write(str(path), samples, sr, format="OGG", subtype=subtype)
    return path


def _assert_no_lrc_files(root: Path) -> None:
    lrcs = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".lrc"]
    assert lrcs == [], f"Unexpected .lrc sidecar files found: {lrcs}"


# ---------------------------------------------------------------------------
# 1. Default Android Library Directory Resolution
# ---------------------------------------------------------------------------


def test_default_android_library_dir_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom_env = tmp_path / "env_lib"
    fake_shared_music = tmp_path / "emulated_music"
    fake_shared_music.mkdir()
    android_stg = tmp_path / "android_stg"
    android_stg.mkdir()
    ext_stg = tmp_path / "ext_stg"
    ext_stg.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setattr(android_bridge, "_ANDROID_SHARED_MUSIC_DIR", fake_shared_music)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    # 1. SONGETTER_LIBRARY_DIR wins
    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(custom_env))
    monkeypatch.setenv("ANDROID_STORAGE", str(android_stg))
    monkeypatch.setenv("EXTERNAL_STORAGE", str(ext_stg))
    assert get_default_android_library_dir() == custom_env

    # 2. Shared /storage/emulated/0/Music/Songetter when writable
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    assert get_default_android_library_dir() == fake_shared_music / "Songetter"

    # 3. ANDROID_STORAGE when shared Music doesn't exist
    monkeypatch.setattr(android_bridge, "_ANDROID_SHARED_MUSIC_DIR", tmp_path / "missing_shared")
    assert get_default_android_library_dir() == android_stg / "Music" / "Songetter"

    # 4. EXTERNAL_STORAGE when ANDROID_STORAGE is unset or not writable
    monkeypatch.delenv("ANDROID_STORAGE", raising=False)
    assert get_default_android_library_dir() == ext_stg / "Music" / "Songetter"

    # 5. Home fallback
    monkeypatch.delenv("EXTERNAL_STORAGE", raising=False)
    assert get_default_android_library_dir() == fake_home / "Music" / "Songetter"


# ---------------------------------------------------------------------------
# 2. Pure-Python Spotify URL Resolution
# ---------------------------------------------------------------------------


def test_resolve_spotify_track_via_embed_next_data() -> None:
    next_data = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "type": "track",
                            "name": "Bohemian Rhapsody",
                            "uri": "spotify:track:4u7EnebtmKWzUH433cf5Qv",
                            "duration": 354320,
                            "artists": [{"name": "Queen"}],
                            "external_ids": {"isrc": "GBUM71029604"},
                        }
                    }
                }
            }
        }
    }
    embed_html = f'<html><head><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/embed/track/4u7EnebtmKWzUH433cf5Qv":
            return httpx.Response(200, text=embed_html, headers={"content-type": "text/html"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = resolve_spotify_url_pure_python(
            "https://open.spotify.com/intl-pt/track/4u7EnebtmKWzUH433cf5Qv?si=abc",
            client=client,
        )

    assert len(records) == 1
    rec = records[0]
    assert rec.url == "https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv"
    assert rec.artist == "Queen"
    assert rec.title == "Bohemian Rhapsody"
    assert rec.raw_row["duration_ms"] == "354320"
    assert rec.raw_row["isrc"] == "GBUM71029604"


def test_resolve_spotify_playlist_tracklist_and_oembed_fallback() -> None:
    playlist_data = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "type": "playlist",
                            "name": "Favorites",
                            "trackList": [
                                {
                                    "uri": "spotify:track:111AAA",
                                    "title": "Song Alpha",
                                    "subtitle": "Artist One,\xa0Artist Two",
                                    "duration": 210000,
                                    "isrc": "USRC11111111",
                                },
                                {
                                    "title": "Song Beta",
                                    "subtitle": "Artist Three",
                                    "duration": 180000,
                                },
                            ],
                        }
                    }
                }
            }
        }
    }
    playlist_html = f'<script id="__NEXT_DATA__">{json.dumps(playlist_data)}</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/embed/playlist/PL123":
            return httpx.Response(200, text=playlist_html)
        if request.url.path == "/embed/track/TRK999":
            return httpx.Response(404, text="Not Found")
        if request.url.path == "/oembed":
            return httpx.Response(
                200,
                json={
                    "title": "Midnight City",
                    "author_name": "M83",
                    "provider_name": "Spotify",
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pl_records = resolve_spotify_url_pure_python(
            "https://open.spotify.com/playlist/PL123", client=client
        )
        assert len(pl_records) == 2
        assert pl_records[0].url == "https://open.spotify.com/track/111AAA"
        assert pl_records[0].artist == "Artist One, Artist Two"
        assert pl_records[0].title == "Song Alpha"
        assert pl_records[1].url != pl_records[0].url
        assert pl_records[1].artist == "Artist Three"

        oembed_records = resolve_spotify_url_pure_python(
            "https://open.spotify.com/track/TRK999", client=client
        )
        assert len(oembed_records) == 1
        assert oembed_records[0].artist == "M83"
        assert oembed_records[0].title == "Midnight City"


def test_resolve_spotify_url_rejects_invalid_and_handles_http_errors() -> None:
    with pytest.raises(ValueError):
        resolve_spotify_url_pure_python("https://example.com/not-spotify")

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    with httpx.Client(transport=httpx.MockTransport(failing_handler)) as client:
        with pytest.raises(RuntimeError):
            resolve_spotify_url_pure_python("https://open.spotify.com/track/FAIL123", client=client)


# ---------------------------------------------------------------------------
# 3. LRCLIB Lyrics Lookup & Metadata-Only Mutagen Tagging
# ---------------------------------------------------------------------------


def test_fetch_lyrics_lrclib_get_and_search_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get":
            if request.url.params.get("track_name") == "Synced Track":
                return httpx.Response(
                    200,
                    json={
                        "syncedLyrics": "[00:01.00] Hello world",
                        "plainLyrics": "Hello world",
                    },
                )
            return httpx.Response(404)
        if request.url.path == "/api/search":
            if request.url.params.get("track_name") == "Fallback Track":
                return httpx.Response(
                    200,
                    json=[
                        {"duration": 100, "plainLyrics": "Wrong duration"},
                        {"duration": 201, "plainLyrics": "Matched plain lyrics"},
                    ],
                )
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        synced = fetch_lyrics_lrclib("Queen", "Synced Track", duration_seconds=200.0, client=client)
        assert synced == "[00:01.00] Hello world"

        fallback = fetch_lyrics_lrclib("Queen", "Fallback Track", duration_seconds=200.0, client=client)
        assert fallback == "Matched plain lyrics"

        missing = fetch_lyrics_lrclib("Queen", "Unknown Song", client=client)
        assert missing is None

        blank = fetch_lyrics_lrclib("", "Song", client=client)
        assert blank is None


def test_embed_lyrics_and_basic_tags_all_formats_zero_lrc(tmp_path: Path) -> None:
    lyrics = "[00:05.00] Line one\n[00:10.00] Line two"

    # MP3
    mp3_path = tmp_path / "raw.mp3"
    mp3_path.write_bytes(generate_minimal_mp3_frames(bitrate_kbps=128, duration_sec=0.5))
    assert embed_basic_tags(mp3_path, "Artist MP3", "Title MP3") is True
    assert embed_lyrics_in_audio(mp3_path, lyrics) is True
    mp3_tags = ID3(str(mp3_path))
    assert [f.text for f in mp3_tags.getall("USLT")] == [lyrics]
    assert extract_metadata(mp3_path).artist == "Artist MP3"
    assert extract_metadata(mp3_path).title == "Title MP3"

    # WAV
    wav_path = create_synthetic_audio(tmp_path / "song.wav", format="wav", artist="A", title="T")
    assert embed_basic_tags(wav_path, "Artist WAV", "Title WAV") is True
    assert embed_lyrics_in_audio(wav_path, lyrics) is True
    wav_tags = WAVE(str(wav_path)).tags
    assert wav_tags is not None
    assert [f.text for f in wav_tags.getall("USLT")] == [lyrics]
    assert extract_metadata(wav_path).artist == "Artist WAV"

    # FLAC
    flac_path = create_synthetic_audio(tmp_path / "song.flac", format="flac", artist="A", title="T")
    assert embed_basic_tags(flac_path, "Artist FLAC", "Title FLAC") is True
    assert embed_lyrics_in_audio(flac_path, lyrics) is True
    assert FLAC(str(flac_path)).get("lyrics") == [lyrics]
    assert extract_metadata(flac_path).title == "Title FLAC"

    # OGG
    ogg_path = _create_ogg_or_opus(tmp_path / "song.ogg", opus=False)
    assert embed_basic_tags(ogg_path, "Artist OGG", "Title OGG") is True
    assert embed_lyrics_in_audio(ogg_path, lyrics) is True
    assert OggVorbis(str(ogg_path)).get("lyrics") == [lyrics]

    # OPUS
    opus_path = _create_ogg_or_opus(tmp_path / "song.opus", opus=True)
    assert embed_basic_tags(opus_path, "Artist OPUS", "Title OPUS") is True
    assert embed_lyrics_in_audio(opus_path, lyrics) is True
    assert OggOpus(str(opus_path)).get("lyrics") == [lyrics]

    # M4A
    m4a_path = _create_minimal_m4a(tmp_path / "song.m4a")
    assert embed_basic_tags(m4a_path, "Artist M4A", "Title M4A") is True
    assert embed_lyrics_in_audio(m4a_path, lyrics) is True
    m4a_tags = MP4(str(m4a_path)).tags
    assert m4a_tags is not None
    assert m4a_tags.get("\xa9lyr") == [lyrics]
    assert m4a_tags.get("\xa9ART") == ["Artist M4A"]
    assert m4a_tags.get("\xa9nam") == ["Title M4A"]

    # Safety on invalid inputs
    assert embed_lyrics_in_audio(tmp_path / "missing.mp3", lyrics) is False
    assert embed_lyrics_in_audio(mp3_path, "   ") is False
    bad_file = tmp_path / "corrupt.mp3"
    bad_file.write_bytes(b"not an audio file")
    assert embed_basic_tags(bad_file, "A", "B") in (True, False)
    assert embed_basic_tags(mp3_path, "", "") is False

    _assert_no_lrc_files(tmp_path)


# ---------------------------------------------------------------------------
# 4. On-Device Download Job (CSV + Spotify + yt-dlp + LRCLIB + Dedup + Flatten)
# ---------------------------------------------------------------------------


def test_run_on_device_job_spotify_ytdlp_lrclib_and_skip_check(
    tmp_path: Path,
    mock_audio_server: MockAudioServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib_dir = tmp_path / "android_music"
    lib_dir.mkdir()

    # Pre-existing track that must be skipped with zero audio server hits
    create_synthetic_audio(
        lib_dir / "Existing Artist - Existing Song.mp3",
        format="mp3",
        artist="Existing Artist",
        title="Existing Song",
        bitrate_kbps=192,
    )

    # Serve raw MP3 audio for the new Spotify-resolved track
    new_mp3_bytes = generate_minimal_mp3_frames(bitrate_kbps=256, duration_sec=0.5)
    stream_url = mock_audio_server.serve_bytes("/audio/new_track.mp3", new_mp3_bytes, "audio/mpeg")
    existing_url = mock_audio_server.serve_bytes("/audio/existing.mp3", new_mp3_bytes, "audio/mpeg")

    # Mock Spotify embed + LRCLIB via injected httpx client
    spotify_next = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "type": "track",
                            "name": "Fresh Song",
                            "uri": "spotify:track:FRESH123",
                            "duration": 180000,
                            "artists": [{"name": "Fresh Artist"}],
                        }
                    }
                }
            }
        }
    }

    def http_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "open.spotify.com" and request.url.path == "/embed/track/FRESH123":
            html_body = f'<script id="__NEXT_DATA__">{json.dumps(spotify_next)}</script>'
            return httpx.Response(200, text=html_body)
        if request.url.host == "lrclib.net" and request.url.path == "/api/get":
            return httpx.Response(200, json={"syncedLyrics": "[00:02.00] Fresh lyrics"})
        return httpx.Response(404)

    # Mock pure-Python yt_dlp module
    fake_ytdlp = types.ModuleType("yt_dlp")

    class _FakeYoutubeDL:
        def __init__(self, opts: Dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> "_FakeYoutubeDL":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, target: str, download: bool = False) -> Dict[str, Any]:
            assert "Fresh Artist - Fresh Song" in target
            return {"entries": [{"url": stream_url}]}

    fake_ytdlp.YoutubeDL = _FakeYoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_ytdlp)
    monkeypatch.setattr(android_bridge.shutil, "which", lambda name: None)

    csv_payload = (
        "url,artist,title\n"
        f"{existing_url},Existing Artist,Existing Song\n"
        "https://open.spotify.com/track/FRESH123,,\n"
    )

    with httpx.Client(transport=httpx.MockTransport(http_handler)) as client:
        result = run_on_device_job(
            csv_payload,
            library_dir=lib_dir,
            is_csv=True,
            embed_lyrics=True,
            client=client,
        )

    # Verify JSON-serializable output
    serialized = json.dumps(result)
    assert serialized

    assert result["status"] == "completed"
    assert result["mode"] == "on_device"
    assert result["downloaded"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["lyrics_embedded"] == 1

    # Existing track caused 0 HTTP requests
    assert mock_audio_server.get_request_count("/audio/existing.mp3") == 0
    assert mock_audio_server.get_request_count("/audio/new_track.mp3") == 1

    expected_file = lib_dir / "Fresh Artist - Fresh Song.mp3"
    assert expected_file.exists()
    tags = ID3(str(expected_file))
    assert [f.text for f in tags.getall("USLT")] == ["[00:02.00] Fresh lyrics"]
    meta = extract_metadata(expected_file)
    assert meta.artist == "Fresh Artist"
    assert meta.title == "Fresh Song"
    _assert_no_lrc_files(lib_dir)


# ---------------------------------------------------------------------------
# 5. Remote Server Sync Job
# ---------------------------------------------------------------------------


def test_run_remote_server_job_skips_local_and_streams_atomic(tmp_path: Path) -> None:
    lib_dir = tmp_path / "local_lib"
    lib_dir.mkdir()

    # Pre-existing local track
    create_synthetic_audio(
        lib_dir / "Daft Punk - Harder Better.mp3",
        format="mp3",
        artist="Daft Punk",
        title="Harder Better",
        bitrate_kbps=192,
    )

    # Remote audio payload (tagged synthetic FLAC)
    remote_flac_src = create_synthetic_audio(
        tmp_path / "remote_staging" / "New Artist - New Track.flac",
        format="flac",
        artist="New Artist",
        title="New Track",
        bitrate_kbps=900,
    )
    remote_flac_bytes = remote_flac_src.read_bytes()

    posted_jobs: List[Dict[str, Any]] = []
    poll_count = 0

    def remote_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST" and request.url.path == "/api/jobs":
            body = json.loads(request.content.decode("utf-8"))
            posted_jobs.append(body)
            return httpx.Response(200, json={"job_id": "job-42", "status": "queued"})
        if request.method == "GET" and request.url.path == "/api/jobs/job-42":
            poll_count += 1
            return httpx.Response(200, json={"job_id": "job-42", "status": "completed"})
        if request.method == "GET" and request.url.path == "/api/library/tracks":
            return httpx.Response(
                200,
                json={
                    "library_dir": "/server/music",
                    "tracks": [
                        {
                            "filename": "Daft Punk - Harder Better.mp3",
                            "artist": "Daft Punk",
                            "title": "Harder Better",
                        },
                        {
                            "filename": "../traversal_attempt.mp3",
                            "artist": "Evil",
                            "title": "Hack",
                        },
                        {
                            "filename": "New Artist - New Track.flac",
                            "artist": "New Artist",
                            "title": "New Track",
                        },
                    ],
                },
            )
        if (
            request.method == "GET"
            and request.url.path == "/api/library/tracks/New Artist - New Track.flac/stream"
        ):
            return httpx.Response(
                200,
                content=remote_flac_bytes,
                headers={
                    "content-type": "audio/flac",
                    "content-length": str(len(remote_flac_bytes)),
                },
            )
        return httpx.Response(404)

    csv_input = (
        "url,artist,title\n"
        "https://open.spotify.com/track/LOCAL1,Daft Punk,Harder Better\n"
        "https://open.spotify.com/track/NEW1,New Artist,New Track\n"
    )

    with httpx.Client(transport=httpx.MockTransport(remote_handler)) as client:
        # 1. All-local batch skips remote POST completely
        all_local = run_remote_server_job(
            "url,artist,title\nhttps://open.spotify.com/track/LOCAL1,Daft Punk,Harder Better\n",
            remote_base_url="http://127.0.0.1:8000",
            library_dir=lib_dir,
            is_csv=True,
            client=client,
        )
        assert all_local["skipped"] == 1
        assert all_local["downloaded"] == 0
        assert len(posted_jobs) == 0

        # 2. Mixed batch submits only NEW1, streams New Track, ignores traversal
        res = run_remote_server_job(
            csv_input,
            remote_base_url="http://127.0.0.1:8000",
            library_dir=lib_dir,
            is_csv=True,
            client=client,
        )

    assert len(posted_jobs) == 1
    submitted_tracks = posted_jobs[0]["tracks"]
    assert len(submitted_tracks) == 1
    assert submitted_tracks[0]["title"] == "New Track"
    assert poll_count == 1

    assert res["status"] == "completed"
    assert res["mode"] == "remote"
    assert res["skipped"] == 1
    assert res["downloaded"] == 1
    assert res["failed"] == 0
    assert (lib_dir / "New Artist - New Track.flac").exists()
    assert not (tmp_path / "traversal_attempt.mp3").exists()
    assert [p for p in lib_dir.rglob("*.part")] == []
    _assert_no_lrc_files(lib_dir)


# ---------------------------------------------------------------------------
# 6. Chaquopy JSON Facade Functions
# ---------------------------------------------------------------------------


def test_chaquopy_json_facades_and_error_isolation(
    tmp_path: Path,
    mock_audio_server: MockAudioServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib_dir = tmp_path / "facade_lib"
    nested = lib_dir / "SubFolder"
    nested.mkdir(parents=True)

    # Create duplicate pair with different bitrates inside nested dir
    create_synthetic_audio(
        nested / "low.mp3",
        format="mp3",
        artist="Queen",
        title="Killer Queen",
        bitrate_kbps=128,
    )
    create_synthetic_audio(
        lib_dir / "high.mp3",
        format="mp3",
        artist="Queen",
        title="Killer Queen",
        bitrate_kbps=320,
    )

    status = json.loads(bridge_get_status(str(lib_dir)))
    assert status["ok"] is True
    assert status["library_writable"] is True
    assert status["track_count"] == 2

    listed = json.loads(bridge_list_library(str(lib_dir)))
    assert listed["ok"] is True
    assert listed["count"] == 2

    organized = json.loads(bridge_organize_library(str(lib_dir)))
    assert organized["ok"] is True
    assert organized["duplicates_count"] >= 1
    assert organized["organized_count"] == 1
    assert (lib_dir / "Queen - Killer Queen.mp3").exists()
    assert not nested.exists()

    # bridge_start_job on-device HTTP download
    mp3_payload = create_synthetic_audio(
        tmp_path / "src.mp3", format="mp3", artist="Blur", title="Song 2", bitrate_kbps=192
    ).read_bytes()
    url = mock_audio_server.serve_bytes("/Blur%20-%20Song%202.mp3", mp3_payload, "audio/mpeg")
    started = json.loads(bridge_start_job(url, library_dir=str(lib_dir), is_csv=False, mode="on_device"))
    assert started["ok"] is True
    assert started["downloaded"] == 1
    assert (lib_dir / "Blur - Song 2.mp3").exists()

    # bridge_search with temporary mock provider
    class _FakeSearchProvider:
        name = "_test_android_mock"
        display_name = "Test Mock"
        requires_auth = False
        description = "Offline test provider"

        def is_configured(self) -> bool:
            return True

        def search(self, query: str, limit: int = 10, search_type: str = "track", client=None):
            return [
                SearchResult(
                    source="_test_android_mock",
                    track_id="42",
                    title="One More Time",
                    artist="Daft Punk",
                    album="Discovery",
                    duration_seconds=320,
                    url="https://open.spotify.com/track/42",
                )
            ]

    monkeypatch.setitem(search_mod._PROVIDERS, "_test_android_mock", _FakeSearchProvider())
    search_resp = json.loads(bridge_search("One More Time", sources_csv="_test_android_mock", limit=5))
    assert search_resp["ok"] is True
    assert search_resp["count"] == 1
    assert search_resp["results"][0]["artist"] == "Daft Punk"

    # Crash safety on invalid inputs
    bad_job = json.loads(bridge_start_job("", library_dir=str(lib_dir)))
    assert bad_job["ok"] is False
    assert bad_job["error"]

    bad_search = json.loads(bridge_search("", sources_csv="_test_android_mock"))
    assert bad_search["ok"] is False
    assert bad_search["error"]

    bad_unknown_source = json.loads(bridge_search("hello", sources_csv="nonexistent_source_xyz"))
    assert bad_unknown_source["ok"] is False

    bad_org = json.loads(bridge_organize_library("/nonexistent_invalid_root_xyz/path"))
    assert isinstance(bad_org, dict)
    _assert_no_lrc_files(lib_dir)


# ---------------------------------------------------------------------------
# 7. Android Gradle / Kotlin / Chaquopy / Docker / CI Project Integrity
# ---------------------------------------------------------------------------


def test_android_project_ci_and_lrc_policy_integrity() -> None:
    required_files = [
        "src/songetter/android_bridge.py",
        "android/settings.gradle.kts",
        "android/build.gradle.kts",
        "android/gradle.properties",
        "android/gradle/wrapper/gradle-wrapper.properties",
        "android/app/build.gradle.kts",
        "android/app/src/main/AndroidManifest.xml",
        "android/app/src/main/java/com/songetter/app/PythonBridge.kt",
        "android/app/src/main/java/com/songetter/app/MainActivity.kt",
        "Dockerfile.android",
        ".github/workflows/android-apk.yml",
    ]
    for rel in required_files:
        path = REPO_ROOT / rel
        assert path.is_file(), f"Missing required Android artifact: {rel}"

    settings_text = (REPO_ROOT / "android/settings.gradle.kts").read_text(encoding="utf-8")
    assert "https://chaquo.com/maven" in settings_text

    root_gradle = (REPO_ROOT / "android/build.gradle.kts").read_text(encoding="utf-8")
    assert "8.5.2" in root_gradle
    assert "1.9.24" in root_gradle
    assert "15.0.1" in root_gradle

    app_gradle = (REPO_ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    for needle in (
        "compileSdk = 34",
        "minSdk = 26",
        "targetSdk = 34",
        "arm64-v8a",
        "x86_64",
        'version = "3.11"',
        "mutagen>=1.47",
        "httpx>=0.27",
        "yt-dlp",
        'srcDir("../../src")',
    ):
        assert needle in app_gradle, f"Missing {needle!r} in android/app/build.gradle.kts"

    manifest = (REPO_ROOT / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    for perm in (
        "android.permission.INTERNET",
        "android.permission.ACCESS_NETWORK_STATE",
        "android.permission.READ_MEDIA_AUDIO",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
        "android.permission.MANAGE_EXTERNAL_STORAGE",
        ".MainActivity",
    ):
        assert perm in manifest

    kt_bridge = (REPO_ROOT / "android/app/src/main/java/com/songetter/app/PythonBridge.kt").read_text(
        encoding="utf-8"
    )
    for fn_name in (
        "bridge_get_status",
        "bridge_list_library",
        "bridge_start_job",
        "bridge_search",
        "bridge_organize_library",
    ):
        assert fn_name in kt_bridge

    kt_main = (REPO_ROOT / "android/app/src/main/java/com/songetter/app/MainActivity.kt").read_text(
        encoding="utf-8"
    )
    for ui_label in ("Download", "Search", "Library", "Settings"):
        assert ui_label in kt_main

    dockerfile_android = (REPO_ROOT / "Dockerfile.android").read_text(encoding="utf-8")
    assert "eclipse-temurin:17" in dockerfile_android
    assert "android-34" in dockerfile_android
    assert "python3.11" in dockerfile_android
    assert "assembleDebug" in dockerfile_android

    ci_yml = (REPO_ROOT / ".github/workflows/android-apk.yml").read_text(encoding="utf-8")
    assert "assembleDebug" in ci_yml
    assert "upload-artifact" in ci_yml
    assert "test_android_bridge.py" in ci_yml

    # Ensure zero .lrc sidecar logic in android_bridge.py
    bridge_src = (REPO_ROOT / "src/songetter/android_bridge.py").read_text(encoding="utf-8")
    assert "--save-lrc" not in bridge_src
    assert '".lrc"' not in bridge_src and "'.lrc'" not in bridge_src



def test_resolve_spotify_shortlink_and_opengraph_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "spotify.link" and request.url.path == "/short1":
            return httpx.Response(
                302,
                headers={"Location": "https://open.spotify.com/track/OGTRACK1"},
            )
        if request.url.host == "open.spotify.com" and request.url.path == "/embed/track/OGTRACK1":
            return httpx.Response(
                200,
                text=(
                    '<html><head>'
                    '<meta property="og:title" content="Starlight - song and lyrics by Muse | Spotify">'
                    "</head></html>"
                ),
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        records = resolve_spotify_url_pure_python("https://spotify.link/short1", client=client)

    assert len(records) == 1
    assert records[0].url == "https://open.spotify.com/track/OGTRACK1"
    assert records[0].artist == "Muse"
    assert records[0].title == "Starlight"


def test_remote_server_job_with_real_fastapi_and_truncated_stream_cleanup(
    tmp_path: Path,
    mock_audio_server: MockAudioServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient
    from songetter.web.app import create_app

    server_lib = tmp_path / "server_lib"
    server_lib.mkdir()
    android_lib = tmp_path / "android_lib"
    android_lib.mkdir()

    # Serve a valid tagged MP3 via MockAudioServer for the FastAPI server to download
    mp3_bytes = create_synthetic_audio(
        tmp_path / "fixture.mp3",
        format="mp3",
        artist="Phoenix",
        title="1901",
        bitrate_kbps=256,
    ).read_bytes()
    track_url = mock_audio_server.serve_bytes("/phoenix_1901.mp3", mp3_bytes, "audio/mpeg")

    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(server_lib))
    app = create_app()
    real_httpx_client = httpx.Client
    with TestClient(app) as test_client:
        def fastapi_transport_handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "songetter-remote.local":
                path_and_query = request.url.raw_path.decode("ascii")
                resp = test_client.request(
                    method=request.method,
                    url=path_and_query,
                    content=request.content,
                    headers=dict(request.headers),
                )
                return httpx.Response(
                    status_code=resp.status_code,
                    content=resp.content,
                    headers=dict(resp.headers),
                )
            with real_httpx_client() as passthrough:
                return passthrough.send(request)

        with monkeypatch.context() as mp:
            mp.setattr(
                android_bridge.httpx,
                "Client",
                lambda *args, **kwargs: real_httpx_client(
                    transport=httpx.MockTransport(fastapi_transport_handler),
                    follow_redirects=kwargs.get("follow_redirects", True),
                ),
            )
            initial_status = json.loads(
                bridge_get_status(str(android_lib), remote_url="http://songetter-remote.local")
            )
            assert initial_status["ok"] is True
            assert initial_status["remote_reachable"] is True
            assert initial_status["remote_track_count"] == 0

            res = run_remote_server_job(
                track_url,
                remote_base_url="http://songetter-remote.local",
                library_dir=android_lib,
                is_csv=False,
            )

            after_status = json.loads(
                bridge_get_status(str(android_lib), remote_url="http://songetter-remote.local")
            )
            assert after_status["ok"] is True
            assert after_status["remote_reachable"] is True
            assert after_status["remote_track_count"] == 1
            assert after_status["track_count"] == 1

    assert res["status"] == "completed"
    assert res["downloaded"] == 1
    assert res["failed"] == 0
    assert (android_lib / "Phoenix - 1901.mp3").exists()
    assert (server_lib / "Phoenix - 1901.mp3").exists()
    _assert_no_lrc_files(android_lib)

    # Also test truncated stream cleanup & .part removal
    def truncated_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs":
            return httpx.Response(200, json={"job_id": "j-trunc", "status": "completed"})
        if request.method == "GET" and request.url.path == "/api/library/tracks":
            return httpx.Response(
                200,
                json={"tracks": [{"filename": "Broken - Song.mp3", "artist": "Broken", "title": "Song"}]},
            )
        if request.method == "GET" and request.url.path.endswith("/stream"):
            return httpx.Response(
                200,
                content=b"partial",
                headers={"content-length": "50000"},
            )
        return httpx.Response(404)

    trunc_lib = tmp_path / "trunc_lib"
    trunc_lib.mkdir()
    with httpx.Client(transport=httpx.MockTransport(truncated_handler)) as trunc_client:
        trunc_res = run_remote_server_job(
            "http://127.0.0.1/broken.mp3",
            remote_base_url="http://127.0.0.1:8000",
            library_dir=trunc_lib,
            client=trunc_client,
        )

    assert trunc_res["failed"] == 1
    assert trunc_res["downloaded"] == 0
    assert not (trunc_lib / "Broken - Song.mp3").exists()
    assert [p for p in trunc_lib.rglob("*.part")] == []

    # Invalid remote URL check
    with pytest.raises(ValueError):
        run_remote_server_job("http://127.0.0.1/a.mp3", remote_base_url="ftp://invalid", library_dir=trunc_lib)
    assert json.loads(bridge_start_job("http://127.0.0.1/a.mp3", library_dir=str(trunc_lib), mode="remote", remote_url="not-a-url"))["ok"] is False

    # bridge_get_status remote reachability probe (unreachable port fails gracefully)
    status = json.loads(bridge_get_status(str(android_lib), remote_url="http://127.0.0.1:1"))
    assert status["ok"] is True
    assert status["remote_reachable"] is False
    assert status["track_count"] == 1
