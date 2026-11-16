"""Tests for Songetter unified CLI, Textual TUI, scripts, and Makefile."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import DataTable, Input, ProgressBar, RichLog, Static, TabbedContent, TextArea

from songetter import cli, core
from songetter.core import DEFAULT_SERVICES, RunResult
from songetter.csv_processor import TrackRecord, parse_csv
from songetter.search import SearchResult
from songetter.tui import SongetterTUI, run_tui
from tests.e2e.conftest import MockAudioServer, create_synthetic_audio, create_synthetic_csv

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mock_audio_server():
    """Offline HTTP server for CLI and TUI download tests."""
    server = MockAudioServer()
    try:
        yield server
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Packaging, Scripts & Makefile Tests
# ---------------------------------------------------------------------------


def test_pyproject_dependencies_and_scripts():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert any(d.startswith("httpx>=") for d in deps)
    assert any(d.startswith("textual>=") for d in deps)

    scripts = data["project"]["scripts"]
    assert scripts["songetter"] == "songetter.cli:main"
    assert scripts["songetter-tui"] == "songetter.cli:main_tui"
    assert scripts["songetter-web"] == "songetter.cli:main_web"


def test_install_script_and_makefile_exist():
    script = REPO_ROOT / "scripts" / "install_and_run.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK), "install_and_run.sh must be executable"
    syntax_check = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax_check.returncode == 0, syntax_check.stderr

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    for target in (
        "install:",
        "tui:",
        "serve:",
        "streamlit:",
        "doctor:",
        "test:",
        "docker-build:",
        "android-apk:",
    ):
        assert target in makefile


# ---------------------------------------------------------------------------
# Doctor & Unified CLI Subcommand Tests
# ---------------------------------------------------------------------------


def test_collect_doctor_report_checks_all_items(tmp_path: Path):
    writable_lib = tmp_path / "lib_ok"
    writable_lib.mkdir()
    report = cli.collect_doctor_report(writable_lib)

    for key in (
        "python",
        "mornye",
        "ffmpeg",
        "ffprobe",
        "node",
        "yt-dlp",
        "textual",
        "httpx",
        "fastapi",
        "mutagen",
        "library_dir",
    ):
        assert key in report, f"Missing doctor key: {key}"

    assert report["python"]["ok"] is True
    assert report["textual"]["ok"] is True
    assert report["httpx"]["ok"] is True
    assert report["fastapi"]["ok"] is True
    assert report["mutagen"]["ok"] is True
    assert report["library_dir"]["writable"] is True
    assert report["library_dir"]["ok"] is True

    ro_lib = tmp_path / "lib_ro"
    ro_lib.mkdir()
    ro_lib.chmod(0o555)
    try:
        ro_report = cli.collect_doctor_report(ro_lib)
        assert ro_report["library_dir"]["exists"] is True
        assert ro_report["library_dir"]["writable"] is False
        assert ro_report["library_dir"]["ok"] is False
        assert ro_report["ok"] is False
    finally:
        ro_lib.chmod(0o755)


def test_cli_doctor_json_and_human_output(tmp_path: Path, capsys):
    lib = tmp_path / "music"
    lib.mkdir()
    rc_json = cli.main(["doctor", "-o", str(lib), "--json"])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["library_dir"]["path"] == str(lib.resolve())
    assert payload["library_dir"]["writable"] is True
    assert payload["textual"]["available"] is True

    rc_text = cli.main(["doctor", "-o", str(lib)])
    assert rc_text == 0
    out = capsys.readouterr().out
    assert "Songetter Doctor" in out
    assert "textual" in out
    assert "WRITABLE" in out


def test_cli_no_args_tty_vs_non_tty(monkeypatch, capsys):
    # Non-TTY prints help and raises SystemExit
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        cli.main([])
    help_out = capsys.readouterr().out
    assert "Songetter" in help_out or "usage:" in help_out.lower()

    # Interactive TTY launches run_tui
    called: list[Any] = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "run_tui", lambda lib=None: called.append(lib) or 0)
    assert cli.main([]) == 0
    assert called == [None]


def test_cli_tui_subcommand_and_entrypoint(monkeypatch, tmp_path: Path):
    calls: list[Any] = []
    monkeypatch.setattr(cli, "run_tui", lambda lib=None: calls.append(lib) or 0)

    lib_dir = tmp_path / "my_music"
    assert cli.main(["tui", "-o", str(lib_dir)]) == 0
    assert cli.main_tui(["-o", str(lib_dir)]) == 0
    assert calls == [lib_dir, lib_dir]


def test_cli_serve_web_subcommands_and_entrypoint(monkeypatch, tmp_path: Path):
    recorded: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_uvicorn_run(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)

    lib_dir = tmp_path / "web_lib"
    assert (
        cli.main(
            ["web", "--host", "127.0.0.1", "--port", "8123", "-o", str(lib_dir), "--reload"]
        )
        == 0
    )
    assert os.environ["SONGETTER_LIBRARY_DIR"] == str(lib_dir.resolve())
    assert recorded[0][0][0] == "songetter.web.app:create_app"
    assert recorded[0][1] == {
        "factory": True,
        "host": "127.0.0.1",
        "port": 8123,
        "reload": True,
    }

    assert cli.main_web(["--host", "127.0.0.1", "--port", "8999"]) == 0
    assert recorded[1][1]["port"] == 8999


def test_cli_download_subcommand_with_csv_m3u_and_flags(
    tmp_path: Path, mock_audio_server: MockAudioServer, capsys
):
    a1 = create_synthetic_audio(
        tmp_path / "src" / "a1.mp3", format="mp3", artist="Daft Punk", title="Voyager", bitrate_kbps=192
    )
    a2 = create_synthetic_audio(
        tmp_path / "src" / "a2.flac", format="flac", artist="Justice", title="Genesis", bitrate_kbps=320
    )
    u1 = mock_audio_server.serve_bytes("voyager.mp3", a1.read_bytes(), "audio/mpeg")
    u2 = mock_audio_server.serve_bytes("genesis.flac", a2.read_bytes(), "audio/flac")

    csv_path = tmp_path / "batch.csv"
    create_synthetic_csv(
        [
            {"url": u1, "artist": "Daft Punk", "title": "Voyager"},
            {"url": u2, "artist": "Justice", "title": "Genesis"},
        ],
        file_path=csv_path,
    )

    lib_dir = tmp_path / "library"
    rc = cli.main(
        [
            "download",
            "-i",
            str(csv_path),
            "-o",
            str(lib_dir),
            "--max-concurrent",
            "2",
            "--m3u",
            "MyList.m3u8",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "downloaded=2 skipped=0 failed=0" in out
    assert (lib_dir / "Daft Punk - Voyager.mp3").is_file()
    assert (lib_dir / "Justice - Genesis.flac").is_file()
    m3u_file = lib_dir / "MyList.m3u8"
    assert m3u_file.is_file()
    assert "#EXTM3U" in m3u_file.read_text(encoding="utf-8")
    assert list(lib_dir.rglob("*.lrc")) == []

    # Re-run skips existing with 0 extra HTTP requests
    rc_skip = cli.main(["download", "-i", str(csv_path), "-o", str(lib_dir)])
    assert rc_skip == 0
    assert "downloaded=0 skipped=2 failed=0" in capsys.readouterr().out
    assert mock_audio_server.get_request_count("voyager.mp3") == 1
    assert mock_audio_server.get_request_count("genesis.flac") == 1

    # --no-skip forces re-download
    rc_noskip = cli.main(["download", u1, "-o", str(lib_dir), "--no-skip", "--no-dedup", "--no-flatten"])
    assert rc_noskip == 0
    assert "downloaded=1 skipped=0 failed=0" in capsys.readouterr().out
    assert mock_audio_server.get_request_count("voyager.mp3") == 2


def test_cli_organize_and_dedup_subcommands(tmp_path: Path, capsys):
    lib = tmp_path / "lib"
    nested_low = lib / "Album A" / "song_low.mp3"
    nested_high = lib / "Album B" / "song_high.mp3"
    nested_other = lib / "Other" / "sub" / "unique.flac"

    create_synthetic_audio(
        nested_low, format="mp3", artist="Kanye West", title="Stronger", bitrate_kbps=128
    )
    create_synthetic_audio(
        nested_high, format="mp3", artist="Kanye West", title="Stronger", bitrate_kbps=320
    )
    create_synthetic_audio(
        nested_other, format="flac", artist="M83", title="Midnight City", bitrate_kbps=320
    )

    rc = cli.main(["organize", "-o", str(lib)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Deduplication: resolved 1 group(s)" in out
    assert (lib / "Kanye West - Stronger.mp3").is_file()
    assert (lib / "M83 - Midnight City.flac").is_file()
    assert not (lib / "Album A").exists()
    assert not (lib / "Other").exists()
    assert list(lib.rglob("*.lrc")) == []

    # Test dedup --dedup-only and organize --flatten-only separately
    lib2 = tmp_path / "lib2"
    sub = lib2 / "Sub"
    create_synthetic_audio(sub / "low.mp3", format="mp3", artist="Artist X", title="Song Y", bitrate_kbps=128)
    create_synthetic_audio(sub / "high.mp3", format="mp3", artist="Artist X", title="Song Y", bitrate_kbps=320)
    assert cli.main(["dedup", "-o", str(lib2), "--dedup-only"]) == 0
    assert (sub / "high.mp3").is_file()
    assert not (sub / "low.mp3").exists()

    assert cli.main(["organize", "-o", str(lib2), "--flatten-only"]) == 0
    assert (lib2 / "Artist X - Song Y.mp3").is_file()
    assert not sub.exists()

    with pytest.raises(SystemExit):
        cli.main(["organize", "-o", str(lib2), "--dedup-only", "--flatten-only"])


def test_cli_search_subcommand_export_and_download(
    monkeypatch, tmp_path: Path, mock_audio_server: MockAudioServer, capsys
):
    audio = create_synthetic_audio(
        tmp_path / "seed" / "track.mp3", format="mp3", artist="Justice", title="D.A.N.C.E.", bitrate_kbps=256
    )
    track_url = mock_audio_server.serve_bytes("dance.mp3", audio.read_bytes(), "audio/mpeg")

    def fake_search(query, sources="deezer", limit=10, search_type="track", deduplicate=False):
        return (
            [
                SearchResult(
                    source="deezer",
                    track_id="55",
                    title="D.A.N.C.E.",
                    artist="Justice",
                    album="Cross",
                    duration_seconds=242,
                    url=track_url,
                )
            ],
            {},
        )

    monkeypatch.setattr(cli, "search_tracks_with_errors", fake_search)
    csv_out = tmp_path / "exports" / "found.csv"
    lib_dir = tmp_path / "lib"

    rc = cli.main(
        [
            "search",
            "D.A.N.C.E.",
            "--source",
            "deezer",
            "--type",
            "track",
            "--limit",
            "5",
            "--export-csv",
            str(csv_out),
            "--download",
            "-o",
            str(lib_dir),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Justice - D.A.N.C.E." in out
    assert csv_out.is_file()
    parsed = parse_csv(csv_out.read_bytes())
    assert len(parsed) == 1 and parsed[0].url == track_url
    assert (lib_dir / "Justice - D.A.N.C.E.mp3").is_file()


def test_cli_legacy_compatibility_spotify_csv_and_direct_http(
    monkeypatch, tmp_path: Path, mock_audio_server: MockAudioServer, capsys
):
    # Legacy Spotify link still invokes parse_source_links/run_download/write_m3u8
    monkeypatch.setattr(cli, "configure_mornye_registry", lambda: "reg")
    called_urls: list[str] = []

    def fake_run_download(url, output_dir, services, max_concurrent_downloads=2):
        called_urls.append(url)
        return RunResult(url=url, downloaded=1, skipped=0, failed=0, tracks=[])

    monkeypatch.setattr(cli, "run_download", fake_run_download)
    monkeypatch.setattr(cli, "write_m3u8", lambda tracks, playlist, output: 0)

    assert cli.main(["https://open.spotify.com/track/abc123", "-o", str(tmp_path / "sp_lib")]) == 0
    assert called_urls == ["https://open.spotify.com/track/abc123"]

    # Legacy -i with a .csv file uses parse_csv + Downloader
    audio = create_synthetic_audio(
        tmp_path / "seed" / "song.mp3", format="mp3", artist="Phoenix", title="1901", bitrate_kbps=192
    )
    u = mock_audio_server.serve_bytes("1901.mp3", audio.read_bytes(), "audio/mpeg")
    csv_file = tmp_path / "legacy.csv"
    create_synthetic_csv([{"url": u, "artist": "Phoenix", "title": "1901"}], file_path=csv_file)

    lib_csv = tmp_path / "csv_lib"
    assert cli.main(["-i", str(csv_file), "-o", str(lib_csv)]) == 0
    assert (lib_csv / "Phoenix - 1901.mp3").is_file()

    # Legacy --max-concurrent 0 exits
    with pytest.raises(SystemExit):
        cli.main(["--max-concurrent", "0", "https://open.spotify.com/track/abc123"])


# ---------------------------------------------------------------------------
# Async Textual Pilot Tests across all 4 TUI Tabs
# ---------------------------------------------------------------------------


def test_tui_tab_navigation_and_defaults(monkeypatch, tmp_path: Path):
    lib = tmp_path / "env_lib"
    lib.mkdir()
    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(lib))

    async def _run():
        app = SongetterTUI()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#main-tabs", TabbedContent)
            assert tabs.active == "tab-download"
            assert app.query_one("#input-library-dir", Input).value == str(lib)

            await pilot.press("s")
            assert tabs.active == "tab-search"
            await pilot.press("l")
            assert tabs.active == "tab-library"
            await pilot.press("p")
            assert tabs.active == "tab-doctor"
            await pilot.press("d")
            assert tabs.active == "tab-download"
            await pilot.press("q")
            await pilot.pause()

        assert app._exit is True

    asyncio.run(_run())


def test_tui_download_tab_csv_load_batch_and_worker(
    tmp_path: Path, mock_audio_server: MockAudioServer
):
    a1 = create_synthetic_audio(
        tmp_path / "seed" / "t1.mp3", format="mp3", artist="Daft Punk", title="Around The World", bitrate_kbps=192
    )
    a2 = create_synthetic_audio(
        tmp_path / "seed" / "t2.flac", format="flac", artist="Air", title="Sexy Boy", bitrate_kbps=320
    )
    u1 = mock_audio_server.serve_bytes("atw.mp3", a1.read_bytes(), "audio/mpeg")
    u2 = mock_audio_server.serve_bytes("sexyboy.flac", a2.read_bytes(), "audio/flac")

    csv_path = tmp_path / "ingest.csv"
    create_synthetic_csv(
        [
            {"url": u1, "artist": "Daft Punk", "title": "Around The World"},
            {"url": u2, "artist": "Air", "title": "Sexy Boy"},
        ],
        file_path=csv_path,
    )
    lib_dir = tmp_path / "tui_music"

    async def _run():
        app = SongetterTUI(library_dir=lib_dir)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()

            # Load CSV via UI controls
            app.query_one("#input-csv-path", Input).value = str(csv_path)
            await pilot.click("#btn-load-csv")
            await pilot.pause()

            assert u1 in app.query_one("#input-links", TextArea).text
            assert "Loaded 2 track(s)" in str(app.query_one("#lbl-status", Static).content)

            # Start background worker download via button
            await pilot.click("#btn-start-download")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert (lib_dir / "Daft Punk - Around The World.mp3").is_file()
            assert (lib_dir / "Air - Sexy Boy.flac").is_file()
            assert list(lib_dir.rglob("*.lrc")) == []

            tbl = app.query_one("#tbl-download-results", DataTable)
            assert tbl.row_count == 2
            assert "downloaded=2" in str(app.query_one("#lbl-status", Static).content)
            assert "Organized: 2" in str(app.query_one("#lbl-metrics", Static).content)
            assert app.query_one("#download-progress", ProgressBar).progress == 2
            assert len(app.query_one("#log-download", RichLog).lines) > 0

            # Deterministic synchronous helper skips existing with 0 extra HTTP hits
            summary2 = app.execute_download_batch(
                [TrackRecord(url=u1, artist="Daft Punk", title="Around The World")],
                lib_dir,
            )
            assert summary2.skipped == 1
            assert summary2.downloaded == 0
            assert mock_audio_server.get_request_count("atw.mp3") == 1

    asyncio.run(_run())


def test_tui_search_tab_query_export_and_download(
    monkeypatch, tmp_path: Path, mock_audio_server: MockAudioServer
):
    audio = create_synthetic_audio(
        tmp_path / "seed" / "stardust.mp3",
        format="mp3",
        artist="Stardust",
        title="Music Sounds Better With You",
        bitrate_kbps=256,
    )
    track_url = mock_audio_server.serve_bytes("stardust.mp3", audio.read_bytes(), "audio/mpeg")

    def fake_search(query, sources="deezer", limit=10, search_type="track", deduplicate=False):
        return (
            [
                SearchResult(
                    source="deezer",
                    track_id="901",
                    title="Music Sounds Better With You",
                    artist="Stardust",
                    album="Single",
                    duration_seconds=405,
                    url=track_url,
                )
            ],
            {},
        )

    from songetter.tui import app as tui_app_mod

    monkeypatch.setattr(tui_app_mod, "search_tracks_with_errors", fake_search)
    lib_dir = tmp_path / "search_lib"

    async def _run():
        app = SongetterTUI(library_dir=lib_dir)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("s")
            await pilot.pause()

            app.query_one("#input-search-query", Input).value = "Stardust"
            app.query_one("#input-search-sources", Input).value = "deezer"
            await pilot.click("#btn-search")
            await app.workers.wait_for_complete()
            await pilot.pause()

            tbl = app.query_one("#tbl-search-results", DataTable)
            assert tbl.row_count == 1
            assert tbl.get_row_at(0)[1] == "Stardust"

            await pilot.click("#btn-export-search-csv")
            await pilot.pause()
            csv_file = lib_dir / "search_results.csv"
            assert csv_file.is_file()
            assert len(parse_csv(csv_file.read_bytes())) == 1

            await pilot.click("#btn-download-search")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert (lib_dir / "Stardust - Music Sounds Better With You.mp3").is_file()

    asyncio.run(_run())


def test_tui_library_tab_refresh_organize_and_m3u8(tmp_path: Path):
    lib = tmp_path / "lib_tab"
    create_synthetic_audio(
        lib / "Nested" / "dup_128.mp3",
        format="mp3",
        artist="Disclosure",
        title="Latch",
        bitrate_kbps=128,
    )
    create_synthetic_audio(
        lib / "Other" / "dup_320.mp3",
        format="mp3",
        artist="Disclosure",
        title="Latch",
        bitrate_kbps=320,
    )
    create_synthetic_audio(
        lib / "Nested" / "solo.flac",
        format="flac",
        artist="Flume",
        title="Never Be Like You",
        bitrate_kbps=320,
    )

    async def _run():
        app = SongetterTUI(library_dir=lib)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("l")
            await pilot.pause()

            await pilot.click("#btn-refresh-lib")
            await pilot.pause()
            tbl = app.query_one("#tbl-library", DataTable)
            assert tbl.row_count == 3

            # Also verify 'r' binding refreshes library
            await pilot.press("r")
            await pilot.pause()
            assert tbl.row_count == 3

            # Verify 'o' binding triggers organize & deduplicate
            await pilot.press("o")
            await pilot.pause()
            assert tbl.row_count == 2
            assert (lib / "Disclosure - Latch.mp3").is_file()
            assert (lib / "Flume - Never Be Like You.flac").is_file()
            assert not (lib / "Nested").exists()
            assert not (lib / "Other").exists()
            assert list(lib.rglob("*.lrc")) == []
            assert "Resolved 1 duplicate group(s)" in str(
                app.query_one("#lbl-library-summary", Static).content
            )

            await pilot.click("#btn-organize-lib")
            await pilot.pause()
            assert tbl.row_count == 2

            await pilot.click("#btn-export-m3u8")
            await pilot.pause()
            m3u_path = lib / "Songetter.m3u8"
            assert m3u_path.is_file()
            m3u_text = m3u_path.read_text(encoding="utf-8")
            assert "#EXTM3U" in m3u_text
            assert "Disclosure - Latch" in m3u_text

    asyncio.run(_run())


def test_tui_doctor_tab_providers_cooldown_clear_and_doctor(monkeypatch, tmp_path: Path):
    cooldown_file = tmp_path / "provider_cooldowns.json"
    monkeypatch.setattr(core, "_cooldown_state_path", lambda: cooldown_file)
    core._record_provider_cooldown(
        {
            "provider": "ext:amazon",
            "until": time.time() + 900,
            "reason": "verification paused",
        }
    )

    lib = tmp_path / "doc_lib"
    lib.mkdir()

    async def _run():
        app = SongetterTUI(library_dir=lib)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("p")
            await pilot.pause()

            await pilot.click("#btn-run-doctor")
            await pilot.pause()

            report_text = str(app.query_one("#lbl-doctor-report", Static).content)
            assert "Python:" in report_text
            assert "WRITABLE" in report_text

            tbl = app.query_one("#tbl-providers", DataTable)
            rows = [tbl.get_row_at(i) for i in range(tbl.row_count)]
            providers = {r[0]: r for r in rows}
            for svc in DEFAULT_SERVICES:
                assert svc in providers
            assert providers["ext:amazon"][1] == "COOLDOWN"

            await pilot.click("#btn-clear-cooldowns")
            await pilot.pause()
            rows_after = [tbl.get_row_at(i) for i in range(tbl.row_count)]
            providers_after = {r[0]: r for r in rows_after}
            assert providers_after["ext:amazon"][1] == "READY"
            assert core._load_cooldowns() == {}

    asyncio.run(_run())


def test_run_tui_helper_returns_int(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(SongetterTUI, "run", lambda self: 0)
    assert run_tui(tmp_path) == 0
