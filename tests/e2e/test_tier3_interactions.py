"""Tier 3: Cross-Feature Interactions E2E Tests (5 Test Cases).

Verifies state transitions and data contracts across feature boundaries:
- INT-01: R1 + R2 (Path Selection to Download Pre-check)
- INT-02: R2 + R3 (Download Execution to Post-Download Quality Deduplication)
- INT-03: R3 + R4 (Duplicate Resolution across Nested Directory Flattening)
- INT-04: R1 + R4 + R5 (CSV Ingestion with Forbidden Characters to Sanitized Flat Output)
- INT-05: R2 + R5 (Multi-track Batch Resumption with Mid-stream Fault Isolation)
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from songetter.csv_processor import (
    TrackRecord,
    parse_csv,
)
from songetter.deduplicator import (
    AudioMetadata,
    extract_metadata,
    resolve_duplicates,
)
from songetter.library import (
    LibraryIndex,
    validate_library_directory,
)
from songetter.organizer import (
    flatten_and_organize_library,
    format_standard_filename,
    sanitize_filename_component,
)

from tests.e2e.conftest import (
    create_synthetic_audio,
    create_synthetic_csv,
    has_downloader_module,
    has_web_module,
    create_app,
    Downloader,
)


def test_interaction_r1_r2_path_selection_to_download_precheck(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T3-INT-01: Verify integration from path selection to pre-download skip and execution."""
    # 1. Select and validate target directory
    target_lib = validate_library_directory(clean_library_dir)
    assert target_lib == clean_library_dir.resolve()

    # 2. Pre-seed library with Track A
    track_a_file = target_lib / "ArtistA - SongA.mp3"
    create_synthetic_audio(track_a_file, format="mp3", artist="ArtistA", title="SongA")

    # 3. CSV contains Track A (existing) and Track B (new)
    csv_bytes = create_synthetic_csv([
        {"url": "http://127.0.0.1/song_a.mp3", "artist": "ArtistA", "title": "SongA"},
        {"url": "http://127.0.0.1/song_b.mp3", "artist": "ArtistB", "title": "SongB"},
    ])
    records = parse_csv(csv_bytes)
    assert len(records) == 2

    # 4. Pre-download check against index
    index = LibraryIndex.build(target_lib)
    assert index.contains_track("ArtistA", "SongA") is True
    assert index.contains_track("ArtistB", "SongB") is False

    # 5. If downloader is available, execute full job
    if has_downloader_module and Downloader is not None:
        u_b = mock_audio_server.serve_bytes("song_b.mp3", b"AUDIO_DATA")
        records[1].url = u_b
        downloader = Downloader(target_lib)
        summary = downloader.download(records)
        assert summary.skipped == 1
        assert summary.downloaded == 1
        assert mock_audio_server.get_request_count("song_a.mp3") == 0
        assert mock_audio_server.get_request_count("song_b.mp3") == 1


def test_interaction_r2_r3_download_execution_to_quality_deduplication(clean_library_dir: Path) -> None:
    """TC-T3-INT-02: Verify post-download quality deduplication replaces inferior replicate."""
    # Pre-existing low-quality file in library (128 kbps)
    existing_low = clean_library_dir / "Queen - Bohemian Rhapsody.mp3"
    create_synthetic_audio(
        existing_low,
        format="mp3",
        artist="Queen",
        title="Bohemian Rhapsody",
        bitrate_kbps=128,
    )

    # Incoming new download arrives with higher bitrate (320 kbps)
    incoming_high = clean_library_dir / "Queen - Bohemian Rhapsody (High Quality).mp3"
    create_synthetic_audio(
        incoming_high,
        format="mp3",
        artist="Queen",
        title="Bohemian Rhapsody",
        bitrate_kbps=320,
    )

    # Trigger post-download duplicate resolution
    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    assert results[0].retained == incoming_high
    assert existing_low in results[0].deleted
    assert incoming_high.exists() is True
    assert existing_low.exists() is False


def test_interaction_r3_r4_duplicate_resolution_across_nested_flattening(clean_library_dir: Path) -> None:
    """TC-T3-INT-03: Verify flattening nested provider folders cooperates with duplicate resolution."""
    # Root has inferior file (128 kbps)
    root_file = clean_library_dir / "Daft Punk - One More Time.mp3"
    create_synthetic_audio(root_file, format="mp3", artist="Daft Punk", title="One More Time", bitrate_kbps=128)

    # Nested provider download has superior file (320 kbps)
    nested_dir = clean_library_dir / "Daft Punk" / "Discovery"
    nested_dir.mkdir(parents=True, exist_ok=True)
    nested_file = nested_dir / "track01.mp3"
    create_synthetic_audio(nested_file, format="mp3", artist="Daft Punk", title="One More Time", bitrate_kbps=320)

    # 1. Flatten filesystem
    flatten_and_organize_library(clean_library_dir)

    # Nested folders should be pruned
    assert not (clean_library_dir / "Daft Punk").exists()

    # 2. Post-flattening duplicate resolution
    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1

    # Only 1 high-quality file should remain
    remaining = [p for p in clean_library_dir.iterdir() if p.is_file() and p.suffix == ".mp3"]
    assert len(remaining) == 1
    assert extract_metadata(remaining[0]).bitrate == 320000


def test_interaction_r1_r4_r5_csv_forbidden_tags_to_sanitized_flat_output(clean_library_dir: Path) -> None:
    """TC-T3-INT-04: Verify CSV entries with forbidden characters pass safely to standardized output."""
    csv_bytes = create_synthetic_csv([
        {
            "url": "http://127.0.0.1/track.flac",
            "artist": "AC/DC",
            "title": 'Who Made Who? [12" Remix] *Special*',
        }
    ])

    # 1. Ingestion parses successfully
    records = parse_csv(csv_bytes)
    assert len(records) == 1
    raw_artist = records[0].artist
    raw_title = records[0].title

    # 2. Create raw audio and organize
    audio_path = clean_library_dir / "incoming.flac"
    create_synthetic_audio(audio_path, format="flac", artist=raw_artist, title=raw_title)

    organized = flatten_and_organize_library(clean_library_dir)
    assert len(organized) == 1

    out_name = organized[0].name
    for forbidden in ["/", "\\", "*", "?", ":", '"', "<", ">", "|"]:
        assert forbidden not in out_name
    assert organized[0].exists() is True


def test_interaction_r2_r5_partial_failure_batch_resumption(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T3-INT-05: Verify multi-track batch isolates mid-stream failures and successfully completes remaining tracks."""
    if not has_downloader_module or Downloader is None:
        pytest.skip("songetter.downloader is not yet implemented (Milestone 2)")

    u1 = mock_audio_server.serve_bytes("good1.mp3", b"VALID_AUDIO_1" * 100)
    u2 = mock_audio_server.serve_truncated("broken.mp3", full_size=100000, cut_at_byte=500)
    u3 = mock_audio_server.serve_bytes("good2.mp3", b"VALID_AUDIO_2" * 100)

    downloader = Downloader(clean_library_dir)
    summary = downloader.download([
        TrackRecord(url=u1, artist="Artist", title="Good1", raw_row={}),
        TrackRecord(url=u2, artist="Artist", title="Broken", raw_row={}),
        TrackRecord(url=u3, artist="Artist", title="Good2", raw_row={}),
    ])

    assert summary.downloaded == 2
    assert summary.failed == 1
    assert not list(clean_library_dir.glob("*.part"))
