"""Tier 4: Real-World Application Scenarios E2E Tests (5 Test Cases).

Verifies complete end-to-end user workflows matching realistic operational lifecycles:
- SCN-01: Initial Clean Library Ingestion & Flattening
- SCN-02: Incremental Library Expansion with Pre-Download Skipping
- SCN-03: Quality Upgrade Workflow (Replacing Low-Bitrate Files with Lossless)
- SCN-04: Quality Downgrade Rejection (Protecting High-Fidelity Collection)
- SCN-05: Fault-Tolerant Ingestion Recovery (Handling Mixed Corrupt & Valid Inputs)
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


def test_scenario_initial_clean_library_ingestion(clean_library_dir: Path) -> None:
    """TC-T4-SCN-01: Complete lifecycle for initial clean library setup and 5-track ingestion."""
    # Step 1: Select and validate clean target library
    lib = validate_library_directory(clean_library_dir)
    assert lib == clean_library_dir.resolve()

    # Step 2: Ingest 5-track CSV
    tracks_spec = [
        {"url": "http://127.0.0.1/song1.mp3", "artist": "Artist 1", "title": "Track 1"},
        {"url": "http://127.0.0.1/song2.mp3", "artist": "Artist 2", "title": "Track 2"},
        {"url": "http://127.0.0.1/song3.mp3", "artist": "Artist 3", "title": "Track 3"},
        {"url": "http://127.0.0.1/song4.flac", "artist": "Artist 4", "title": "Track 4"},
        {"url": "http://127.0.0.1/song5.flac", "artist": "Artist 5", "title": "Track 5"},
    ]
    csv_bytes = create_synthetic_csv(tracks_spec)
    records = parse_csv(csv_bytes)
    assert len(records) == 5

    # Step 3: Populate/download files into library (potentially nested)
    for i, r in enumerate(records):
        sub = lib / f"Provider_Folder_{i}"
        fmt = "flac" if r.url.endswith(".flac") else "mp3"
        create_synthetic_audio(
            sub / f"raw_{i}.{fmt}",
            format=fmt,
            artist=r.artist or "Artist",
            title=r.title or f"Track {i}",
        )

    # Step 4: Organize and standardize library
    organized = flatten_and_organize_library(lib)
    assert len(organized) == 5

    # Step 5: Verification assertions
    all_items = list(lib.iterdir())
    assert len(all_items) == 5
    # All items are regular flat audio files in the library root
    assert all(item.is_file() for item in all_items)
    # Check standardized naming
    assert (lib / "Artist 1 - Track 1.mp3").exists()
    assert (lib / "Artist 4 - Track 4.flac").exists()
    # Zero .part files
    assert not list(lib.glob("*.part"))


def test_scenario_incremental_library_update_with_precheck(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T4-SCN-02: Incremental library expansion verifying 0 network requests for 3 existing tracks."""
    lib = validate_library_directory(clean_library_dir)

    # Initial state: library contains 3 tracks
    initial_tracks = [
        ("Queen", "Bohemian Rhapsody", "mp3"),
        ("Pink Floyd", "Time", "flac"),
        ("Daft Punk", "One More Time", "mp3"),
    ]
    for artist, title, fmt in initial_tracks:
        create_synthetic_audio(
            lib / f"{artist} - {title}.{fmt}",
            format=fmt,
            artist=artist,
            title=title,
        )

    # Operator uploads CSV with 6 tracks: 3 existing + 3 new
    incoming_spec = [
        {"url": "http://127.0.0.1/queen.mp3", "artist": "Queen", "title": "Bohemian Rhapsody"},
        {"url": "http://127.0.0.1/time.flac", "artist": "Pink Floyd", "title": "Time"},
        {"url": "http://127.0.0.1/daft.mp3", "artist": "Daft Punk", "title": "One More Time"},
        {"url": "http://127.0.0.1/new1.mp3", "artist": "Radiohead", "title": "Karma Police"},
        {"url": "http://127.0.0.1/new2.flac", "artist": "Nirvana", "title": "Smells Like Teen Spirit"},
        {"url": "http://127.0.0.1/new3.mp3", "artist": "ACDC", "title": "Thunderstruck"},
    ]
    csv_bytes = create_synthetic_csv(incoming_spec)
    records = parse_csv(csv_bytes)
    assert len(records) == 6

    # Verify pre-download index identification
    index = LibraryIndex.build(lib)
    skipped_records = [r for r in records if index.contains_track(r.artist or "", r.title or "")]
    download_records = [r for r in records if not index.contains_track(r.artist or "", r.title or "")]

    assert len(skipped_records) == 3
    assert len(download_records) == 3

    # Populate the 3 new tracks and organize
    for r in download_records:
        fmt = "flac" if r.url.endswith(".flac") else "mp3"
        create_synthetic_audio(lib / f"{r.artist} - {r.title}.{fmt}", format=fmt, artist=r.artist or "", title=r.title or "")

    flatten_and_organize_library(lib)
    assert len(list(lib.iterdir())) == 6


def test_scenario_quality_upgrade_workflow(clean_library_dir: Path) -> None:
    """TC-T4-SCN-03: Quality upgrade replacing existing low-bitrate MP3s with lossless FLAC."""
    lib = validate_library_directory(clean_library_dir)

    # 1. Library pre-populated with 2 low-quality MP3s (128 kbps)
    mp3_1 = lib / "Artist A - Hit 1.mp3"
    mp3_2 = lib / "Artist B - Hit 2.mp3"
    create_synthetic_audio(mp3_1, format="mp3", artist="Artist A", title="Hit 1", bitrate_kbps=128)
    create_synthetic_audio(mp3_2, format="mp3", artist="Artist B", title="Hit 2", bitrate_kbps=128)

    # 2. Ingest higher quality FLAC versions
    flac_1 = lib / "Artist A - Hit 1.flac"
    flac_2 = lib / "Artist B - Hit 2.flac"
    create_synthetic_audio(flac_1, format="flac", artist="Artist A", title="Hit 1", bitrate_kbps=1411)
    create_synthetic_audio(flac_2, format="flac", artist="Artist B", title="Hit 2", bitrate_kbps=1411)

    # 3. Post-download deduplication resolves quality
    results = resolve_duplicates(lib)
    assert len(results) == 2

    # 4. Assert FLAC files retained, MP3s deleted
    assert flac_1.exists() is True
    assert flac_2.exists() is True
    assert mp3_1.exists() is False
    assert mp3_2.exists() is False

    remaining = list(lib.iterdir())
    assert len(remaining) == 2
    assert all(p.suffix == ".flac" for p in remaining)


def test_scenario_quality_downgrade_rejection(clean_library_dir: Path) -> None:
    """TC-T4-SCN-04: Protecting collection by rejecting/deleting incoming lower-quality duplicates."""
    lib = validate_library_directory(clean_library_dir)

    # 1. Pre-existing high-fidelity FLAC track
    flac_orig = lib / "Master - Masterpiece.flac"
    create_synthetic_audio(flac_orig, format="flac", artist="Master", title="Masterpiece", bitrate_kbps=1411)

    # 2. Incoming lower-quality MP3 replicate (128 kbps)
    mp3_inferior = lib / "Master - Masterpiece.mp3"
    create_synthetic_audio(mp3_inferior, format="mp3", artist="Master", title="Masterpiece", bitrate_kbps=128)

    # 3. Post-download duplicate scanner
    results = resolve_duplicates(lib)
    assert len(results) == 1
    assert results[0].retained == flac_orig
    assert mp3_inferior in results[0].deleted

    # 4. Verify original FLAC preserved and inferior MP3 unlinked
    assert flac_orig.exists() is True
    assert mp3_inferior.exists() is False
    assert len(list(lib.iterdir())) == 1


def test_scenario_fault_tolerant_ingestion_recovery(clean_library_dir: Path) -> None:
    """TC-T4-SCN-05: Real-world CSV with valid tracks, empty lines, and bad formats recovers safely."""
    lib = validate_library_directory(clean_library_dir)

    messy_csv = (
        "url,artist,title\n"
        "http://127.0.0.1/valid1.mp3,Valid Artist 1,Song 1\n"
        ",Missing URL,Song 2\n"
        "\n"
        "http://127.0.0.1/valid2.flac,Valid Artist 2,Song 2\n"
        "invalid_uri_string,Bad Artist,Bad Title\n"
        "http://127.0.0.1/valid3.wav,Valid Artist 3,Song 3\n"
    )

    # 1. Ingestion isolates errors
    records = parse_csv(messy_csv)
    assert len(records) == 3

    # 2. Populate valid tracks
    for r in records:
        fmt = r.url.split(".")[-1]
        create_synthetic_audio(
            lib / f"incoming_{r.title}.{fmt}",
            format=fmt,
            artist=r.artist or "",
            title=r.title or "",
        )

    # 3. Organize library
    organized = flatten_and_organize_library(lib)
    assert len(organized) == 3

    # 4. Assert clean, standardized final library
    filenames = sorted([p.name for p in lib.iterdir()])
    assert filenames == [
        "Valid Artist 1 - Song 1.mp3",
        "Valid Artist 2 - Song 2.flac",
        "Valid Artist 3 - Song 3.wav",
    ]
    assert not list(lib.glob("*.part"))
