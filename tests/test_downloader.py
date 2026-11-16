"""Comprehensive offline unit & integration tests for Milestone 2 (library + downloader)."""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import httpx
import pytest

from songetter.core import RunResult
from songetter.csv_processor import TrackRecord
from songetter.deduplicator import extract_metadata
from songetter.downloader import (
    Downloader,
    DownloadSummary,
    TrackDownloadResult,
    _is_mornye_url,
    deduplicate_records,
    download_tracks,
)
from songetter.library import (
    LibraryIndex,
    NotADirectoryError,
    PermissionDeniedError,
    extract_url_stem,
)
from tests.e2e.conftest import MockAudioServer, create_synthetic_audio


@pytest.fixture
def mock_audio_server():
    """Local fixture wrapping MockAudioServer for tests outside tests/e2e."""
    server = MockAudioServer()
    try:
        yield server
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# 1. URL Stem Extraction & LibraryIndex Stem Matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("Queen - Bohemian Rhapsody", "Queen - Bohemian Rhapsody"),
        ("Queen - Bohemian Rhapsody.flac", "Queen - Bohemian Rhapsody"),
        ("Queen - Bohemian Rhapsody.FLAC", "Queen - Bohemian Rhapsody"),
        (
            "http://127.0.0.1/ACDC%20-%20Back%20In%20Black.flac",
            "ACDC - Back In Black",
        ),
        (
            "https://cdn.example.com:8443/music/Beyonc%C3%A9%20-%20D%C3%A9j%C3%A0%20Vu.mp3?dl=1#top",
            "Beyoncé - Déjà Vu",
        ),
        ("http://127.0.0.1/path/to/track_without_ext", "track_without_ext"),
        ("http://127.0.0.1/Dr.%20Dre%20-%20Still%20D.R.E..mp3", "Dr. Dre - Still D.R.E."),
        ("http://127.0.0.1/", ""),
        ("http://127.0.0.1/album/", "album"),
        ("AC/DC - Back In Black", "AC/DC - Back In Black"),
        ("Florence + The Machine - Dog Days.mp3", "Florence + The Machine - Dog Days"),
        ("http://127.0.0.1/notes.txt", "notes.txt"),
    ],
)
def test_extract_url_stem_variants(raw: str, expected: str) -> None:
    assert extract_url_stem(raw) == expected


def test_library_index_contains_stem_with_encoded_urls(tmp_path: Path) -> None:
    create_synthetic_audio(
        tmp_path / "ACDC - Back In Black.flac",
        format="flac",
        artist="ACDC",
        title="Back In Black",
    )
    create_synthetic_audio(
        tmp_path / "Beyoncé - Déjà Vu.mp3",
        format="mp3",
        artist="Beyoncé",
        title="Déjà Vu",
    )

    index = LibraryIndex.build(tmp_path)
    assert index.contains_stem("http://127.0.0.1/ACDC%20-%20Back%20In%20Black.flac") is True
    assert index.contains_stem("http://127.0.0.1/acdc%20-%20back%20in%20black.mp3?v=2") is True
    assert (
        index.contains_stem("https://example.org/Beyonce%20-%20Deja%20Vu.FLAC") is True
    )
    assert index.contains_stem("http://127.0.0.1/Unknown%20-%20Track.flac") is False
    assert index.contains_stem("http://127.0.0.1/") is False


# ---------------------------------------------------------------------------
# 2. Batch URL Deduplication
# ---------------------------------------------------------------------------


def test_deduplicate_records_preserves_first_seen_order() -> None:
    r1 = TrackRecord(url="http://127.0.0.1/a.mp3", artist="A", title="One")
    r2 = TrackRecord(url="http://127.0.0.1/b.mp3", artist="B", title="Two")
    r3 = TrackRecord(url="  http://127.0.0.1/a.mp3  ", artist="A2", title="One Dup")
    r4 = TrackRecord(url="http://127.0.0.1/c.mp3", artist="C", title="Three")
    r5 = TrackRecord(url="http://127.0.0.1/b.mp3", artist="B2", title="Two Dup")

    deduped = deduplicate_records([r1, r2, r3, r4, r5])
    assert deduped == [r1, r2, r4]


def test_downloader_batch_url_deduplication_hits_server_once(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    payload = b"UNIQUE_AUDIO_BYTES" * 20
    u1 = mock_audio_server.serve_bytes("dup_song.mp3", payload)
    u2 = mock_audio_server.serve_bytes("other_song.mp3", payload)

    records = [
        TrackRecord(url=u1, artist="Band", title="First"),
        TrackRecord(url=u2, artist="Band", title="Second"),
        TrackRecord(url=f"  {u1}  ", artist="Band", title="First Duplicate"),
    ]
    summary = Downloader(tmp_path).download(records)

    assert summary.downloaded == 2
    assert summary.skipped == 0
    assert summary.failed == 0
    assert len(summary.tracks) == 2
    assert mock_audio_server.get_request_count("dup_song.mp3") == 1
    assert mock_audio_server.get_request_count("other_song.mp3") == 1


# ---------------------------------------------------------------------------
# 3. Pre-Download Skip Checks (Zero HTTP / Zero Mornye Calls)
# ---------------------------------------------------------------------------


def test_pre_download_skip_by_tags_and_url_encoded_stem(
    tmp_path: Path, mock_audio_server: MockAudioServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_synthetic_audio(
        tmp_path / "odd_local_name.flac",
        format="flac",
        artist="Pink Floyd",
        title="Time",
    )
    create_synthetic_audio(
        tmp_path / "ACDC - Back In Black.flac",
        format="flac",
        artist="ACDC",
        title="Back In Black",
    )

    u_tags = mock_audio_server.serve_bytes("pink.flac", b"DATA")
    u_stem = mock_audio_server.serve_bytes("ACDC - Back In Black.flac", b"DATA")
    encoded_u_stem = f"{mock_audio_server.base_url}/ACDC%20-%20Back%20In%20Black.flac"
    u_new = mock_audio_server.serve_bytes("new_song.mp3", b"NEW_AUDIO_DATA" * 10)

    mornye_mock = MagicMock()
    monkeypatch.setattr("songetter.core.run_download", mornye_mock)

    records = [
        TrackRecord(url=u_tags, artist="Pink Floyd", title="Time"),
        TrackRecord(url=encoded_u_stem, artist=None, title=None),
        TrackRecord(
            url="https://open.spotify.com/track/12345",
            artist="Pink Floyd",
            title="Time",
        ),
        TrackRecord(url=u_new, artist="New Artist", title="New Title"),
    ]
    summary = Downloader(tmp_path).download(records)

    assert summary.skipped == 3
    assert summary.downloaded == 1
    assert summary.failed == 0
    assert [t.status for t in summary.tracks] == ["skipped", "skipped", "skipped", "downloaded"]
    assert mock_audio_server.get_request_count("pink.flac") == 0
    assert mock_audio_server.get_request_count("ACDC") == 0
    assert mock_audio_server.get_request_count("new_song.mp3") == 1
    mornye_mock.assert_not_called()


def test_pre_download_partial_artist_without_title_does_not_skip(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    create_synthetic_audio(
        tmp_path / "Queen - Bohemian Rhapsody.mp3",
        format="mp3",
        artist="Queen",
        title="Bohemian Rhapsody",
    )
    u_other = mock_audio_server.serve_bytes("another_queen_track.mp3", b"AUDIO" * 10)
    summary = Downloader(tmp_path).download(
        [TrackRecord(url=u_other, artist="Queen", title=None)]
    )
    assert summary.downloaded == 1
    assert summary.skipped == 0
    assert mock_audio_server.get_request_count("another_queen_track.mp3") == 1


# ---------------------------------------------------------------------------
# 4. Atomic .part Staging, Exact Bytes, Naming, & Convenience Function
# ---------------------------------------------------------------------------


def test_atomic_part_staging_observed_during_transfer_and_cleaned_up(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    payload = b"RAW_AUDIO_STREAM_FRAME_" * 100
    url = mock_audio_server.serve_slow("slow_track.mp3", payload, chunk_size=150, delay_sec=0.04)

    observed_parts: List[str] = []
    stop_event = threading.Event()

    def watch_parts() -> None:
        while not stop_event.is_set():
            for p in tmp_path.glob("*.part"):
                observed_parts.append(p.name)
            time.sleep(0.01)

    watcher = threading.Thread(target=watch_parts, daemon=True)
    watcher.start()
    try:
        summary = Downloader(tmp_path).download(
            [TrackRecord(url=url, artist="Slow Artist", title="Slow Song")]
        )
    finally:
        stop_event.set()
        watcher.join(timeout=1.0)

    assert summary.downloaded == 1
    assert len(observed_parts) > 0
    assert all(name.endswith(".part") for name in observed_parts)
    assert list(tmp_path.glob("*.part")) == []

    dest = tmp_path / "Slow Artist - Slow Song.mp3"
    assert dest.is_file()
    assert dest.read_bytes() == payload
    assert summary.tracks[0].file_path == dest


def test_download_tracks_creates_missing_dir_and_preserves_bytes(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    target_dir = tmp_path / "nested" / "new_library"
    payload = b"EXACT_FLAC_PAYLOAD" * 30
    url = mock_audio_server.serve_bytes("stream_no_ext", payload, content_type="audio/flac")

    summary = download_tracks(
        [TrackRecord(url=url, artist="Exact", title="Bytes")],
        target_dir,
    )
    assert isinstance(summary, DownloadSummary)
    assert summary.downloaded == 1
    expected = target_dir / "Exact - Bytes.flac"
    assert expected.is_file()
    assert expected.read_bytes() == payload


def test_download_redirects_and_content_disposition_extension(
    tmp_path: Path,
) -> None:
    payload = b"REDIRECTED_OGG_BYTES" * 15

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/final_download")
                self.end_headers()
            elif self.path == "/final_download":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="Track.ogg"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/start"
        summary = Downloader(tmp_path).download(
            [TrackRecord(url=url, artist="Redirect", title="Test")]
        )
        assert summary.downloaded == 1
        out_file = tmp_path / "Redirect - Test.ogg"
        assert out_file.is_file()
        assert out_file.read_bytes() == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 5. Error Isolation & .part Cleanup (404, 429, Truncated, HTML, Invalid Dir)
# ---------------------------------------------------------------------------


def test_http_errors_and_truncated_stream_cleanup_part_files(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    u_404 = mock_audio_server.serve_error("missing.mp3", status_code=404)
    u_429 = mock_audio_server.serve_error("ratelimit.mp3", status_code=429, retry_after=10)
    u_cut = mock_audio_server.serve_truncated("cut.mp3", full_size=50000, cut_at_byte=400)
    u_ok = mock_audio_server.serve_bytes("valid.mp3", b"VALID_MP3_BYTES" * 20)

    summary = Downloader(tmp_path).download(
        [
            TrackRecord(url=u_404, artist="A", title="Missing"),
            TrackRecord(url=u_429, artist="B", title="RateLimited"),
            TrackRecord(url=u_cut, artist="C", title="Truncated"),
            TrackRecord(url=u_ok, artist="D", title="Valid"),
        ]
    )

    assert summary.downloaded == 1
    assert summary.failed == 3
    assert summary.skipped == 0
    assert [t.status for t in summary.tracks] == ["failed", "failed", "failed", "downloaded"]
    assert list(tmp_path.glob("*.part")) == []
    assert [p.name for p in tmp_path.iterdir()] == ["D - Valid.mp3"]


def test_html_response_localhost_fails_without_ytdlp(
    tmp_path: Path, mock_audio_server: MockAudioServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    u_html = mock_audio_server.serve_bytes(
        "page", b"<html><body>Not audio</body></html>", content_type="text/html"
    )
    which_mock = MagicMock()
    monkeypatch.setattr("songetter.downloader.shutil.which", which_mock)

    summary = Downloader(tmp_path).download(
        [TrackRecord(url=u_html, artist="Page", title="HTML")]
    )
    assert summary.failed == 1
    assert summary.downloaded == 0
    assert list(tmp_path.iterdir()) == []
    which_mock.assert_not_called()


def test_html_response_non_localhost_uses_ytdlp_fallback_or_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><body>Video page</body></html>",
        )

    transport = httpx.MockTransport(html_handler)
    orig_client = httpx.Client

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("songetter.downloader.httpx.Client", client_factory)

    # Case A: yt-dlp not available -> fails cleanly
    monkeypatch.setattr("songetter.downloader.shutil.which", lambda _: None)
    fail_summary = Downloader(tmp_path).download(
        [TrackRecord(url="https://example.com/watch?v=123", artist="YT", title="NoBin")]
    )
    assert fail_summary.failed == 1
    assert list(tmp_path.iterdir()) == []

    # Case B: yt-dlp mocked -> writes audio file without .lrc sidecars
    monkeypatch.setattr("songetter.downloader.shutil.which", lambda _: "/usr/bin/yt-dlp")

    def fake_run(cmd: List[str], **kwargs: Any) -> MagicMock:
        out_idx = cmd.index("-o") + 1
        template = cmd[out_idx]
        target = Path(template.replace("%(title)s.%(ext)s", "fallback_track.mp3"))
        target.write_bytes(b"YTDLP_MP3_DATA" * 20)
        res = MagicMock()
        res.returncode = 0
        return res

    monkeypatch.setattr("songetter.downloader.subprocess.run", fake_run)
    ok_summary = Downloader(tmp_path).download(
        [TrackRecord(url="https://example.com/watch?v=456", artist="YT", title="Fallback")]
    )
    assert ok_summary.downloaded == 1
    assert (tmp_path / "YT - Fallback.mp3").is_file()
    assert not list(tmp_path.glob("*.lrc"))
    assert not list(tmp_path.glob(".songetter_ytdlp_*"))


def test_downloader_rejects_invalid_and_readonly_directories(tmp_path: Path) -> None:
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("file", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        Downloader(file_path).download([])

    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        with pytest.raises(PermissionDeniedError):
            Downloader(ro_dir).download([])
    finally:
        ro_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# 6. Bounded Concurrency
# ---------------------------------------------------------------------------


def test_bounded_concurrency_enforced(tmp_path: Path) -> None:
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}
    payload = b"CONCURRENT_AUDIO_PAYLOAD" * 10

    class ConcurrencyHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                time.sleep(0.08)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                with lock:
                    state["active"] -= 1

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ConcurrencyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        records = [
            TrackRecord(
                url=f"http://{host}:{port}/track_{i}.mp3",
                artist="Concurrent",
                title=f"Track {i}",
            )
            for i in range(6)
        ]
        summary = Downloader(tmp_path, max_concurrent=2).download(records)
        assert summary.downloaded == 6
        assert summary.failed == 0
        assert 1 <= state["max_active"] <= 2
        assert state["max_active"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 7. Mornye Delegation Routing & Post-Processing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", True),
        ("https://spotify.link/abc123", True),
        ("spotify:track:4cOdK2wGLETKBW3PvgPWqT", True),
        ("https://www.deezer.com/track/123", True),
        ("https://listen.tidal.com/track/456", True),
        ("https://open.qobuz.com/track/789", True),
        ("https://music.apple.com/us/album/abc/123", True),
        ("https://soundcloud.com/artist/track", True),
        ("https://www.jiosaavn.com/song/abc/123", True),
        ("https://music.youtube.com/watch?v=xyz", True),
        ("https://open.spotify.com/preview/clip.mp3", False),
        ("http://127.0.0.1:8080/track", False),
        ("http://localhost:9000/spotify.com/track", False),
        ("https://example.com/song.flac", False),
        ("https://notspotify.com/track/1", False),
    ],
)
def test_mornye_url_routing_rules(url: str, expected: bool) -> None:
    assert _is_mornye_url(url) is expected


def test_mornye_delegation_flattens_nested_output_without_lrc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[tuple] = []

    def fake_run_download(
        url: str,
        output_dir: Path,
        services: list[str] | None = None,
        max_concurrent_downloads: int = 2,
    ) -> RunResult:
        calls.append((url, output_dir, services, max_concurrent_downloads))
        nested = output_dir / "Daft Punk" / "Discovery"
        nested.mkdir(parents=True, exist_ok=True)
        out = create_synthetic_audio(
            nested / "01 - One More Time.flac",
            format="flac",
            artist="Daft Punk",
            title="One More Time",
        )
        return RunResult(
            url=url,
            downloaded=1,
            skipped=0,
            failed=0,
            tracks=[{"status": "downloaded", "file_path": str(out)}],
        )

    monkeypatch.setattr("songetter.core.run_download", fake_run_download)

    summary = Downloader(
        tmp_path,
        max_concurrent=3,
        services=["ext:qobuz-web"],
    ).download(
        [
            TrackRecord(
                url="https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV",
                artist="Daft Punk",
                title="One More Time",
            ),
            TrackRecord(
                url="https://tidal.com/browse/track/999",
                artist="Other",
                title="Missing",
            ),
        ]
    )

    assert len(calls) == 2
    assert calls[0][2] == ["ext:qobuz-web"]
    assert calls[0][3] == 3
    assert summary.downloaded == 2

    flat_file = tmp_path / "Daft Punk - One More Time.flac"
    assert flat_file.is_file()
    assert not (tmp_path / "Daft Punk").exists()
    assert not list(tmp_path.rglob("*.lrc"))


def test_mornye_failure_maps_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_run_download(url: str, output_dir: Path, **kwargs: Any) -> RunResult:
        return RunResult(
            url=url,
            downloaded=0,
            skipped=0,
            failed=1,
            tracks=[],
            error="Provider cooldown",
        )

    monkeypatch.setattr("songetter.core.run_download", failing_run_download)
    summary = Downloader(tmp_path).download(
        [TrackRecord(url="https://open.spotify.com/track/xyz")]
    )
    assert summary.failed == 1
    assert summary.downloaded == 0
    assert summary.tracks[0].status == "failed"
    assert "cooldown" in (summary.tracks[0].error or "").lower()


# ---------------------------------------------------------------------------
# 8. Post-Download Quality Deduplication & Flat Organization
# ---------------------------------------------------------------------------


def test_post_download_quality_upgrade_replaces_existing_and_cleans_collision_suffix(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    existing_low = tmp_path / "Queen - Bohemian Rhapsody.mp3"
    create_synthetic_audio(
        existing_low,
        format="mp3",
        artist="Queen",
        title="Bohemian Rhapsody",
        bitrate_kbps=128,
    )

    staging = tmp_path / "_staging_high.mp3"
    high_bytes = create_synthetic_audio(
        staging,
        format="mp3",
        artist="Queen",
        title="Bohemian Rhapsody",
        bitrate_kbps=320,
    ).read_bytes()
    staging.unlink()

    url = mock_audio_server.serve_bytes("queen_hq.mp3", high_bytes)
    summary = Downloader(tmp_path, skip_existing=False).download(
        [TrackRecord(url=url, artist="Queen", title="Bohemian Rhapsody")]
    )

    assert summary.downloaded == 1
    assert len(summary.duplicates_resolved) == 1
    final_files = [p for p in tmp_path.iterdir() if p.is_file()]
    assert len(final_files) == 1
    assert final_files[0].name == "Queen - Bohemian Rhapsody.mp3"
    assert extract_metadata(final_files[0]).bitrate == 320000
    assert summary.tracks[0].file_path == final_files[0]
    assert summary.organized_files == final_files


def test_post_download_downgrade_rejected_keeps_existing_flac(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    existing_flac = tmp_path / "Master - Track.flac"
    create_synthetic_audio(
        existing_flac,
        format="flac",
        artist="Master",
        title="Track",
        bitrate_kbps=1411,
    )

    staging = tmp_path / "_staging_low.mp3"
    low_bytes = create_synthetic_audio(
        staging,
        format="mp3",
        artist="Master",
        title="Track",
        bitrate_kbps=128,
    ).read_bytes()
    staging.unlink()

    url = mock_audio_server.serve_bytes("master_low.mp3", low_bytes)
    summary = Downloader(tmp_path, skip_existing=False).download(
        [TrackRecord(url=url, artist="Master", title="Track")]
    )

    assert summary.downloaded == 1
    assert len(summary.duplicates_resolved) == 1
    final_files = [p for p in tmp_path.iterdir() if p.is_file()]
    assert [p.name for p in final_files] == ["Master - Track.flac"]


def test_post_download_concurrent_duplicate_urls_dedup_and_cleanup_collision(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    # Two distinct URLs yielding different quality versions of the same track
    s_low = tmp_path / "_low.mp3"
    low_bytes = create_synthetic_audio(
        s_low, format="mp3", artist="Daft Punk", title="Get Lucky", bitrate_kbps=128
    ).read_bytes()
    s_low.unlink()

    s_high = tmp_path / "_high.mp3"
    high_bytes = create_synthetic_audio(
        s_high, format="mp3", artist="Daft Punk", title="Get Lucky", bitrate_kbps=320
    ).read_bytes()
    s_high.unlink()

    # Pre-existing genuine track with '(1)' in its actual title must NOT be renamed
    create_synthetic_audio(
        tmp_path / "Other - Track (1).mp3",
        format="mp3",
        artist="Other",
        title="Track (1)",
        bitrate_kbps=192,
    )

    u_low = mock_audio_server.serve_bytes("get_lucky_128.mp3", low_bytes)
    u_high = mock_audio_server.serve_bytes("get_lucky_320.mp3", high_bytes)

    summary = Downloader(tmp_path, max_concurrent=2).download(
        [
            TrackRecord(url=u_low, artist="Daft Punk", title="Get Lucky"),
            TrackRecord(url=u_high, artist="Daft Punk", title="Get Lucky"),
        ]
    )

    assert summary.downloaded == 2
    assert len(summary.duplicates_resolved) == 1
    names = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert names == ["Daft Punk - Get Lucky.mp3", "Other - Track (1).mp3"]
    assert extract_metadata(tmp_path / "Daft Punk - Get Lucky.mp3").bitrate == 320000


def test_auto_flags_disabled_skips_dedup_and_organization(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    s1 = tmp_path / "_1.mp3"
    b1 = create_synthetic_audio(s1, format="mp3", artist="Band", title="Hit", bitrate_kbps=128).read_bytes()
    s1.unlink()
    s2 = tmp_path / "_2.mp3"
    b2 = create_synthetic_audio(s2, format="mp3", artist="Band", title="Hit", bitrate_kbps=320).read_bytes()
    s2.unlink()

    u1 = mock_audio_server.serve_bytes("hit1.mp3", b1)
    u2 = mock_audio_server.serve_bytes("hit2.mp3", b2)

    summary = Downloader(
        tmp_path,
        auto_organize=False,
        auto_deduplicate=False,
    ).download(
        [
            TrackRecord(url=u1, artist="Band", title="Hit"),
            TrackRecord(url=u2, artist="Band", title="Hit"),
        ]
    )

    assert summary.downloaded == 2
    assert summary.duplicates_resolved == []
    assert summary.organized_files == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Band - Hit (1).mp3", "Band - Hit.mp3"]


def test_headerless_download_organized_by_embedded_tags_updates_file_path(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    staging = tmp_path / "_tagged_src.flac"
    payload = create_synthetic_audio(
        staging,
        format="flac",
        artist="Radiohead",
        title="Karma Police",
        bitrate_kbps=900,
    ).read_bytes()
    staging.unlink()

    url = mock_audio_server.serve_bytes("raw_stream_99.flac", payload)
    summary = Downloader(tmp_path).download(
        [TrackRecord(url=url, artist=None, title=None)]
    )

    assert summary.downloaded == 1
    expected = tmp_path / "Radiohead - Karma Police.flac"
    assert expected.is_file()
    assert summary.tracks[0].file_path == expected
    assert summary.organized_files == [expected]


def test_post_flattening_collision_cleanup_when_nested_beats_root(
    tmp_path: Path, mock_audio_server: MockAudioServer
) -> None:
    # Nested directory has an existing 320 kbps file while root has 128 kbps;
    # after deduplication + flattening, only the standardized filename remains.
    create_synthetic_audio(
        tmp_path / "Band - Song.mp3",
        format="mp3",
        artist="Band",
        title="Song",
        bitrate_kbps=128,
    )
    sub = tmp_path / "ArtistFolder"
    sub.mkdir()
    create_synthetic_audio(
        sub / "Band - Song.mp3",
        format="mp3",
        artist="Band",
        title="Song",
        bitrate_kbps=320,
    )

    # Also test downloading an incoming 192 kbps copy for the same track
    staging = tmp_path / "_mid.mp3"
    mid_bytes = create_synthetic_audio(
        staging,
        format="mp3",
        artist="Band",
        title="Song",
        bitrate_kbps=192,
    ).read_bytes()
    staging.unlink()

    url = mock_audio_server.serve_bytes("band_mid.mp3", mid_bytes)
    summary = Downloader(tmp_path, skip_existing=False).download(
        [TrackRecord(url=url, artist="Band", title="Song")]
    )

    assert summary.downloaded == 1
    assert not (tmp_path / "ArtistFolder").exists()
    final_files = [p for p in tmp_path.iterdir() if p.is_file()]
    assert [p.name for p in final_files] == ["Band - Song.mp3"]
    assert extract_metadata(final_files[0]).bitrate == 320000

