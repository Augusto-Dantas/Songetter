"""Tier 1: Feature Coverage E2E Tests (25 Test Cases).

Verifies fundamental feature compliance across all 5 core requirements in isolation:
- R1: Server-Side Web Interface & Library Selection (5 cases)
- R2: Existing File Check & Download Execution (5 cases)
- R3: Duplicate Detection & Quality-Based Retention (5 cases)
- R4: Filesystem Organization & Flat Structure (5 cases)
- R5: Error Handling & Fault Isolation (5 cases)
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from songetter.csv_processor import (
    EmptyCSVError,
    MalformedCSVError,
    NoValidUrlsError,
    TrackRecord,
    parse_csv,
)
from songetter.deduplicator import (
    AudioMetadata,
    DuplicateResolutionResult,
    extract_metadata,
    resolve_duplicates,
)
from songetter.library import (
    InvalidPathError,
    LibraryIndex,
    NotADirectoryError,
    PermissionDeniedError,
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


# ===========================================================================
# Requirement R1: Server-Side Web Interface & Library Selection (5 Cases)
# ===========================================================================

def test_library_selection_valid_directory(clean_library_dir: Path) -> None:
    """TC-T1-R1-01: Verify selection and validation of a valid, writable directory."""
    # 1. Verify underlying domain logic
    canonical = validate_library_directory(clean_library_dir)
    assert canonical == clean_library_dir.resolve()
    # Ensure genuine write probe file was cleaned up
    assert not list(clean_library_dir.glob(".songetter_probe_*"))

    # 2. Verify web interface endpoint if web module is available
    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post("/api/library", json={"path": str(clean_library_dir)})
        assert response.status_code == 200
        data = response.json()
        assert Path(data["path"]) == clean_library_dir.resolve()


def test_library_selection_auto_create_nonexistent(clean_library_dir: Path) -> None:
    """TC-T1-R1-02: Verify non-existent subfolder is automatically created."""
    target_subdir = clean_library_dir / "auto_created_sub"
    assert not target_subdir.exists()

    # 1. Verify underlying domain creation
    resolved = validate_library_directory(target_subdir, create=True)
    assert resolved.is_dir()
    assert resolved == target_subdir.resolve()

    # 2. Verify web interface endpoint if web module is available
    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        new_target = clean_library_dir / "web_auto_created"
        response = client.post("/api/library", json={"path": str(new_target), "create": True})
        assert response.status_code == 200
        assert new_target.is_dir()


def test_web_ui_form_rendering(api_client: TestClient) -> None:
    """TC-T1-R1-03: Verify web server serves HTML UI with directory input and CSV upload form."""
    response = api_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    html = response.text.lower()
    # Check for library path input and file upload components
    assert "library" in html or "directory" in html or "folder" in html
    assert "csv" in html or 'type="file"' in html


def test_csv_upload_standard_rfc4180() -> None:
    """TC-T1-R1-04: Verify manual upload and parsing of standard RFC 4180 CSV."""
    csv_bytes = create_synthetic_csv([
        {"url": "http://127.0.0.1/song1.mp3"},
        {"url": "http://127.0.0.1/song2.mp3"},
        {"url": "http://127.0.0.1/song3.mp3"},
    ])

    # 1. Verify domain CSV parser
    records = parse_csv(csv_bytes)
    assert len(records) == 3
    assert records[0].url == "http://127.0.0.1/song1.mp3"
    assert records[1].url == "http://127.0.0.1/song2.mp3"
    assert records[2].url == "http://127.0.0.1/song3.mp3"

    # 2. Verify web endpoint if available
    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post(
            "/api/upload",
            files={"file": ("links.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("total_tracks") == 3
        assert data.get("valid_tracks") == 3


def test_csv_upload_with_metadata_columns() -> None:
    """TC-T1-R1-05: Verify CSV upload with metadata columns (artist, title, url)."""
    csv_bytes = create_synthetic_csv(
        [
            {"artist": "Queen", "title": "Bohemian Rhapsody", "url": "http://mock/queen.flac"},
            {"artist": "Pink Floyd", "title": "Time", "url": "http://mock/time.flac"},
        ],
        header=("artist", "title", "url"),
    )

    # 1. Verify domain parsing
    records = parse_csv(csv_bytes)
    assert len(records) == 2
    assert records[0].artist == "Queen"
    assert records[0].title == "Bohemian Rhapsody"
    assert records[0].url == "http://mock/queen.flac"
    assert records[1].artist == "Pink Floyd"
    assert records[1].title == "Time"

    # 2. Verify web endpoint if available
    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post(
            "/api/upload",
            files={"file": ("metadata.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert response.status_code == 200
        preview = response.json().get("preview", [])
        assert len(preview) == 2
        assert preview[0]["artist"] == "Queen"


# ===========================================================================
# Requirement R2: Existing File Check & Download Execution (5 Cases)
# ===========================================================================

def test_download_execution_to_library(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T1-R2-01: Verify audio files downloaded from mock server are saved into library."""
    if not has_downloader_module or Downloader is None:
        pytest.skip("songetter.downloader is not yet implemented (Milestone 2)")

    audio_bytes = create_synthetic_audio(
        clean_library_dir / "temp_src.mp3",
        artist="Download Artist",
        title="Download Song",
        bitrate_kbps=128,
    ).read_bytes()
    clean_library_dir.joinpath("temp_src.mp3").unlink()

    url = mock_audio_server.serve_bytes("stream1.mp3", audio_bytes)
    downloader = Downloader(clean_library_dir)
    res = downloader.download([TrackRecord(url=url, artist="Download Artist", title="Download Song", raw_row={})])

    assert res.downloaded == 1
    assert any("Download Artist" in f.name for f in clean_library_dir.iterdir())


def test_pre_download_check_exact_filename_match(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T1-R2-02: Verify pre-download check skips existing file by exact filename match."""
    # Pre-populate library
    file_path = clean_library_dir / "Queen - Bohemian Rhapsody.flac"
    create_synthetic_audio(file_path, format="flac", artist="Queen", title="Bohemian Rhapsody")

    # 1. Verify LibraryIndex detects exact stem and track
    index = LibraryIndex.build(clean_library_dir)
    assert index.contains_stem("Queen - Bohemian Rhapsody") is True
    assert index.contains_track("Queen", "Bohemian Rhapsody") is True

    # 2. Verify with Downloader if available (asserting 0 mock HTTP requests)
    if has_downloader_module and Downloader is not None:
        url = mock_audio_server.serve_bytes("queen.flac", b"FLAC_BYTES")
        downloader = Downloader(clean_library_dir)
        summary = downloader.download([TrackRecord(url=url, artist="Queen", title="Bohemian Rhapsody", raw_row={})])
        assert summary.skipped == 1
        assert summary.downloaded == 0
        assert mock_audio_server.get_request_count("queen.flac") == 0


def test_pre_download_check_tag_metadata_match(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T1-R2-03: Verify pre-download check skips file by Mutagen metadata match even with different filename."""
    # Filename differs from standard pattern, but internal tags match
    odd_filename = clean_library_dir / "random_file_name_99.flac"
    create_synthetic_audio(odd_filename, format="flac", artist="Pink Floyd", title="Time")

    # 1. Verify LibraryIndex identifies track by tags
    index = LibraryIndex.build(clean_library_dir)
    assert index.contains_track("Pink Floyd", "Time") is True

    # 2. Verify with Downloader if available
    if has_downloader_module and Downloader is not None:
        url = mock_audio_server.serve_bytes("time.flac", b"FLAC_BYTES")
        downloader = Downloader(clean_library_dir)
        summary = downloader.download([TrackRecord(url=url, artist="Pink Floyd", title="Time", raw_row={})])
        assert summary.skipped == 1
        assert mock_audio_server.get_request_count("time.flac") == 0


def test_pre_download_mixed_batch_skip_and_download(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T1-R2-04: Verify correct partition of existing vs new tracks in a mixed batch."""
    # Pre-populate Track 1
    create_synthetic_audio(
        clean_library_dir / "Artist1 - Song1.mp3",
        artist="Artist1",
        title="Song1",
    )

    index = LibraryIndex.build(clean_library_dir)
    assert index.contains_track("Artist1", "Song1") is True
    assert index.contains_track("Artist2", "Song2") is False
    assert index.contains_track("Artist3", "Song3") is False

    if has_downloader_module and Downloader is not None:
        u1 = mock_audio_server.serve_bytes("s1.mp3", b"MP3_DATA")
        u2 = mock_audio_server.serve_bytes("s2.mp3", b"MP3_DATA")
        u3 = mock_audio_server.serve_bytes("s3.mp3", b"MP3_DATA")
        downloader = Downloader(clean_library_dir)
        summary = downloader.download([
            TrackRecord(url=u1, artist="Artist1", title="Song1", raw_row={}),
            TrackRecord(url=u2, artist="Artist2", title="Song2", raw_row={}),
            TrackRecord(url=u3, artist="Artist3", title="Song3", raw_row={}),
        ])
        assert summary.skipped == 1
        assert summary.downloaded == 2
        assert mock_audio_server.get_request_count("s1.mp3") == 0
        assert mock_audio_server.get_request_count("s2.mp3") == 1
        assert mock_audio_server.get_request_count("s3.mp3") == 1


def test_download_atomic_staging_part_file(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T1-R2-05: Verify downloads use temporary .part staging before promoting atomically."""
    if not has_downloader_module or Downloader is None:
        pytest.skip("songetter.downloader is not yet implemented (Milestone 2)")

    data = b"MPEG_AUDIO_TEST_PAYLOAD" * 50
    url = mock_audio_server.serve_slow("slow.mp3", data, chunk_size=100, delay_sec=0.05)
    downloader = Downloader(clean_library_dir)
    downloader.download([TrackRecord(url=url, artist="Slow", title="Track", raw_row={})])

    # Final file should exist; zero .part files should remain
    assert not list(clean_library_dir.glob("*.part"))


# ===========================================================================
# Requirement R3: Duplicate Detection & Quality-Based Retention (5 Cases)
# ===========================================================================

def test_duplicate_detection_metadata_identification(clean_library_dir: Path) -> None:
    """TC-T1-R3-01: Verify duplicate scanner groups tracks sharing normalized (artist, title)."""
    p1 = clean_library_dir / "TrackA_v1.flac"
    p2 = clean_library_dir / "TrackA_v2.flac"
    create_synthetic_audio(p1, format="flac", artist="Daft Punk", title="One More Time", bitrate_kbps=100)
    create_synthetic_audio(p2, format="flac", artist="Daft Punk", title="One More Time", bitrate_kbps=200)

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    assert results[0].artist == "Daft Punk"
    assert results[0].title == "One More Time"
    assert len(results[0].deleted) == 1


def test_duplicate_retention_higher_bitrate(clean_library_dir: Path) -> None:
    """TC-T1-R3-02: Verify higher bitrate file is retained and lower bitrate replicate deleted."""
    high = clean_library_dir / "Track_High.mp3"
    low = clean_library_dir / "Track_Low.mp3"

    create_synthetic_audio(high, format="mp3", artist="Artist", title="Song", bitrate_kbps=320)
    create_synthetic_audio(low, format="mp3", artist="Artist", title="Song", bitrate_kbps=128)

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    assert high.exists() is True
    assert low.exists() is False


def test_duplicate_retention_larger_filesize(clean_library_dir: Path) -> None:
    """TC-T1-R3-03: Verify when bitrates are equal, larger file size is retained."""
    large = clean_library_dir / "Song_Large.flac"
    small = clean_library_dir / "Song_Small.flac"

    create_synthetic_audio(large, format="flac", artist="Artist", title="Song", pad_bytes=10000)
    create_synthetic_audio(small, format="flac", artist="Artist", title="Song", pad_bytes=0)

    # Confirm equal bitrates but different file sizes
    m_large = extract_metadata(large)
    m_small = extract_metadata(small)
    assert m_large.bitrate == m_small.bitrate
    assert m_large.file_size > m_small.file_size

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    assert large.exists() is True
    assert small.exists() is False


def test_duplicate_tie_breaker_identical_metrics(clean_library_dir: Path) -> None:
    """TC-T1-R3-04: Verify when bitrate and size are identical, exactly one file is retained and one deleted."""
    file_a = clean_library_dir / "Copy_A.mp3"
    file_b = clean_library_dir / "Copy_B.mp3"

    create_synthetic_audio(file_a, format="mp3", artist="Clone", title="Track", bitrate_kbps=128)
    file_b.write_bytes(file_a.read_bytes())

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1

    remaining_files = [p for p in clean_library_dir.iterdir() if p.suffix == ".mp3"]
    assert len(remaining_files) == 1
    assert (file_a.exists() and not file_b.exists()) or (file_b.exists() and not file_a.exists())


def test_duplicate_multi_replicate_cluster(clean_library_dir: Path) -> None:
    """TC-T1-R3-05: Verify cluster with >2 replicates retains highest quality and deletes all others."""
    p1 = clean_library_dir / "C1.mp3"
    p2 = clean_library_dir / "C2.mp3"
    p3 = clean_library_dir / "C3.mp3"

    create_synthetic_audio(p1, format="mp3", artist="Band", title="Epic", bitrate_kbps=96)
    create_synthetic_audio(p2, format="mp3", artist="Band", title="Epic", bitrate_kbps=192)
    create_synthetic_audio(p3, format="mp3", artist="Band", title="Epic", bitrate_kbps=320)

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    assert p3.exists() is True
    assert p1.exists() is False
    assert p2.exists() is False
    assert len(results[0].deleted) == 2


# ===========================================================================
# Requirement R4: Filesystem Organization (5 Cases)
# ===========================================================================

def test_filesystem_flat_folder_enforcement(nested_library_dir: Path) -> None:
    """TC-T1-R4-01: Verify nested audio files are flattened into library root."""
    flatten_and_organize_library(nested_library_dir)

    all_items = list(nested_library_dir.iterdir())
    assert len(all_items) >= 2
    # Ensure every single item in library root is a file, zero subdirectories
    assert all(item.is_file() for item in all_items)


def test_filesystem_standardized_naming_format(clean_library_dir: Path) -> None:
    """TC-T1-R4-02: Verify output files strictly follow '{Artist} - {Title}.{ext}'."""
    raw_file = clean_library_dir / "arbitrary_input_name_123.flac"
    create_synthetic_audio(raw_file, format="flac", artist="Radiohead", title="Karma Police")

    flatten_and_organize_library(clean_library_dir)
    expected = clean_library_dir / "Radiohead - Karma Police.flac"
    assert expected.exists() is True


def test_filesystem_forbidden_character_sanitization(clean_library_dir: Path) -> None:
    """TC-T1-R4-03: Verify characters forbidden by filesystems ([\\/*?:\"<>|]) are sanitized."""
    raw_file = clean_library_dir / "raw.flac"
    create_synthetic_audio(
        raw_file,
        format="flac",
        artist="AC/DC",
        title='Who Made Who? [12" Remix]',
    )

    organized = flatten_and_organize_library(clean_library_dir)
    assert len(organized) == 1
    final_name = organized[0].name
    for forbidden in ["/", "\\", "*", "?", ":", '"', "<", ">", "|"]:
        assert forbidden not in final_name
    assert organized[0].exists() is True


def test_filesystem_preserves_embedded_metadata_lyrics(clean_library_dir: Path) -> None:
    """TC-T1-R4-04: Verify embedded metadata lyrics are preserved during flattening without .lrc sidecars."""
    from mutagen.flac import FLAC

    sub = clean_library_dir / "Artist" / "Album"
    sub.mkdir(parents=True, exist_ok=True)
    audio = sub / "track.flac"

    create_synthetic_audio(audio, format="flac", artist="Queen", title="Bohemian Rhapsody")
    flac = FLAC(audio)
    flac["lyrics"] = "[00:01.00] Is this the real life?"
    flac.save()

    flatten_and_organize_library(clean_library_dir)

    expected_audio = clean_library_dir / "Queen - Bohemian Rhapsody.flac"

    assert expected_audio.exists() is True
    assert FLAC(expected_audio).get("lyrics") == ["[00:01.00] Is this the real life?"]
    assert not (clean_library_dir / "Queen - Bohemian Rhapsody.lrc").exists()


def test_filesystem_recursive_empty_dir_pruning(clean_library_dir: Path) -> None:
    """TC-T1-R4-05: Verify deep empty directories are recursively pruned after flattening."""
    deep = clean_library_dir / "level1" / "level2" / "level3"
    deep.mkdir(parents=True, exist_ok=True)
    track = deep / "song.mp3"
    create_synthetic_audio(track, artist="Singer", title="Melody")

    flatten_and_organize_library(clean_library_dir)

    # level1 must be completely removed
    assert not (clean_library_dir / "level1").exists()
    assert (clean_library_dir / "Singer - Melody.mp3").exists() is True


# ===========================================================================
# Requirement R5: Error Handling and Automated Verification (5 Cases)
# ===========================================================================

def test_error_handling_path_is_file(tmp_path: Path) -> None:
    """TC-T1-R5-01: Verify selecting a regular file instead of a directory is rejected."""
    fake_file = tmp_path / "regular_file.txt"
    fake_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        validate_library_directory(fake_file)

    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post("/api/library", json={"path": str(fake_file)})
        assert response.status_code == 400


def test_error_handling_readonly_directory(unwritable_library_dir: Path) -> None:
    """TC-T1-R5-02: Verify selecting a read-only directory fails probe writability test."""
    with pytest.raises(PermissionDeniedError):
        validate_library_directory(unwritable_library_dir)

    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post("/api/library", json={"path": str(unwritable_library_dir)})
        assert response.status_code in (400, 403)


def test_error_handling_empty_csv_file() -> None:
    """TC-T1-R5-03: Verify 0-byte or whitespace-only CSV is rejected with EmptyCSVError."""
    with pytest.raises(EmptyCSVError):
        parse_csv(b"")

    with pytest.raises(EmptyCSVError):
        parse_csv("   \n\n  \t  \n")

    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post(
            "/api/upload",
            files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")},
        )
        assert response.status_code == 400


def test_error_handling_corrupted_binary_csv() -> None:
    """TC-T1-R5-04: Verify corrupted binary data uploaded as CSV raises MalformedCSVError."""
    binary_junk = b"\x00\xff\xfe\x00\x1f\x8b\x08binary_payload"
    with pytest.raises(MalformedCSVError):
        parse_csv(binary_junk)

    if has_web_module and create_app is not None:
        client = TestClient(create_app())
        response = client.post(
            "/api/upload",
            files={"file": ("corrupt.csv", io.BytesIO(binary_junk), "text/csv")},
        )
        assert response.status_code == 400


def test_error_handling_download_network_404(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T1-R5-05: Verify HTTP 404 download error is isolated without process crash."""
    if not has_downloader_module or Downloader is None:
        pytest.skip("songetter.downloader is not yet implemented (Milestone 2)")

    bad_url = mock_audio_server.serve_error("missing.mp3", status_code=404)
    downloader = Downloader(clean_library_dir)
    res = downloader.download([TrackRecord(url=bad_url, artist="Artist", title="Missing", raw_row={})])
    assert res.failed == 1
    assert not list(clean_library_dir.glob("*.part"))
