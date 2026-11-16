"""Offline unit and integration tests for Songetter CLI, Streamlit app, and Dockerfile."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import py_compile
from types import SimpleNamespace
from typing import Any

import pytest

from songetter import cli
from songetter.core import RunResult
from songetter.search import SearchResult
from tests.e2e.conftest import MockAudioServer, create_synthetic_audio

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mock_audio_server():
    """Local fixture wrapping MockAudioServer for offline CLI download tests."""
    server = MockAudioServer()
    try:
        yield server
    finally:
        server.shutdown()


def test_cli_list_sources_prints_registered_sources(capsys):
    rc = cli.main(["--list-sources"])
    assert rc == 0
    out = capsys.readouterr().out
    for source_id in ("deezer", "tidal", "itunes", "musicbrainz", "qobuz", "spotify"):
        assert source_id in out
    assert "Deezer" in out
    assert "no auth required" in out
    assert "auth required" in out


def test_cli_list_sources_json_output(capsys):
    rc = cli.main(["--list-sources", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    ids = {entry["id"] for entry in payload}
    assert {"deezer", "tidal", "itunes", "musicbrainz", "qobuz", "spotify"}.issubset(ids)
    for entry in payload:
        assert {"id", "name", "requires_auth", "configured"}.issubset(entry.keys())


def test_cli_search_formatted_and_options(monkeypatch, capsys):
    captured: dict[str, Any] = {}

    def fake_search(query, sources="deezer", limit=10, search_type="track", deduplicate=False):
        captured.update(
            {
                "query": query,
                "sources": list(sources),
                "limit": limit,
                "search_type": search_type,
                "deduplicate": deduplicate,
            }
        )
        results = [
            SearchResult(
                source="deezer",
                track_id="101",
                title="One More Time",
                artist="Daft Punk",
                album="Discovery",
                duration_seconds=320,
                url="https://www.deezer.com/track/101",
            )
        ]
        errors = {"spotify": "Spotify search is not configured."}
        return results, errors

    monkeypatch.setattr(cli, "search_tracks_with_errors", fake_search)

    rc = cli.main(
        [
            "--search",
            "Discovery",
            "--search-source",
            "deezer,itunes",
            "--search-source",
            "spotify",
            "--search-limit",
            "5",
            "--search-type",
            "album",
            "--deduplicate-search",
        ]
    )
    assert rc == 0
    assert captured == {
        "query": "Discovery",
        "sources": ["deezer", "itunes", "spotify"],
        "limit": 5,
        "search_type": "album",
        "deduplicate": True,
    }
    out = capsys.readouterr().out
    assert "[deezer] Daft Punk - One More Time" in out
    assert "Discovery" in out
    assert "[5:20]" in out
    assert "Warning [spotify]: Spotify search is not configured." in out


def test_cli_search_defaults_and_all_source(monkeypatch, capsys):
    calls: list[dict[str, Any]] = []

    def fake_search(query, sources="deezer", limit=10, search_type="track", deduplicate=False):
        calls.append(
            {
                "query": query,
                "sources": list(sources),
                "limit": limit,
                "search_type": search_type,
                "deduplicate": deduplicate,
            }
        )
        return [], {}

    monkeypatch.setattr(cli, "search_tracks_with_errors", fake_search)

    assert cli.main(["--search", "No Match"]) == 0
    assert calls[0] == {
        "query": "No Match",
        "sources": ["deezer"],
        "limit": 10,
        "search_type": "track",
        "deduplicate": False,
    }
    assert "No results found" in capsys.readouterr().out

    assert cli.main(["--search", "Anything", "--search-source", "all"]) == 0
    assert calls[1]["sources"] == ["all"]


def test_cli_search_json_output(monkeypatch, capsys):
    def fake_search(query, sources="deezer", limit=10, search_type="track", deduplicate=False):
        return (
            [
                SearchResult(
                    source="itunes",
                    track_id="777",
                    title="Stronger",
                    artist="Kanye West",
                    album="Graduation",
                    duration_seconds=311,
                    url="https://music.apple.com/us/album/stronger/777",
                    isrc="USUM70741299",
                )
            ],
            {"qobuz": "missing token"},
        )

    monkeypatch.setattr(cli, "search_tracks_with_errors", fake_search)

    rc = cli.main(["--search", "Stronger", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "Stronger"
    assert payload["count"] == 1
    assert payload["results"][0]["source"] == "itunes"
    assert payload["results"][0]["title"] == "Stronger"
    assert payload["results"][0]["isrc"] == "USUM70741299"
    assert payload["errors"] == {"qobuz": "missing token"}


def test_cli_search_unsupported_source_returns_error(capsys):
    rc = cli.main(["--search", "test", "--search-source", "unknown_provider_xyz"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unsupported search source" in err


def test_cli_download_search_via_mock_audio_server(
    monkeypatch, tmp_path: Path, mock_audio_server: MockAudioServer, capsys
):
    src_audio = create_synthetic_audio(
        tmp_path / "src" / "hb.mp3",
        format="mp3",
        artist="Daft Punk",
        title="Harder Better",
        bitrate_kbps=192,
    )
    track_url = mock_audio_server.serve_bytes(
        "daft_punk_hb.mp3", src_audio.read_bytes(), content_type="audio/mpeg"
    )
    err_url = mock_audio_server.serve_error("missing_track.mp3", status_code=404)

    def fake_search(query, sources="deezer", limit=10, search_type="track", deduplicate=False):
        return (
            [
                SearchResult(
                    source="deezer",
                    track_id="1",
                    title="Harder Better",
                    artist="Daft Punk",
                    url=track_url,
                ),
                SearchResult(
                    source="deezer",
                    track_id="2",
                    title="Missing Song",
                    artist="Daft Punk",
                    url=err_url,
                ),
            ],
            {},
        )

    monkeypatch.setattr(cli, "search_tracks_with_errors", fake_search)

    lib_dir = tmp_path / "library"
    rc = cli.main(
        [
            "--search",
            "Daft Punk",
            "--download-search",
            "-o",
            str(lib_dir),
            "--max-concurrent",
            "2",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "downloaded=1 skipped=0 failed=1" in out
    assert "ERROR [Missing Song]" in out
    assert (lib_dir / "Daft Punk - Harder Better.mp3").is_file()
    assert mock_audio_server.get_request_count("daft_punk_hb.mp3") == 1


def test_cli_download_search_requires_search():
    with pytest.raises(SystemExit):
        cli.main(["--download-search"])


def test_cli_csv_downloads_organizes_and_skips_existing(
    tmp_path: Path, mock_audio_server: MockAudioServer, capsys
):
    mp3_file = create_synthetic_audio(
        tmp_path / "seed" / "song1.mp3",
        format="mp3",
        artist="Artist One",
        title="Track Alpha",
        bitrate_kbps=192,
    )
    flac_file = create_synthetic_audio(
        tmp_path / "seed" / "song2.flac",
        format="flac",
        artist="Artist Two",
        title="Track Beta",
        bitrate_kbps=320,
    )
    url1 = mock_audio_server.serve_bytes("track_alpha.mp3", mp3_file.read_bytes(), content_type="audio/mpeg")
    url2 = mock_audio_server.serve_bytes("track_beta.flac", flac_file.read_bytes(), content_type="audio/flac")

    csv_path = tmp_path / "tracks.csv"
    csv_path.write_text(
        f"url,artist,title\n{url1},Artist One,Track Alpha\n{url2},Artist Two,Track Beta\n",
        encoding="utf-8",
    )

    lib_dir = tmp_path / "music_lib"
    rc1 = cli.main(["--csv", str(csv_path), "-o", str(lib_dir), "--max-concurrent", "2"])
    assert rc1 == 0
    out1 = capsys.readouterr().out
    assert "Parsed 2 track(s)" in out1
    assert "downloaded=2 skipped=0 failed=0" in out1
    assert (lib_dir / "Artist One - Track Alpha.mp3").is_file()
    assert (lib_dir / "Artist Two - Track Beta.flac").is_file()

    # Re-running the same CSV must skip both tracks with zero extra HTTP requests
    rc2 = cli.main(["--csv", str(csv_path), "-o", str(lib_dir)])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "downloaded=0 skipped=2 failed=0" in out2
    assert mock_audio_server.get_request_count("track_alpha.mp3") == 1
    assert mock_audio_server.get_request_count("track_beta.flac") == 1


def test_cli_csv_error_cases(tmp_path: Path, capsys):
    missing_rc = cli.main(["--csv", str(tmp_path / "does_not_exist.csv")])
    assert missing_rc == 1
    assert "not found" in capsys.readouterr().err

    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("   \n", encoding="utf-8")
    empty_rc = cli.main(["--csv", str(empty_csv)])
    assert empty_rc == 1
    assert "empty" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    ("argv", "expected_host", "expected_port"),
    [
        (["serve"], "0.0.0.0", 8000),
        (["serve", "--host", "127.0.0.1", "--port", "9000"], "127.0.0.1", 9000),
        (["--serve"], "0.0.0.0", 8000),
        (["--serve", "--host", "127.0.0.1", "--port", "9001"], "127.0.0.1", 9001),
    ],
)
def test_cli_serve_and_flag_launch_uvicorn(monkeypatch, argv, expected_host, expected_port):
    recorded: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_uvicorn_run(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)

    rc = cli.main(argv)
    assert rc == 0
    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args[0] == "songetter.web.app:create_app"
    assert kwargs["factory"] is True
    assert kwargs["host"] == expected_host
    assert kwargs["port"] == expected_port


@pytest.mark.parametrize(
    "bad_argv",
    [
        [],
        ["--max-concurrent", "0", "https://open.spotify.com/track/abc123"],
        ["--search", "daft punk", "--search-limit", "0"],
        ["serve", "--port", "70000"],
        ["serve", "extra_arg"],
        ["--serve", "https://open.spotify.com/track/abc123"],
        ["--list-sources", "--search", "daft punk"],
    ],
)
def test_cli_invalid_argument_combinations_exit(bad_argv):
    with pytest.raises(SystemExit):
        cli.main(bad_argv)


def test_cli_links_and_input_file_backward_compatible_multisource(
    monkeypatch, tmp_path: Path, capsys
):
    links_file = tmp_path / "links.txt"
    links_file.write_text(
        "\n".join(
            [
                "https://open.spotify.com/track/abc123",
                "https://tidal.com/browse/track/999",
                "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            ]
        ),
        encoding="utf-8",
    )

    configured = {"called": False}
    download_calls: list[dict[str, Any]] = []
    m3u_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(cli, "configure_mornye_registry", lambda: configured.__setitem__("called", True) or "reg")

    def fake_run_download(url, output_dir, services, max_concurrent_downloads=2):
        download_calls.append(
            {
                "url": url,
                "output_dir": output_dir,
                "services": list(services),
                "max_concurrent_downloads": max_concurrent_downloads,
            }
        )
        return RunResult(
            url=url,
            downloaded=1,
            skipped=0,
            failed=0,
            tracks=[{"title": "Song", "artists": "Artist", "status": "downloaded", "file_path": str(output_dir / "Song.flac")}],
        )

    def fake_write_m3u8(tracks, playlist, output):
        m3u_calls.append({"tracks": tracks, "playlist": playlist, "output": output})
        return len(tracks)

    monkeypatch.setattr(cli, "run_download", fake_run_download)
    monkeypatch.setattr(cli, "write_m3u8", fake_write_m3u8)

    lib_dir = tmp_path / "out_music"
    rc = cli.main(
        [
            "https://open.spotify.com/intl-pt/track/abc123",
            "https://www.deezer.com/track/42",
            "-i",
            str(links_file),
            "-o",
            str(lib_dir),
            "--service",
            "ext:deezer",
            "--service",
            "ext:tidal-web",
            "--max-concurrent",
            "4",
            "--m3u",
            "Custom.m3u8",
        ]
    )
    assert rc == 0
    assert configured["called"] is True

    # Localized Spotify URL normalizes to /track/abc123 and deduplicates with links_file
    assert [c["url"] for c in download_calls] == [
        "https://open.spotify.com/track/abc123",
        "https://www.deezer.com/track/42",
        "https://tidal.com/browse/track/999",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    ]
    assert all(c["services"] == ["ext:deezer", "ext:tidal-web"] for c in download_calls)
    assert all(c["max_concurrent_downloads"] == 4 for c in download_calls)
    assert len(m3u_calls) == 1
    assert m3u_calls[0]["playlist"] == lib_dir.resolve() / "Custom.m3u8"

    out = capsys.readouterr().out
    assert "Completed: {'downloaded': 4, 'skipped': 0, 'failed': 0}" in out
    assert "Providers: ext:deezer, ext:tidal-web" in out


def test_cli_links_rejects_unknown_host(capsys):
    rc = cli.main(["https://example.com/not-supported"])
    assert rc == 1
    assert "Invalid music link" in capsys.readouterr().err


def test_app_py_compiles_and_renders_three_tabs():
    app_path = REPO_ROOT / "app.py"
    cli_path = REPO_ROOT / "src" / "songetter" / "cli.py"

    py_compile.compile(str(app_path), doraise=True)
    py_compile.compile(str(cli_path), doraise=True)
    ast.parse(app_path.read_text(encoding="utf-8"))

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(app_path)).run()
    assert not at.exception
    assert [tab.label for tab in at.tabs] == [
        "Links & Batch Download",
        "Multi-Source Search",
        "CSV Library Ingestion",
    ]


def test_dockerfile_fastapi_entrypoint_and_streamlit_preserved():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "SONGETTER_LIBRARY_DIR=/library" in dockerfile
    assert "XDG_CACHE_HOME=/cache" in dockerfile
    assert "EXPOSE 8501 8000" in dockerfile
    assert "COPY pyproject.toml README.md app.py ./" in dockerfile
    assert (
        'CMD ["uvicorn", "songetter.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]'
        in dockerfile
    )
