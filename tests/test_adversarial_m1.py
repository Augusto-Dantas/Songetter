"""Adversarial stress and edge-case test harness for Songetter Milestone 1.

Targets:
- src/songetter/csv_processor.py
- src/songetter/library.py
- src/songetter/config.py
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
import soundfile as sf
import numpy as np
from mutagen.flac import FLAC

from songetter.config import (
    AUDIO_SUFFIXES,
    DEFAULT_CSV_DELIMITERS,
    PROBE_PREFIX,
)
from songetter.csv_processor import (
    CSVProcessorError,
    EmptyCSVError,
    MalformedCSVError,
    NoValidUrlsError,
    TrackRecord,
    parse_csv,
)
from songetter.deduplicator import normalize_text
from songetter.library import (
    InvalidPathError,
    LibraryError,
    LibraryIndex,
    NotADirectoryError,
    PermissionDeniedError,
    validate_library_directory,
)


def _generate_minimal_flac(path: Path, artist: str = "Test Artist", title: str = "Test Title") -> Path:
    """Generates an authentic tiny FLAC file with Vorbis tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 22050
    duration = 0.05
    n_samples = int(sample_rate * duration)
    data = 0.1 * np.sin(2 * np.pi * 440 * np.linspace(0, duration, n_samples, endpoint=False))
    sf.write(str(path), data, sample_rate, format="FLAC")
    audio = FLAC(path)
    audio["artist"] = artist
    audio["title"] = title
    audio.save()
    return path


# ============================================================================
# Section 1: Adversarial CSV Ingestion Tests
# ============================================================================

class TestAdversarialCSVIngestion:
    """Adversarial stress tests for csv_processor.py."""

    def test_embedded_newlines_in_quoted_fields(self):
        """RFC 4180 allows newlines inside double quotes for artist and title."""
        csv_data = (
            '"url","artist","title"\n'
            '"https://example.com/track1","Artist\nWith\nNewlines","Track\nTitle"\n'
            '"https://example.com/track2","Normal Artist","Normal Title"\n'
        )
        records = parse_csv(csv_data)
        assert len(records) == 2
        assert records[0].url == "https://example.com/track1"
        assert "Artist\nWith\nNewlines" == records[0].artist
        assert "Track\nTitle" == records[0].title
        assert records[1].url == "https://example.com/track2"

    def test_mixed_delimiters_across_rows(self):
        """Sniffer detects primary delimiter even if secondary delimiters appear in cells."""
        # Comma delimited with semicolon inside title
        csv_data = (
            'url,artist,title\n'
            'https://example.com/1,"Band; The Best","Song; Remix"\n'
            'https://example.com/2,"Another Band","Another Song"\n'
        )
        records = parse_csv(csv_data)
        assert len(records) == 2
        assert records[0].artist == "Band; The Best"
        assert records[0].title == "Song; Remix"

    def test_utf8_bom_variations(self):
        """UTF-8 BOM must be stripped cleanly for both bytes and str."""
        bom_bytes = b"\xef\xbb\xbfurl,artist,title\nhttps://example.com/1,Artist,Title\n"
        records_bytes = parse_csv(bom_bytes)
        assert len(records_bytes) == 1
        assert records_bytes[0].url == "https://example.com/1"

        bom_str = "\ufeffurl,artist,title\nhttps://example.com/1,Artist,Title\n"
        records_str = parse_csv(bom_str)
        assert len(records_str) == 1
        assert records_str[0].url == "https://example.com/1"

    def test_utf16_behavior(self):
        """Verify behavior with UTF-16LE and UTF-16BE bytes (contains null bytes)."""
        utf16_le = "url,artist,title\nhttps://example.com/1,Artist,Title\n".encode("utf-16le")
        # In current design, bytes with null bytes raise MalformedCSVError
        with pytest.raises(MalformedCSVError):
            parse_csv(utf16_le)

        utf16_be = "url,artist,title\nhttps://example.com/1,Artist,Title\n".encode("utf-16be")
        with pytest.raises(MalformedCSVError):
            parse_csv(utf16_be)

    def test_single_url_file_no_newline(self):
        """A single line containing only a valid URL without trailing newline."""
        raw = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
        records = parse_csv(raw)
        assert len(records) == 1
        assert records[0].url == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
        assert records[0].artist is None
        assert records[0].title is None

    def test_single_url_file_with_newline_and_whitespace(self):
        """A single URL surrounded by leading/trailing whitespace and newlines."""
        raw = "\n   \t  https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT   \n\n"
        records = parse_csv(raw)
        assert len(records) == 1
        assert records[0].url == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"

    def test_headerless_lists_with_sporadic_whitespace_and_comments(self):
        """Headerless list with varying whitespace, blank rows, and comment lines."""
        raw = (
            "# Top Tracks\n"
            "   \n"
            "https://example.com/t1   \n"
            "# Mid-list note\n"
            "https://example.com/t2 , Radiohead , Creep \n"
            "   \n"
            "https://example.com/t3 , Nirvana \n"
            "# End of list\n"
        )
        records = parse_csv(raw)
        assert len(records) == 3
        assert records[0].url == "https://example.com/t1"
        assert records[1].url == "https://example.com/t2"
        assert records[1].artist == "Radiohead"
        assert records[1].title == "Creep"
        assert records[2].url == "https://example.com/t3"
        assert records[2].artist == "Nirvana"

    def test_unquoted_commas_in_query_parameters(self):
        """RFC 4180 unquoted comma in query string splits field if comma is delimiter."""
        # If comma is delimiter, unquoted comma splits query string
        csv_data = "url\nhttps://example.com/audio?id=1,2,3\n"
        # Since 'url' header exists, only column 0 is taken as raw_url
        records = parse_csv(csv_data)
        assert len(records) == 1
        # The URL before the comma is still a valid URL:
        assert records[0].url == "https://example.com/audio?id=1"

        # When properly quoted, the entire URL is preserved
        csv_data_quoted = 'url\n"https://example.com/audio?id=1,2,3"\n'
        records_quoted = parse_csv(csv_data_quoted)
        assert len(records_quoted) == 1
        assert records_quoted[0].url == "https://example.com/audio?id=1,2,3"

    def test_complex_url_schemes_and_query_parameters(self):
        """URLs with complex query parameters, fragments, and IPv4 netloc."""
        urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=youtu.be&t=42s",
            "http://127.0.0.1:8080/audio/stream?codec=flac&res=24bit#t=10s",
            "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
            "https://sub.domain.co.uk:9000/path/to/song.mp3?token=abc%20123&uid=44#section",
        ]
        csv_content = "\n".join(urls)
        records = parse_csv(csv_content)
        assert len(records) == 4
        assert [r.url for r in records] == urls

    def test_empty_csv_variations(self):
        """EmptyCSVError must be raised on None, empty string, whitespace only, and empty rows."""
        with pytest.raises(EmptyCSVError):
            parse_csv(None)
        with pytest.raises(EmptyCSVError):
            parse_csv("")
        with pytest.raises(EmptyCSVError):
            parse_csv("   \t\n\r\n   ")
        with pytest.raises(EmptyCSVError):
            parse_csv(b"")
        with pytest.raises(EmptyCSVError):
            parse_csv(b"   \t\n   ")
        with pytest.raises(EmptyCSVError):
            parse_csv(",,\n,,\n\n")

    def test_mixed_delimiters_no_valid_urls_raises_no_valid_urls_or_empty(self):
        """Delimiter characters with no content raise NoValidUrlsError or EmptyCSVError."""
        with pytest.raises((NoValidUrlsError, EmptyCSVError)):
            parse_csv(",,\n;;;\n\t\t\n")

    def test_malformed_csv_variations(self):
        """MalformedCSVError must be raised for null bytes, missing URL header, and invalid types."""
        # Null bytes in bytes
        with pytest.raises(MalformedCSVError):
            parse_csv(b"https://example.com/\x00/test")
        # Null bytes in str
        with pytest.raises(MalformedCSVError):
            parse_csv("https://example.com/\x00/test")
        # Metadata headers present without URL column
        with pytest.raises(MalformedCSVError):
            parse_csv("artist,title,album\nQueen,Bohemian Rhapsody,A Night at the Opera\n")
        # Unsupported type
        with pytest.raises(MalformedCSVError):
            parse_csv(12345)  # type: ignore

    def test_no_valid_urls_variations(self):
        """NoValidUrlsError must be raised when input contains rows but no valid URLs."""
        # Only comments
        with pytest.raises(NoValidUrlsError):
            parse_csv("# Comment 1\n# Comment 2\n# Comment 3\n")
        # Headers present, but rows have invalid URL schemes or non-URLs
        with pytest.raises(NoValidUrlsError):
            parse_csv(
                "url,artist,title\n"
                "ftp://ftp.example.com/audio.mp3,Artist,Title\n"
                "mailto:user@example.com,Artist,Title\n"
                "not-a-url,Artist,Title\n"
                "file:///etc/passwd,Artist,Title\n"
                "http://,Artist,Title\n"
            )
        # Headerless text without URLs
        with pytest.raises(NoValidUrlsError):
            parse_csv("Just some random text\nAnother line without any URLs\n")


# ============================================================================
# Section 2: Library & Path Validation Adversarial Tests
# ============================================================================

class TestAdversarialPathValidation:
    """Adversarial stress tests for library path validation and write probe."""

    def test_symlink_to_valid_directory(self, tmp_path: Path):
        """A symlink pointing to an existing writable directory resolves and passes."""
        real_target = tmp_path / "real_dir"
        real_target.mkdir()
        symlink_path = tmp_path / "symlink_dir"
        symlink_path.symlink_to(real_target, target_is_directory=True)

        resolved = validate_library_directory(symlink_path)
        assert resolved == real_target.resolve()
        assert resolved.is_dir()

    def test_broken_symlink_raises_invalid_path(self, tmp_path: Path):
        """A broken symlink pointing to nonexistent path raises InvalidPathError."""
        broken_symlink = tmp_path / "broken_link"
        broken_symlink.symlink_to(tmp_path / "nonexistent_target")
        with pytest.raises(InvalidPathError):
            validate_library_directory(broken_symlink)

    def test_symlink_to_regular_file_raises_not_a_directory(self, tmp_path: Path):
        """A symlink pointing to a regular file raises NotADirectoryError."""
        real_file = tmp_path / "real_file.txt"
        real_file.write_text("hello")
        symlink_to_file = tmp_path / "symlink_to_file"
        symlink_to_file.symlink_to(real_file)

        with pytest.raises(NotADirectoryError):
            validate_library_directory(symlink_to_file)

    def test_relative_paths_and_trailing_slashes(self, tmp_path: Path, monkeypatch):
        """Relative paths and trailing slashes resolve to absolute canonical paths."""
        target_dir = tmp_path / "my_music"
        target_dir.mkdir()
        monkeypatch.chdir(tmp_path)

        rel_path = Path("my_music///")
        resolved = validate_library_directory(rel_path)
        assert resolved.is_absolute()
        assert resolved == target_dir.resolve()

    def test_system_root_rejection(self):
        """Root paths like '/' and '/root' must be rejected."""
        with pytest.raises(InvalidPathError):
            validate_library_directory("/")
        with pytest.raises(InvalidPathError):
            validate_library_directory("/root")

    def test_regular_file_rejection(self, tmp_path: Path):
        """Regular file raises NotADirectoryError."""
        sample_file = tmp_path / "sample.mp3"
        sample_file.write_bytes(b"dummy audio")
        with pytest.raises(NotADirectoryError):
            validate_library_directory(sample_file)

    def test_device_file_rejection(self):
        """Device files like /dev/null or /dev/zero must raise NotADirectoryError."""
        dev_null = Path("/dev/null")
        if dev_null.exists():
            with pytest.raises(NotADirectoryError):
                validate_library_directory(dev_null)

        dev_zero = Path("/dev/zero")
        if dev_zero.exists():
            with pytest.raises(NotADirectoryError):
                validate_library_directory(dev_zero)

    def test_probe_writability_and_guaranteed_cleanup(self, tmp_path: Path):
        """Probe file is created, flushed, and strictly cleaned up."""
        valid_dir = tmp_path / "clean_probe_dir"
        valid_dir.mkdir()

        # Run validation 10 times in sequence
        for _ in range(10):
            resolved = validate_library_directory(valid_dir)
            assert resolved == valid_dir.resolve()

        # Ensure zero probe files remain
        remaining_probes = list(valid_dir.glob(f"{PROBE_PREFIX}*"))
        assert len(remaining_probes) == 0

    def test_probe_cleanup_on_write_failure(self, tmp_path: Path):
        """Probe file must be unlinked even when exception occurs during write test."""
        target_dir = tmp_path / "failing_probe_dir"
        target_dir.mkdir()

        # Mock open inside validate_library_directory to simulate an OSError during write
        original_open = open

        def failing_open(file, *args, **kwargs):
            if PROBE_PREFIX in str(file):
                # Create the file on disk first to simulate partial write failure
                f = original_open(file, *args, **kwargs)
                f.write(b"partial_write")
                f.close()
                raise OSError("Simulated disk full (ENOSPC) during probe flush")
            return original_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=failing_open):
            with pytest.raises(PermissionDeniedError):
                validate_library_directory(target_dir)

        # Confirm probe file was unlinked in finally: block
        remaining_probes = list(target_dir.glob(f"{PROBE_PREFIX}*"))
        assert len(remaining_probes) == 0


# ============================================================================
# Section 3: LibraryIndex Accuracy, Concurrency, and Normalization Tests
# ============================================================================

class TestAdversarialLibraryIndex:
    """Adversarial stress tests for LibraryIndex and normalizations."""

    def test_concurrent_lookups_thread_safety(self, tmp_path: Path):
        """Concurrent lookups across 20 worker threads must not corrupt or fail."""
        lib_dir = tmp_path / "concurrent_lib"
        lib_dir.mkdir()

        # Create 5 synthetic tracks
        tracks = [
            ("The Beatles", "Hey Jude", lib_dir / "The Beatles - Hey Jude.flac"),
            ("Queen", "Bohemian Rhapsody", lib_dir / "Queen - Bohemian Rhapsody.flac"),
            ("Pink Floyd", "Comfortably Numb", lib_dir / "Pink Floyd - Comfortably Numb.flac"),
            ("Daft Punk", "Get Lucky", lib_dir / "Daft Punk - Get Lucky.flac"),
            ("Radiohead", "Karma Police", lib_dir / "Radiohead - Karma Police.flac"),
        ]
        for artist, title, path in tracks:
            _generate_minimal_flac(path, artist=artist, title=title)

        index = LibraryIndex.build(lib_dir)

        def lookup_worker(iter_count: int) -> bool:
            for _ in range(iter_count):
                for artist, title, path in tracks:
                    if not index.contains_track(artist, title):
                        return False
                    if not index.contains_stem(path.stem):
                        return False
                    matches = index.find_track_files(artist, title)
                    if not matches or path not in matches:
                        return False
                # Negative lookups
                if index.contains_track("Nonexistent", "Unknown"):
                    return False
                if index.contains_stem("Completely Fake Track"):
                    return False
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(lookup_worker, 50) for _ in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert all(results)
        assert len(results) == 20

    def test_normalization_resistance_diacritics_case_punctuation(self, tmp_path: Path):
        """Index correctly identifies tracks regardless of case, diacritics, and punctuation."""
        lib_dir = tmp_path / "norm_lib"
        lib_dir.mkdir()

        _generate_minimal_flac(
            lib_dir / "Beyoncé - Déjà Vu.flac",
            artist="Beyoncé",
            title="Déjà Vu",
        )
        _generate_minimal_flac(
            lib_dir / "Motörhead - Overkill.flac",
            artist="Motörhead",
            title="Overkill",
        )
        _generate_minimal_flac(
            lib_dir / "AC-DC - Back in Black.flac",
            artist="AC/DC",
            title="Back in Black",
        )

        index = LibraryIndex.build(lib_dir)

        # Diacritics resistance
        assert index.contains_track("Beyonce", "Deja Vu")
        assert index.contains_track("BEYONCÉ", "DÉJÀ VU")
        assert index.contains_track("beyonce", "deja vu")

        assert index.contains_track("Motorhead", "Overkill")
        assert index.contains_track("MOTÖRHEAD", "OVERKILL")

        # Punctuation & Stem resistance
        assert index.contains_stem("Beyoncé - Déjà Vu")
        assert index.contains_stem("Beyonce - Deja Vu.flac")
        assert index.contains_stem("beyonce - deja vu")

        assert index.contains_track("AC/DC", "Back in Black")
        assert index.contains_track("ACDC", "Back in Black")

        # Reverse stem lookup: "{Title} - {Artist}"
        assert index.contains_track("Overkill", "Motörhead")
        matches = index.find_track_files("Deja Vu", "Beyonce")
        assert len(matches) == 1
        assert matches[0].name == "Beyoncé - Déjà Vu.flac"

    def test_library_index_deep_hierarchy_and_uppercase_extensions(self, tmp_path: Path):
        """Index recursively traverses arbitrary directory depths and matches uppercase extensions."""
        deep_dir = tmp_path / "deep" / "level1" / "level2" / "level3"
        deep_dir.mkdir(parents=True)

        # File with uppercase extension
        flac_path = deep_dir / "Pink Floyd - Time.FLAC"
        _generate_minimal_flac(flac_path, artist="Pink Floyd", title="Time")

        index = LibraryIndex.build(tmp_path / "deep")
        assert index.contains_track("Pink Floyd", "Time")
        assert index.contains_stem("Pink Floyd - Time")
        matches = index.find_track_files("Pink Floyd", "Time")
        assert len(matches) == 1
        assert matches[0].resolve() == flac_path.resolve()

    def test_library_index_multiple_identical_tracks(self, tmp_path: Path):
        """When identical tracks exist across folders, find_track_files returns all of them."""
        dir_a = tmp_path / "album_a"
        dir_b = tmp_path / "album_b"
        dir_a.mkdir()
        dir_b.mkdir()

        track_a = _generate_minimal_flac(dir_a / "Queen - Radio Ga Ga.flac", artist="Queen", title="Radio Ga Ga")
        track_b = _generate_minimal_flac(dir_b / "Queen - Radio Ga Ga (Live).flac", artist="Queen", title="Radio Ga Ga")

        index = LibraryIndex.build(tmp_path)
        matches = index.find_track_files("Queen", "Radio Ga Ga")
        assert len(matches) == 2
        assert track_a.resolve() in matches
        assert track_b.resolve() in matches


# ============================================================================
# Section 4: Scale and Boundary Stress Tests
# ============================================================================

class TestScaleAndBoundaryStress:
    """Scale and stress tests for CSV processor and library validation."""

    def test_massive_csv_scale_10k_records(self):
        """Parses 10,000 CSV rows efficiently without OOM or quadratic slowdown."""
        import time
        header = "url,artist,title\n"
        rows = [f"https://example.com/track/{i},Artist {i},Title {i}\n" for i in range(10_000)]
        payload = header + "".join(rows)

        start = time.perf_counter()
        records = parse_csv(payload)
        elapsed = time.perf_counter() - start

        assert len(records) == 10_000
        assert records[0].url == "https://example.com/track/0"
        assert records[-1].url == "https://example.com/track/9999"
        # 10k rows must parse in under 1.5 seconds in Python
        assert elapsed < 1.5

    def test_crlf_and_cr_line_endings(self):
        """Handles Windows CRLF (\\r\\n) and cleanly raises MalformedCSVError on bare CR (\\r)."""
        crlf_csv = "url\r\nhttps://example.com/1\r\nhttps://example.com/2\r\n"
        records_crlf = parse_csv(crlf_csv)
        assert len(records_crlf) == 2

        cr_csv = "url\rhttps://example.com/1\rhttps://example.com/2\r"
        with pytest.raises(MalformedCSVError):
            parse_csv(cr_csv)

    def test_nested_missing_directory_creation(self, tmp_path: Path):
        """Creates deeply nested missing directory when create_if_missing=True."""
        nested_dir = tmp_path / "deep" / "nested" / "path" / "music"
        assert not nested_dir.exists()

        validated = validate_library_directory(nested_dir, create_if_missing=True)
        assert validated.exists()
        assert validated.is_dir()
        assert validated == nested_dir.resolve()

    def test_library_path_with_unicode_and_spaces(self, tmp_path: Path):
        """Path validation supports non-ASCII Unicode and spaces in folder names."""
        unicode_dir = tmp_path / "Música & Áudio (2026) — 🎵"
        unicode_dir.mkdir()

        validated = validate_library_directory(unicode_dir)
        assert validated.exists()
        assert validated == unicode_dir.resolve()

    def test_library_unwritable_directory_error_and_probe_cleanup(self, tmp_path: Path):
        """Read-only directory raises PermissionDeniedError and leaves no probe file."""
        ro_dir = tmp_path / "read_only_dir"
        ro_dir.mkdir()

        # Set read-only permissions (0o555 on Unix)
        ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        try:
            with pytest.raises(PermissionDeniedError):
                validate_library_directory(ro_dir)
            # Ensure zero probe files
            assert len(list(ro_dir.glob(f"{PROBE_PREFIX}*"))) == 0
        finally:
            # Restore permissions for cleanup
            ro_dir.chmod(stat.S_IRWXU)

