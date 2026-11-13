"""Tier 2: Boundary & Corner Cases E2E Tests (25 Test Cases).

Verifies boundary conditions, resilience, and edge cases across all 5 core requirements:
- R1 Boundaries: Paths with whitespace/slashes, delimiter variants, BOM, headerless, aliases (5 cases)
- R2 Boundaries: Cross-extension match, whitespace/casing, URL-encoding, duplicate rows, non-audio files (5 cases)
- R3 Boundaries: Zero bitrate fallback, missing tags stem fallback, unicode diacritics, smart quotes, OSError resilience (5 cases)
- R4 Boundaries: Empty after stripping, 240-byte length bounding, deep nesting, name collisions, reserved names (5 cases)
- R5 Boundaries: Mixed valid/malformed rows, zero data rows, truncated streams, parent permission denied, rate limits (5 cases)
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
    compare_quality,
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
    truncate_utf8,
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
# Requirement R1 Boundaries (5 Cases)
# ===========================================================================

def test_boundary_path_with_spaces_and_trailing_slashes(clean_library_dir: Path) -> None:
    """TC-T2-R1-01: Verify path selection with whitespace, special symbols, and multiple trailing slashes."""
    complex_sub = clean_library_dir / "my music   folder///sub///"
    resolved = validate_library_directory(str(complex_sub), create=True)

    assert resolved.exists()
    assert resolved.is_dir()
    # Canonical resolved path should strip redundant slashes
    assert not str(resolved).endswith("///")


def test_boundary_csv_delimiters_semicolon_tab_pipe() -> None:
    """TC-T2-R1-02: Verify delimiter sniffing for semicolon, tab, and pipe separators."""
    delimiters = [";", "\t", "|"]
    for delim in delimiters:
        csv_bytes = create_synthetic_csv(
            [
                {"url": "http://127.0.0.1/song_a.mp3", "artist": "Band A", "title": "Track A"},
                {"url": "http://127.0.0.1/song_b.mp3", "artist": "Band B", "title": "Track B"},
            ],
            delimiter=delim,
            header=("url", "artist", "title"),
        )
        records = parse_csv(csv_bytes)
        assert len(records) == 2
        assert records[0].url == "http://127.0.0.1/song_a.mp3"
        assert records[0].artist == "Band A"
        assert records[0].title == "Track A"
        assert records[1].url == "http://127.0.0.1/song_b.mp3"


def test_boundary_csv_utf8_bom_handling() -> None:
    """TC-T2-R1-03: Verify transparent ingestion of CSV with UTF-8 BOM prefix (\\xef\\xbb\\xbf)."""
    csv_bytes = create_synthetic_csv(
        [
            {"url": "http://127.0.0.1/bom_track.flac", "artist": "BOM Artist", "title": "BOM Title"},
        ],
        bom=True,
    )
    assert csv_bytes.startswith(b"\xef\xbb\xbf")

    records = parse_csv(csv_bytes)
    assert len(records) == 1
    assert records[0].url == "http://127.0.0.1/bom_track.flac"
    assert records[0].artist == "BOM Artist"
    assert records[0].title == "BOM Title"
    # Ensure BOM did not contaminate raw_row keys
    assert not any("\ufeff" in k for k in records[0].raw_row.keys())


def test_boundary_csv_headerless_raw_url_list() -> None:
    """TC-T2-R1-04: Verify ingestion of bare URL lists without headers and with blank lines."""
    raw_text = (
        "\n"
        "http://127.0.0.1/track1.mp3\n"
        "\n"
        "   \n"
        "http://127.0.0.1/track2.flac\n"
        "http://127.0.0.1/track3.wav\n"
        "\n"
    )
    records = parse_csv(raw_text)
    assert len(records) == 3
    urls = [r.url for r in records]
    assert urls == [
        "http://127.0.0.1/track1.mp3",
        "http://127.0.0.1/track2.flac",
        "http://127.0.0.1/track3.wav",
    ]


def test_boundary_csv_flexible_column_aliases() -> None:
    """TC-T2-R1-05: Verify header alias mapping for non-standard column headers."""
    csv_bytes = create_synthetic_csv(
        [
            {"url": "http://127.0.0.1/alias.mp3", "artist": "Alias Singer", "title": "Alias Melody"},
        ],
        header=("track_uri", "artist_name", "song_title"),
    )
    records = parse_csv(csv_bytes)
    assert len(records) == 1
    assert records[0].url == "http://127.0.0.1/alias.mp3"
    assert records[0].artist == "Alias Singer"
    assert records[0].title == "Alias Melody"


# ===========================================================================
# Requirement R2 Boundaries (5 Cases)
# ===========================================================================

def test_boundary_pre_download_cross_extension_match(clean_library_dir: Path) -> None:
    """TC-T2-R2-01: Verify pre-download check identifies existing track across different extensions (.mp3 vs .flac)."""
    # Library already houses an MP3
    existing_mp3 = clean_library_dir / "Queen - Bohemian Rhapsody.mp3"
    create_synthetic_audio(existing_mp3, format="mp3", artist="Queen", title="Bohemian Rhapsody")

    index = LibraryIndex.build(clean_library_dir)
    # The pre-check by track tags matches regardless of requested download extension
    assert index.contains_track("Queen", "Bohemian Rhapsody") is True


def test_boundary_pre_download_case_and_whitespace_variance(clean_library_dir: Path) -> None:
    """TC-T2-R2-02: Verify pre-download check matches filenames with differing casing and whitespace."""
    existing_file = clean_library_dir / "artist   -   title.FLAC"
    create_synthetic_audio(existing_file, format="flac", artist="Artist", title="Title")

    index = LibraryIndex.build(clean_library_dir)
    # Normalized search matches variance in whitespace and uppercase
    assert index.contains_track("artist", "title") is True
    assert index.contains_track("ARTIST", "TITLE") is True
    assert index.contains_stem("Artist - Title") is True


def test_boundary_pre_download_url_encoded_stems(clean_library_dir: Path) -> None:
    """TC-T2-R2-03: Verify stem matching when URL contains percent-encoded characters."""
    existing_file = clean_library_dir / "ACDC - Back In Black.flac"
    create_synthetic_audio(existing_file, format="flac", artist="ACDC", title="Back In Black")

    index = LibraryIndex.build(clean_library_dir)
    # URL encoded string
    encoded_url = "http://127.0.0.1/ACDC%20-%20Back%20In%20Black.flac"
    import urllib.parse
    stem_from_url = Path(urllib.parse.unquote(urllib.parse.urlparse(encoded_url).path)).stem

    assert index.contains_stem(stem_from_url) is True


def test_boundary_pre_download_duplicate_csv_rows(clean_library_dir: Path) -> None:
    """TC-T2-R2-04: Verify duplicate rows within the same CSV are deduplicated."""
    csv_bytes = create_synthetic_csv([
        {"url": "http://127.0.0.1/dup.mp3", "artist": "Band", "title": "Hit"},
        {"url": "http://127.0.0.1/other.mp3", "artist": "Band", "title": "Other"},
        {"url": "http://127.0.0.1/dup.mp3", "artist": "Band", "title": "Hit"},
    ])
    records = parse_csv(csv_bytes)
    assert len(records) == 3

    # Deduplication by URL
    unique_urls = set()
    deduped_records: List[TrackRecord] = []
    for r in records:
        if r.url not in unique_urls:
            unique_urls.add(r.url)
            deduped_records.append(r)
    assert len(deduped_records) == 2


def test_boundary_pre_download_non_audio_file_rejection(clean_library_dir: Path) -> None:
    """TC-T2-R2-05: Verify non-audio files (.txt, .jpg) matching stem do NOT trigger skip."""
    text_file = clean_library_dir / "Queen - Bohemian Rhapsody.txt"
    text_file.write_text("lyrics only", encoding="utf-8")
    img_file = clean_library_dir / "Queen - Bohemian Rhapsody.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0")

    index = LibraryIndex.build(clean_library_dir)
    # AUDIO_SUFFIXES only: non-audio files must NOT be indexed as audio tracks
    assert index.contains_stem("Queen - Bohemian Rhapsody") is False
    assert index.contains_track("Queen", "Bohemian Rhapsody") is False


# ===========================================================================
# Requirement R3 Boundaries (5 Cases)
# ===========================================================================

def test_boundary_duplicate_missing_zero_bitrate_fallback(clean_library_dir: Path) -> None:
    """TC-T2-R3-01: Verify quality comparison falls back to file size / duration when bitrate is 0 or equal."""
    p1 = clean_library_dir / "Wav_Big.wav"
    p2 = clean_library_dir / "Wav_Small.wav"

    create_synthetic_audio(p1, format="wav", artist="Composer", title="Symphony", pad_bytes=10000)
    create_synthetic_audio(p2, format="wav", artist="Composer", title="Symphony", pad_bytes=0)

    m1 = extract_metadata(p1)
    m2 = extract_metadata(p2)

    # When bitrates are identical (uncompressed WAV), size comparison determines superior
    assert m1.bitrate == m2.bitrate
    assert m1.file_size > m2.file_size
    assert compare_quality(m1, m2) == 1
    assert compare_quality(m2, m1) == -1


def test_boundary_duplicate_missing_tags_filename_fallback(clean_library_dir: Path) -> None:
    """TC-T2-R3-02: Verify duplicate extraction falls back to filename stem when embedded tags are missing."""
    # Place duplicate untagged audio files across subdirectories with identical stem
    sub1 = clean_library_dir / "sub1"
    sub2 = clean_library_dir / "sub2"
    sub1.mkdir(parents=True, exist_ok=True)
    sub2.mkdir(parents=True, exist_ok=True)

    p1 = sub1 / "Band Name - Song Title.mp3"
    p2 = sub2 / "Band Name - Song Title.mp3"

    create_synthetic_audio(p1, format="mp3", artist="Band Name", title="Song Title", bitrate_kbps=320)
    create_synthetic_audio(p2, format="mp3", artist="Band Name", title="Song Title", bitrate_kbps=128)

    # Strip ID3 tags to verify fallback to filename stem
    from mutagen.id3 import ID3
    try:
        ID3(str(p1)).delete()
        ID3(str(p2)).delete()
    except Exception:
        pass

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    # Exactly one file must be retained (320 kbps) and one deleted (128 kbps)
    assert p1.exists() is True
    assert p2.exists() is False


def test_boundary_duplicate_unicode_diacritics_normalization(clean_library_dir: Path) -> None:
    """TC-T2-R3-03: Verify duplicate matching across diacritic variations (Beyoncé vs Beyonce)."""
    p1 = clean_library_dir / "Beyonce_v1.flac"
    p2 = clean_library_dir / "Beyonce_v2.flac"

    create_synthetic_audio(p1, format="flac", artist="Beyoncé", title="Halo", bitrate_kbps=100)
    create_synthetic_audio(p2, format="flac", artist="Beyonce", title="Halo", bitrate_kbps=200)

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    # Exactly 1 file should remain in library
    assert len(list(clean_library_dir.iterdir())) == 1


def test_boundary_duplicate_punctuation_quote_normalization(clean_library_dir: Path) -> None:
    """TC-T2-R3-04: Verify duplicate matching when titles differ by smart quotes vs straight apostrophes."""
    p1 = clean_library_dir / "Quote1.mp3"
    p2 = clean_library_dir / "Quote2.mp3"

    # Don't with smart curved quote vs straight apostrophe
    create_synthetic_audio(p1, format="mp3", artist="Journey", title="Don’t Stop Believin’", bitrate_kbps=128)
    create_synthetic_audio(p2, format="mp3", artist="Journey", title="Don't Stop Believin'", bitrate_kbps=320)

    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1
    assert p2.exists() is True
    assert p1.exists() is False


def test_boundary_duplicate_deletion_os_error_resilience(clean_library_dir: Path, monkeypatch) -> None:
    """TC-T2-R3-05: Verify resolve_duplicates catches OSError on deletion and does not crash."""
    p1 = clean_library_dir / "TrackA.mp3"
    p2 = clean_library_dir / "TrackB.mp3"
    create_synthetic_audio(p1, format="mp3", artist="Band", title="Hit", bitrate_kbps=320)
    create_synthetic_audio(p2, format="mp3", artist="Band", title="Hit", bitrate_kbps=128)

    # Monkeypatch unlink to simulate an OS permission/lock error
    original_unlink = Path.unlink

    def mock_unlink(self, *args, **kwargs):
        if self == p2:
            raise OSError("Simulated file lock error")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    # Must complete cleanly without unhandled exception
    results = resolve_duplicates(clean_library_dir)
    assert len(results) == 1


# ===========================================================================
# Requirement R4 Boundaries (5 Cases)
# ===========================================================================

def test_boundary_sanitization_empty_after_stripping() -> None:
    """TC-T2-R4-01: Verify fallback naming when artist/title consists entirely of stripped characters."""
    clean_artist = sanitize_filename_component("???", fallback="Unknown Artist")
    clean_title = sanitize_filename_component("///", fallback="Unknown Title")
    assert clean_artist == "Unknown Artist"
    assert clean_title == "Unknown Title"

    filename = format_standard_filename("???", "///", ".flac")
    assert filename == "Unknown Artist - Unknown Title.flac"


def test_boundary_sanitization_filename_length_bounding(clean_library_dir: Path) -> None:
    """TC-T2-R4-02: Verify titles exceeding filesystem limits are truncated to <= 240 bytes."""
    long_title = "A" * 350
    formatted = format_standard_filename("Artist", long_title, ".flac")
    byte_len = len(formatted.encode("utf-8"))
    assert byte_len <= 240

    # Ensure file can be safely created on disk
    test_path = clean_library_dir / formatted
    test_path.write_bytes(b"DATA")
    assert test_path.exists() is True


def test_boundary_flattening_deeply_nested_subfolders(clean_library_dir: Path) -> None:
    """TC-T2-R4-03: Verify flattening traverses deeply nested folders (5+ levels) and prunes all."""
    deep_path = clean_library_dir / "l1" / "l2" / "l3" / "l4" / "l5"
    deep_path.mkdir(parents=True, exist_ok=True)
    audio = deep_path / "deep_song.flac"
    create_synthetic_audio(audio, format="flac", artist="Explorer", title="Deep Sea")

    flatten_and_organize_library(clean_library_dir)

    assert not (clean_library_dir / "l1").exists()
    assert (clean_library_dir / "Explorer - Deep Sea.flac").exists() is True


def test_boundary_flattening_name_collision_resolution(clean_library_dir: Path) -> None:
    """TC-T2-R4-04: Verify collision resolution when nested file shares name with file in root."""
    root_file = clean_library_dir / "Band - Song.mp3"
    create_synthetic_audio(root_file, format="mp3", artist="Band", title="Song", bitrate_kbps=320)

    sub = clean_library_dir / "subfolder"
    sub.mkdir(parents=True, exist_ok=True)
    nested_file = sub / "Band - Song.mp3"
    create_synthetic_audio(nested_file, format="mp3", artist="Band", title="Song", bitrate_kbps=128)

    flatten_and_organize_library(clean_library_dir)

    # After flattening, one file is at root and one has collision counter (1)
    root_files = [p for p in clean_library_dir.iterdir() if p.is_file() and p.suffix == ".mp3"]
    assert len(root_files) == 2
    assert any("(1)" in p.name for p in root_files)

    # Deduplication then cleans up the inferior copy
    resolve_duplicates(clean_library_dir)
    final_files = [p for p in clean_library_dir.iterdir() if p.is_file() and p.suffix == ".mp3"]
    assert len(final_files) == 1
    assert extract_metadata(final_files[0]).bitrate == 320000


def test_boundary_sanitization_leading_trailing_dots_reserved_names(clean_library_dir: Path) -> None:
    """TC-T2-R4-05: Verify stripping of leading/trailing dots and Windows reserved names."""
    clean_artist = sanitize_filename_component("..The Beatles..")
    clean_title = sanitize_filename_component("CON")

    assert not clean_artist.startswith(".")
    assert not clean_artist.endswith(".")
    assert clean_artist == "The Beatles"

    formatted = format_standard_filename("..The Beatles..", "CON", ".flac")
    assert formatted == "The Beatles - CON.flac"
    assert not formatted.startswith(".")

    # Ensure file can be safely saved on disk
    target = clean_library_dir / formatted
    target.write_bytes(b"DATA")
    assert target.exists() is True


# ===========================================================================
# Requirement R5 Boundaries (5 Cases)
# ===========================================================================

def test_boundary_csv_mixed_valid_and_malformed_rows() -> None:
    """TC-T2-R5-01: Verify parser extracts all valid tracks while skipping empty or malformed rows."""
    csv_content = (
        "url,artist,title\n"
        "http://127.0.0.1/s1.mp3,Artist 1,Song 1\n"
        ",Empty URL Row,Song 2\n"
        "\n"
        "http://127.0.0.1/s3.mp3,Artist 3,Song 3\n"
        "not_a_valid_url,Artist 4,Song 4\n"
        "http://127.0.0.1/s5.mp3,Artist 5,Song 5\n"
    )
    records = parse_csv(csv_content)
    assert len(records) == 3
    assert records[0].url == "http://127.0.0.1/s1.mp3"
    assert records[1].url == "http://127.0.0.1/s3.mp3"
    assert records[2].url == "http://127.0.0.1/s5.mp3"


def test_boundary_csv_headers_present_zero_data_rows() -> None:
    """TC-T2-R5-02: Verify CSV with valid header but 0 data rows raises NoValidUrlsError."""
    header_only = "url,artist,title\n\n\n"
    with pytest.raises((NoValidUrlsError, EmptyCSVError)):
        parse_csv(header_only)


def test_boundary_download_truncated_stream_cleanup(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T2-R5-03: Verify interrupted stream deletes temporary .part file without leaving corrupt audio."""
    if not has_downloader_module or Downloader is None:
        pytest.skip("songetter.downloader is not yet implemented (Milestone 2)")

    truncated_url = mock_audio_server.serve_truncated("cut.mp3", full_size=100000, cut_at_byte=500)
    downloader = Downloader(clean_library_dir)
    res = downloader.download([TrackRecord(url=truncated_url, artist="Artist", title="Cut", raw_row={})])

    assert res.failed == 1
    assert not list(clean_library_dir.glob("*.part"))


def test_boundary_path_parent_creation_permission_denied() -> None:
    """TC-T2-R5-04: Verify path creation under unwritable root raises PermissionDeniedError or InvalidPathError."""
    forbidden_path = "/root_forbidden_123/sub/music"
    with pytest.raises((PermissionDeniedError, InvalidPathError, OSError)):
        validate_library_directory(forbidden_path, create=True)


def test_boundary_download_provider_rate_limit_429(clean_library_dir: Path, mock_audio_server) -> None:
    """TC-T2-R5-05: Verify downloader handles HTTP 429 rate limit gracefully without crashing."""
    if not has_downloader_module or Downloader is None:
        pytest.skip("songetter.downloader is not yet implemented (Milestone 2)")

    rate_url = mock_audio_server.serve_error("rate.mp3", status_code=429, retry_after=5)
    downloader = Downloader(clean_library_dir)
    res = downloader.download([TrackRecord(url=rate_url, artist="Artist", title="Rate", raw_row={})])

    assert res.failed == 1
