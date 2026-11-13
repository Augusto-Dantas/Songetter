from pathlib import Path
import pytest
from songetter.core import DEFAULT_SERVICES, MORNYE_REGISTRY, configure_mornye_registry, parse_links, run_downloads, RunResult, write_m3u8

def test_parse_links_deduplicates_and_accepts_track_and_playlist():
    links = parse_links("https://open.spotify.com/track/abc123\nhttps://open.spotify.com/playlist/def456")
    assert len(links) == 2

def test_parse_links_accepts_localized_artist_url_and_normalizes_it():
    links = parse_links("https://open.spotify.com/intl-pt/artist/3p7PcrEHaaKLJnPUGOtRIT")
    assert links == ["https://open.spotify.com/artist/3p7PcrEHaaKLJnPUGOtRIT"]

def test_parse_links_accepts_artist_url():
    assert parse_links("https://open.spotify.com/artist/abc123") == [
        "https://open.spotify.com/artist/abc123"
    ]

def test_parse_links_rejects_non_spotify():
    with pytest.raises(ValueError): parse_links("https://example.com/song")

def test_configure_registry_preserves_existing_and_adds_catalog(monkeypatch):
    monkeypatch.setenv("MORNYE_REGISTRIES", "https://example.test/registry.json")
    configured = configure_mornye_registry().split(",")
    assert configured[0] == "https://example.test/registry.json"
    assert MORNYE_REGISTRY in configured and len(configured) == 2

def test_configure_registry_does_not_duplicate_catalog(monkeypatch):
    monkeypatch.setenv("MORNYE_REGISTRIES", MORNYE_REGISTRY)
    assert configure_mornye_registry() == MORNYE_REGISTRY

def test_default_services_are_extension_ids():
    assert DEFAULT_SERVICES and all(x.startswith("ext:") for x in DEFAULT_SERVICES)

def test_write_m3u8_only_includes_existing_audio(tmp_path: Path):
    audio = tmp_path / "Artist" / "Song.flac"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    tracks = [
        {"status": "downloaded", "file_path": str(audio), "title": "Song", "artists": "Artist", "duration_ms": 123000},
        {"status": "failed", "file_path": str(audio), "title": "Nope"},
    ]
    playlist = tmp_path / "library.m3u8"
    assert write_m3u8(tracks, playlist, tmp_path) == 1
    content = playlist.read_text(encoding="utf-8")
    assert "#EXTM3U" in content and "Artist - Song" in content and "Song.flac" in content


def test_run_downloads_processes_urls_concurrently_and_preserves_input_order(tmp_path, monkeypatch):
    import time
    import songetter.core as core

    def fake_run_download(url, output_dir, services=None, lyrics_providers=None, timeout=3600):
        time.sleep(0.02 if url.endswith("1") else 0.01)
        return RunResult(url, 1, 0, 0, [{"title": url}])

    monkeypatch.setattr(core, "run_download", fake_run_download)
    progress = []
    results = run_downloads(
        ["https://open.spotify.com/track/1", "https://open.spotify.com/track/2"],
        tmp_path,
        max_workers=2,
        on_complete=lambda done, total, result: progress.append((done, total)),
    )

    assert [result.url for result in results] == [
        "https://open.spotify.com/track/1",
        "https://open.spotify.com/track/2",
    ]
    assert sorted(progress) == [(1, 2), (2, 2)]


def test_run_downloads_rejects_zero_workers(tmp_path):
    with pytest.raises(ValueError, match="Worker count"):
        run_downloads([], tmp_path, max_workers=0)
