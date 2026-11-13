"""Comprehensive unit test suite for Milestone 1 domain modules.

Verifies:
- config.py
- csv_processor.py
- library.py
- deduplicator.py
- organizer.py

Runs 100% offline using synthetic audio fixtures (mutagen + soundfile + wave).
Zero network calls. Execution time < 2 seconds.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import wave
from pathlib import Path
from typing import Optional

import mutagen
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1
from mutagen.mp3 import MP3

from songetter.config import (
    AUDIO_SUFFIXES,
    DEFAULT_FALLBACK_ARTIST,
    DEFAULT_FALLBACK_TITLE,
    MAX_FILENAME_BYTES,
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
from songetter.deduplicator import (
    AudioMetadata,
    DuplicateResolutionResult,
    compare_quality,
    extract_metadata,
    normalize_text,
    resolve_duplicates,
)
from songetter.library import (
    InvalidPathError,
    LibraryError,
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


# ============================================================================
# Synthetic Audio Generation Helpers
# ============================================================================

def make_flac(
    path: Path,
    artist: Optional[str] = "Artist",
    title: Optional[str] = "Title",
    sample_rate: int = 44100,
    duration: float = 0.05,
    subtype: str = "PCM_16",
    pad_bytes: int = 0,
) -> Path:
    """Generates an authentic offline FLAC audio file with Vorbis comments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(sample_rate * duration))
    t = np.linspace(0, duration, n_samples, endpoint=False)
    data = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(str(path), data, sample_rate, subtype=subtype, format="FLAC")

    audio = FLAC(path)
    if artist is not None:
        audio["artist"] = artist
    if title is not None:
        audio["title"] = title
    if pad_bytes > 0:
        audio["comment"] = "X" * pad_bytes
    audio.save()
    return path


def make_mp3(
    path: Path,
    artist: Optional[str] = "Artist",
    title: Optional[str] = "Title",
    sample_rate: int = 44100,
    duration: float = 0.05,
    pad_bytes: int = 0,
) -> Path:
    """Generates an authentic offline MP3 audio file with ID3v2 tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(sample_rate * duration))
    t = np.linspace(0, duration, n_samples, endpoint=False)
    data = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(str(path), data, sample_rate, format="MP3")

    audio = MP3(path)
    try:
        audio.add_tags()
    except Exception:
        pass
    if title is not None:
        audio.tags.add(TIT2(encoding=3, text=title))
    if artist is not None:
        audio.tags.add(TPE1(encoding=3, text=artist))
    audio.save()

    if pad_bytes > 0:
        with open(path, "ab") as f:
            f.write(b"\x00" * pad_bytes)
    return path


def make_wav(
    path: Path,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    sample_rate: int = 44100,
    duration: float = 0.05,
) -> Path:
    """Generates an authentic offline WAV audio file using stdlib wave."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = max(1, int(sample_rate * duration))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = bytearray()
        for i in range(n_samples):
            val = int(16384 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))
            raw.extend(struct.pack("<h", val))
        wf.writeframes(raw)
    return path


# ============================================================================
# Group 1: CSV Processor Tests
# ============================================================================

def test_parse_csv_standard_comma():
    content = "url,artist,title\nhttps://open.spotify.com/track/1,Queen,Bohemian Rhapsody\n"
    records = parse_csv(content)
    assert len(records) == 1
    assert records[0].url == "https://open.spotify.com/track/1"
    assert records[0].artist == "Queen"
    assert records[0].title == "Bohemian Rhapsody"


@pytest.mark.parametrize("delim", [";", "\t", "|"])
def test_parse_csv_delimiters(delim: str):
    content = f"url{delim}artist{delim}title\nhttps://example.com/song{delim}Artist Name{delim}Song Title\n"
    records = parse_csv(content)
    assert len(records) == 1
    assert records[0].url == "https://example.com/song"
    assert records[0].artist == "Artist Name"
    assert records[0].title == "Song Title"


def test_parse_csv_utf8_sig_bom():
    content = b"\xef\xbb\xbfurl,artist,title\nhttps://example.com/song,Artist,Title\n"
    records = parse_csv(content)
    assert len(records) == 1
    assert records[0].url == "https://example.com/song"
    assert records[0].artist == "Artist"
    assert records[0].title == "Title"


def test_parse_csv_column_aliases():
    content1 = "link,performer,song\nhttps://example.com/1,Queen,Radio Ga Ga\n"
    records1 = parse_csv(content1)
    assert records1[0].url == "https://example.com/1"
    assert records1[0].artist == "Queen"
    assert records1[0].title == "Radio Ga Ga"

    content2 = "download_url,author,track_name\nhttps://example.com/2,Daft Punk,One More Time\n"
    records2 = parse_csv(content2)
    assert records2[0].url == "https://example.com/2"
    assert records2[0].artist == "Daft Punk"
    assert records2[0].title == "One More Time"


def test_parse_csv_case_insensitive_headers():
    content = "URL,ARTIST,TITLE\nhttps://example.com/track,Queen,Under Pressure\n"
    records = parse_csv(content)
    assert len(records) == 1
    assert records[0].url == "https://example.com/track"
    assert records[0].artist == "Queen"
    assert records[0].title == "Under Pressure"


def test_parse_csv_headerless_raw_urls():
    content = "https://example.com/1\nhttps://example.com/2\n"
    records = parse_csv(content)
    assert len(records) == 2
    assert records[0].url == "https://example.com/1"
    assert records[0].artist is None
    assert records[0].title is None
    assert records[1].url == "https://example.com/2"


def test_parse_csv_single_column_url_header():
    content = "url\nhttps://example.com/1\n"
    records = parse_csv(content)
    assert len(records) == 1
    assert records[0].url == "https://example.com/1"
    assert records[0].artist is None
    assert records[0].title is None


def test_parse_csv_empty_string_raises():
    with pytest.raises(EmptyCSVError):
        parse_csv("")
    with pytest.raises(EmptyCSVError):
        parse_csv("   \n\t  ")


def test_parse_csv_missing_url_column_raises():
    content = "artist,title,album\nQueen,Bohemian Rhapsody,A Night at the Opera\n"
    with pytest.raises(MalformedCSVError):
        parse_csv(content)


def test_parse_csv_no_valid_urls_raises():
    content = "url,artist,title\nnot_a_valid_url,Queen,Bohemian Rhapsody\n"
    with pytest.raises(NoValidUrlsError):
        parse_csv(content)


def test_parse_csv_rfc4180_quotes_and_commas():
    content = '"url","artist","title"\n"https://example.com/1","Queen, The","Bohemian, Rhapsody"\n'
    records = parse_csv(content)
    assert len(records) == 1
    assert records[0].artist == "Queen, The"
    assert records[0].title == "Bohemian, Rhapsody"


def test_parse_csv_whitespace_trimming():
    content = "  url  ,  artist  ,  title  \n  https://example.com/1  ,  Queen  ,  Bohemian Rhapsody  \n"
    records = parse_csv(content)
    assert records[0].url == "https://example.com/1"
    assert records[0].artist == "Queen"
    assert records[0].title == "Bohemian Rhapsody"


def test_parse_csv_bytes_and_string_inputs():
    text = "url,artist,title\nhttps://example.com/1,Queen,Song1\n"
    raw_bytes = text.encode("utf-8")
    records_str = parse_csv(text)
    records_bytes = parse_csv(raw_bytes)
    assert len(records_str) == len(records_bytes) == 1
    assert records_str[0].url == records_bytes[0].url == "https://example.com/1"


def test_parse_csv_binary_data_raises_malformed():
    with pytest.raises(MalformedCSVError):
        parse_csv(b"url,artist\x00,title\nhttps://example.com/1,A,T\n")


# ============================================================================
# Group 2: Library Tests
# ============================================================================

def test_validate_library_directory_valid(tmp_path: Path):
    resolved = validate_library_directory(tmp_path)
    assert resolved == tmp_path.resolve()


def test_validate_library_directory_probe_cleaned(tmp_path: Path):
    validate_library_directory(tmp_path)
    probes = list(tmp_path.glob(f"{PROBE_PREFIX}*"))
    assert len(probes) == 0


def test_validate_library_directory_nonexistent(tmp_path: Path):
    with pytest.raises(InvalidPathError):
        validate_library_directory(tmp_path / "nonexistent_dir_12345")


def test_validate_library_directory_is_file(tmp_path: Path):
    file_path = tmp_path / "not_a_dir.txt"
    file_path.write_text("content")
    with pytest.raises(NotADirectoryError):
        validate_library_directory(file_path)


def test_validate_library_directory_unwritable_chmod(tmp_path: Path):
    d = tmp_path / "readonly_dir"
    d.mkdir()
    os.chmod(d, 0o555)
    try:
        with pytest.raises(PermissionDeniedError):
            validate_library_directory(d)
    finally:
        os.chmod(d, 0o755)


def test_validate_library_directory_mock_permission_error(tmp_path: Path):
    target = tmp_path / "mock_dir"
    target.mkdir()

    original_open = open

    def failing_open(*args, **kwargs):
        if PROBE_PREFIX in str(args[0]):
            raise PermissionError("Simulated probe permission denied")
        return original_open(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.open", failing_open)
        with pytest.raises(PermissionDeniedError):
            validate_library_directory(target)


def test_library_index_build_empty(tmp_path: Path):
    index = LibraryIndex.build(tmp_path)
    assert index.contains_stem("Queen - Bohemian Rhapsody") is False
    assert index.contains_track("Queen", "Bohemian Rhapsody") is False


def test_library_index_contains_stem(tmp_path: Path):
    make_flac(tmp_path / "Queen - Bohemian Rhapsody.flac", artist="Queen", title="Bohemian Rhapsody")
    index = LibraryIndex.build(tmp_path)
    assert index.contains_stem("Queen - Bohemian Rhapsody") is True
    assert index.contains_stem("Queen - Bohemian Rhapsody.flac") is True
    assert index.contains_stem("queen - bohemian rhapsody") is True
    assert index.contains_stem("Nonexistent Song") is False


def test_library_index_contains_track_tags(tmp_path: Path):
    make_flac(tmp_path / "track_001.flac", artist="Daft Punk", title="Get Lucky")
    index = LibraryIndex.build(tmp_path)
    assert index.contains_track("Daft Punk", "Get Lucky") is True
    assert index.contains_track("daft punk", "get lucky") is True
    assert index.contains_track("Daft Punk", "One More Time") is False


def test_library_index_find_track_files(tmp_path: Path):
    p1 = make_flac(tmp_path / "t1.flac", artist="Queen", title="Radio Ga Ga")
    p2 = make_flac(tmp_path / "t2.flac", artist="Queen", title="Radio Ga Ga")
    index = LibraryIndex.build(tmp_path)
    found = index.find_track_files("Queen", "Radio Ga Ga")
    assert set(found) == {p1.resolve(), p2.resolve()}


def test_library_index_ignores_non_audio(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("notes")
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "playlist.m3u8").write_text("#EXTM3U")
    index = LibraryIndex.build(tmp_path)
    assert index.contains_stem("notes") is False
    assert index.contains_stem("cover") is False


# ============================================================================
# Group 3: Deduplicator Tests
# ============================================================================

def test_extract_metadata_flac_vorbis(tmp_path: Path):
    p = make_flac(tmp_path / "song.flac", artist="Queen", title="Bohemian Rhapsody")
    meta = extract_metadata(p)
    assert meta.artist == "Queen"
    assert meta.title == "Bohemian Rhapsody"
    assert meta.bitrate > 0
    assert meta.file_size > 0
    assert meta.duration > 0


def test_extract_metadata_mp3_id3(tmp_path: Path):
    p = make_mp3(tmp_path / "song.mp3", artist="Daft Punk", title="One More Time")
    meta = extract_metadata(p)
    assert meta.artist == "Daft Punk"
    assert meta.title == "One More Time"
    assert meta.bitrate > 0
    assert meta.file_size > 0


def test_extract_metadata_untagged_stem_fallback(tmp_path: Path):
    p = make_wav(tmp_path / "Daft Punk - One More Time.wav", artist=None, title=None)
    meta = extract_metadata(p)
    assert meta.artist == "Daft Punk"
    assert meta.title == "One More Time"


def test_extract_metadata_malformed_stem_fallback(tmp_path: Path):
    p = make_wav(tmp_path / "SingleTitleOnly.wav", artist=None, title=None)
    meta = extract_metadata(p)
    assert meta.title == "SingleTitleOnly"
    assert meta.artist == DEFAULT_FALLBACK_ARTIST


def test_compare_quality_tier1_bitrate_higher_a():
    meta_a = AudioMetadata(Path("a.flac"), "A", "T", bitrate=320000, file_size=5000, duration=1.0)
    meta_b = AudioMetadata(Path("b.mp3"), "A", "T", bitrate=128000, file_size=10000, duration=1.0)
    assert compare_quality(meta_a, meta_b) == 1


def test_compare_quality_tier1_bitrate_higher_b():
    meta_a = AudioMetadata(Path("a.mp3"), "A", "T", bitrate=128000, file_size=10000, duration=1.0)
    meta_b = AudioMetadata(Path("b.flac"), "A", "T", bitrate=320000, file_size=5000, duration=1.0)
    assert compare_quality(meta_a, meta_b) == -1


def test_compare_quality_tier2_size_higher_a():
    meta_a = AudioMetadata(Path("a.flac"), "A", "T", bitrate=128000, file_size=10000, duration=1.0)
    meta_b = AudioMetadata(Path("b.flac"), "A", "T", bitrate=128000, file_size=5000, duration=1.0)
    assert compare_quality(meta_a, meta_b) == 1


def test_compare_quality_tier2_size_higher_b():
    meta_a = AudioMetadata(Path("a.flac"), "A", "T", bitrate=128000, file_size=5000, duration=1.0)
    meta_b = AudioMetadata(Path("b.flac"), "A", "T", bitrate=128000, file_size=10000, duration=1.0)
    assert compare_quality(meta_a, meta_b) == -1


def test_compare_quality_tier3_identical():
    meta_a = AudioMetadata(Path("a.flac"), "A", "T", bitrate=128000, file_size=5000, duration=1.0)
    meta_b = AudioMetadata(Path("b.flac"), "A", "T", bitrate=128000, file_size=5000, duration=1.0)
    assert compare_quality(meta_a, meta_b) == 0


def test_resolve_duplicates_tier1_deletes_lower_bitrate(tmp_path: Path):
    p_high = make_flac(tmp_path / "high.flac", artist="Queen", title="Radio Ga Ga", sample_rate=48000, subtype="PCM_24")
    p_low = make_flac(tmp_path / "low.flac", artist="Queen", title="Radio Ga Ga", sample_rate=22050, subtype="PCM_16")
    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    assert p_high.exists() is True
    assert p_low.exists() is False
    assert results[0].retained == p_high.resolve()
    assert results[0].deleted == [p_low.resolve()]


def test_resolve_duplicates_tier2_deletes_smaller_size(tmp_path: Path):
    p_large = make_flac(tmp_path / "large.flac", artist="Queen", title="Radio Ga Ga", pad_bytes=5000)
    p_small = make_flac(tmp_path / "small.flac", artist="Queen", title="Radio Ga Ga", pad_bytes=0)
    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    assert p_large.exists() is True
    assert p_small.exists() is False


def test_resolve_duplicates_tier3_identical_deletes_one(tmp_path: Path):
    p1 = make_flac(tmp_path / "orig.flac", artist="Queen", title="Radio Ga Ga")
    p2 = tmp_path / "copy.flac"
    shutil.copy2(p1, p2)
    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    remaining = list(tmp_path.glob("*.flac"))
    assert len(remaining) == 1


def test_resolve_duplicates_multi_replicates(tmp_path: Path):
    p_48 = make_flac(tmp_path / "f48.flac", artist="Queen", title="Radio Ga Ga", sample_rate=48000, subtype="PCM_24")
    p_44 = make_flac(tmp_path / "f44.flac", artist="Queen", title="Radio Ga Ga", sample_rate=44100, subtype="PCM_16")
    p_22 = make_flac(tmp_path / "f22.flac", artist="Queen", title="Radio Ga Ga", sample_rate=22050, subtype="PCM_16")
    p_mp3 = make_mp3(tmp_path / "f.mp3", artist="Queen", title="Radio Ga Ga", sample_rate=22050)
    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    assert p_48.exists() is True
    assert p_44.exists() is False
    assert p_22.exists() is False
    assert p_mp3.exists() is False


def test_resolve_duplicates_different_songs_no_deletion(tmp_path: Path):
    p1 = make_flac(tmp_path / "song1.flac", artist="Queen", title="Radio Ga Ga")
    p2 = make_flac(tmp_path / "song2.flac", artist="Queen", title="Bohemian Rhapsody")
    results = resolve_duplicates(tmp_path)
    assert len(results) == 0
    assert p1.exists() is True
    assert p2.exists() is True


def test_resolve_duplicates_preserves_embedded_lyrics_on_winner(tmp_path: Path):
    p_high = make_flac(tmp_path / "high.flac", artist="Queen", title="Radio Ga Ga", sample_rate=48000)
    audio = FLAC(p_high)
    audio["lyrics"] = "All we hear is Radio ga ga"
    audio.save()
    p_low = make_flac(tmp_path / "low.flac", artist="Queen", title="Radio Ga Ga", sample_rate=22050)
    resolve_duplicates(tmp_path)
    assert p_high.exists() is True
    assert p_low.exists() is False
    assert FLAC(p_high).get("lyrics") == ["All we hear is Radio ga ga"]


def test_normalize_text_diacritics_and_quotes():
    assert normalize_text("Café René") == "cafe rene"
    assert normalize_text("“Rock ’n’ Roll”") == "rock n roll"
    assert normalize_text("AC/DC — Back in Black") == "acdc back in black"


# ============================================================================
# Group 4: Organizer Tests
# ============================================================================

def test_sanitize_filename_component_forbidden_chars():
    raw = 'AC/DC: Live *2024*? <Bootleg> | "Rock"'
    clean = sanitize_filename_component(raw)
    for forbidden in r'/\?%*:|"<>':
        assert forbidden not in clean
    assert clean == "ACDC Live 2024 Bootleg Rock"


def test_sanitize_filename_component_control_chars():
    raw = "Track\x00\x1f\n\rName"
    clean = sanitize_filename_component(raw)
    assert clean == "Track Name"


def test_sanitize_filename_component_whitespace_collapse():
    raw = "  Excessive    Whitespace   "
    clean = sanitize_filename_component(raw)
    assert clean == "Excessive Whitespace"


def test_sanitize_filename_component_empty_fallback():
    assert sanitize_filename_component("") == "Unknown"
    assert sanitize_filename_component("???") == "Unknown"
    assert sanitize_filename_component("...", fallback="Fallback") == "Fallback"


def test_sanitize_filename_component_length_bounding():
    long_str = "A" * 300
    clean = sanitize_filename_component(long_str, max_bytes=240)
    assert len(clean.encode("utf-8")) <= 240

    long_cjk = "你好世界" * 70
    clean_cjk = sanitize_filename_component(long_cjk, max_bytes=240)
    assert len(clean_cjk.encode("utf-8")) <= 240
    # Must decode cleanly without error
    clean_cjk.encode("utf-8").decode("utf-8")


def test_format_standard_filename():
    formatted = format_standard_filename("Queen", "Bohemian Rhapsody", "flac")
    assert formatted == "Queen - Bohemian Rhapsody.flac"


def test_format_standard_filename_strips_dot():
    f1 = format_standard_filename("Queen", "Song", ".flac")
    f2 = format_standard_filename("Queen", "Song", "flac")
    assert f1 == f2 == "Queen - Song.flac"


def test_flatten_and_organize_nested_subdirectories(tmp_path: Path):
    nested = tmp_path / "ArtistFolder" / "AlbumFolder"
    nested.mkdir(parents=True)
    orig = make_flac(nested / "track.flac", artist="Queen", title="Bohemian Rhapsody")
    organized = flatten_and_organize_library(tmp_path)
    assert len(organized) == 1
    expected = tmp_path / "Queen - Bohemian Rhapsody.flac"
    assert organized[0] == expected
    assert expected.exists() is True
    assert orig.exists() is False


def test_flatten_and_organize_prunes_empty_directories(tmp_path: Path):
    nested = tmp_path / "Level1" / "Level2" / "Level3"
    nested.mkdir(parents=True)
    make_flac(nested / "track.flac", artist="Queen", title="Bohemian Rhapsody")
    flatten_and_organize_library(tmp_path)
    assert (tmp_path / "Level1").exists() is False


def test_flatten_and_organize_name_collision(tmp_path: Path):
    dir1 = tmp_path / "Folder1"
    dir2 = tmp_path / "Folder2"
    dir1.mkdir()
    dir2.mkdir()
    make_flac(dir1 / "track1.flac", artist="Queen", title="Radio Ga Ga", sample_rate=44100)
    make_flac(dir2 / "track2.flac", artist="Queen", title="Radio Ga Ga", sample_rate=48000)
    organized = flatten_and_organize_library(tmp_path)
    assert len(organized) == 2
    filenames = {p.name for p in organized}
    assert "Queen - Radio Ga Ga.flac" in filenames
    assert "Queen - Radio Ga Ga (1).flac" in filenames


def test_flatten_and_organize_preserves_embedded_metadata_lyrics(tmp_path: Path):
    sub = tmp_path / "Album"
    sub.mkdir()
    src = make_flac(sub / "track.flac", artist="Queen", title="Radio Ga Ga")
    audio = FLAC(src)
    audio["lyrics"] = "All we hear is Radio ga ga"
    audio.save()
    organized = flatten_and_organize_library(tmp_path)
    target = tmp_path / "Queen - Radio Ga Ga.flac"
    assert organized[0] == target
    assert FLAC(target).get("lyrics") == ["All we hear is Radio ga ga"]
    assert not (tmp_path / "Queen - Radio Ga Ga.lrc").exists()


# ============================================================================
# Group 5: Config Tests
# ============================================================================

def test_audio_suffixes_contains_expected():
    assert ".flac" in AUDIO_SUFFIXES
    assert ".mp3" in AUDIO_SUFFIXES
    assert ".wav" in AUDIO_SUFFIXES
    assert ".ogg" in AUDIO_SUFFIXES
    assert ".m4a" in AUDIO_SUFFIXES


def test_config_probe_prefix():
    assert PROBE_PREFIX.startswith(".songetter_probe_")
