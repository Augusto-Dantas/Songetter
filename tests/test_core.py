from pathlib import Path
import json
import subprocess

import pytest

from songetter.core import (
    DEFAULT_SERVICES,
    MORNYE_REGISTRY,
    _read_report,
    RunResult,
    _merge_provider_tracks,
    _parse_provider_cooldown,
    _active_provider_cooldown,
    _record_provider_cooldown,
    configure_mornye_registry,
    parse_links,
    write_m3u8,
)


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
    with pytest.raises(ValueError):
        parse_links("https://example.com/song")


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


def test_read_report_preserves_provider_failures():
    report = {
        "schema_version": 1,
        "tracks": [{"title": "Stronger", "status": "failed", "error": "verification paused"}],
        "summary": {"downloaded": 0, "skipped": 0, "failed": 1},
    }
    result = _read_report(json.dumps(report), "spotify-url", 0, "")
    assert result.failed == 1
    assert "1 track(s) failed" in result.error
    assert result.tracks[0]["error"] == "verification paused"



def test_read_report_counts_provider_log_failures_missing_from_summary():
    report = {
        "schema_version": 1,
        "tracks": [],
        "summary": {"downloaded": 0, "skipped": 0, "failed": 0},
    }
    stderr = (
        "  ✗  [#3] ext:amazon  ·  verification paused after a failed attempt\\n"
        "  ✗  [#7] ext:deezer  ·  Failed to resolve Deezer download\\n"
    )
    result = _read_report(json.dumps(report), "spotify-url", 0, stderr)
    assert result.failed == 2
    assert len(result.tracks) == 2
    assert {track["provider"] for track in result.tracks} == {"ext:amazon", "ext:deezer"}
    assert "2 track(s) failed" in result.error


def test_extract_provider_warning_without_track_index():
    from songetter.core import _extract_log_failures

    failures = _extract_log_failures(
        "WARNING: [ext:amazon] Download FAILED for 'Stronger' after 0.0s: verification paused"
    )
    assert len(failures) == 1
    assert failures[0]["title"] == "Stronger"
    assert failures[0]["status"] == "failed"

def test_read_report_handles_invalid_json_without_name_error():
    result = _read_report("{broken", "spotify-url", 1, "provider error")
    assert result.failed == 1
    assert "provider error" in result.error


def test_read_report_handles_empty_output():
    result = _read_report("", "spotify-url", 2, "")
    assert result.failed == 1
    assert "without producing a JSON report" in result.error


def test_run_download_rejects_zero_concurrency(tmp_path):
    from songetter.core import run_download

    with pytest.raises(ValueError, match="Concurrent download count"):
        run_download("https://open.spotify.com/track/abc123", tmp_path, max_concurrent_downloads=0)


def test_parse_provider_cooldown_from_mornye_warning():
    event = _parse_provider_cooldown(
        "WARNING: [ext:amazon] Download FAILED for 'Stronger': verification paused after a failed attempt — next try in 17 min",
        "ext:amazon",
    )
    assert event is not None
    assert event["provider"] == "ext:amazon"
    assert event["retry_after_seconds"] == 17 * 60
    assert event["status"] == "cooldown"


def test_parse_provider_cooldown_from_indexed_failure():
    event = _parse_provider_cooldown(
        "  ✗ [#3] ext:deezer · rate limit reached; retry in 45 seconds",
        "ext:amazon",
    )
    assert event is not None
    assert event["provider"] == "ext:deezer"
    assert event["retry_after_seconds"] == 45


def test_provider_cooldown_is_persisted_until_expiry(monkeypatch, tmp_path):
    from songetter import core

    monkeypatch.setattr(core, "_cooldown_state_path", lambda: tmp_path / "provider_cooldowns.json")
    event = {
        "provider": "ext:amazon",
        "until": __import__("time").time() + 600,
        "reason": "verification paused",
    }
    _record_provider_cooldown(event)
    active = _active_provider_cooldown("ext:amazon")
    assert active is not None
    assert active["retry_after_seconds"] > 0


def test_provider_track_merge_prefers_successful_fallback():
    first = [{"title": "Stronger", "artists": "Kanye West", "album": "Graduation", "status": "failed"}]
    second = [{"title": "Stronger", "artists": "Kanye West", "album": "Graduation", "status": "downloaded"}]
    merged = _merge_provider_tracks(first, second)
    assert len(merged) == 1
    assert merged[0]["status"] == "downloaded"


def test_run_download_moves_to_next_provider_after_cooldown(monkeypatch, tmp_path):
    from songetter import core

    calls = []
    monkeypatch.setattr(core, "_active_provider_cooldown", lambda provider: None)

    def fake_run(url, output_dir, services, lyrics_providers, timeout, concurrency, progress, metrics):
        provider = services[0]
        calls.append(provider)
        if provider == "ext:amazon":
            return RunResult(
                url, 0, 0, 0, [],
                "ext:amazon entered cooldown",
                0.1,
                [{
                    "provider": provider,
                    "status": "cooldown",
                    "retry_after_seconds": 900,
                    "until": __import__("time").time() + 900,
                    "reason": "verification paused",
                }],
            )
        return RunResult(
            url, 1, 0, 0,
            [{"title": "Stronger", "artists": "Kanye West", "album": "Graduation", "status": "downloaded"}],
        )

    monkeypatch.setattr(core, "_run_download_once", fake_run)
    result = core.run_download(
        "https://open.spotify.com/album/abc123",
        tmp_path,
        services=["ext:amazon", "ext:deezer"],
    )
    assert calls == ["ext:amazon", "ext:deezer"]
    assert result.downloaded == 1
    assert result.failed == 0
    assert result.tracks[0]["status"] == "downloaded"
    assert result.provider_status[0]["status"] == "cooldown"
