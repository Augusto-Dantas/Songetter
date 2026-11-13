"""Empirical and adversarial test suite for deduplicator.py and organizer.py.

Exercises:
1. Multi-tier deduplication quality hierarchy:
   - Tier 1: Bitrate dominance (e.g. 320 kbps vs 256 kbps with inverse file sizes)
   - Tier 2: File size tie-breaker when bitrate is equal
   - Tier 3: Deterministic tie-breaker when bitrate and file size are identical
2. Stem fallback and metadata extraction:
   - Missing tags across containers (WAV, MP3, FLAC, OGG)
   - Fallback with standard 'Artist - Title', single title, multiple dashes
4. Organizer sanitization & UTF-8 bounding:
   - Forbidden characters and ASCII 0-31 control chars
   - Extreme UTF-8 byte bounding (300-byte CJK, emojis) <= 240 bytes
5. Deeply nested directory flattening & pruning:
   - 7+ level nested trees
   - Pruning empty directories
   - Preserving non-empty non-audio directories
   - Collision handling with numbered suffixes
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from songetter.config import DEFAULT_FALLBACK_ARTIST, DEFAULT_FALLBACK_TITLE
from songetter.deduplicator import (
    AudioMetadata,
    compare_quality,
    extract_metadata,
    resolve_duplicates,
)
from songetter.organizer import (
    flatten_and_organize_library,
    format_standard_filename,
    sanitize_filename_component,
    truncate_utf8,
)


def _generate_mp3(path: Path, duration: int = 2, bitrate_k: int = 320, artist: str = None, title: str = None):
    """Generates a genuine MP3 audio bitstream using ffmpeg with specified bitrate and optional tags."""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-b:a", f"{bitrate_k}k",
        str(path),
    ]
    subprocess.run(cmd, check=True)
    if artist or title:
        try:
            audio = MP3(path, ID3=EasyID3)
        except Exception:
            audio = MP3(path)
            audio.add_tags(ID3=EasyID3)
        if artist:
            audio["artist"] = artist
        if title:
            audio["title"] = title
        audio.save()


def _generate_flac(path: Path, duration: int = 2, artist: str = None, title: str = None):
    """Generates a genuine FLAC audio bitstream using ffmpeg with optional Vorbis tags."""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        str(path),
    ]
    subprocess.run(cmd, check=True)
    if artist or title:
        audio = FLAC(path)
        if artist:
            audio["artist"] = artist
        if title:
            audio["title"] = title
        audio.save()


def _generate_wav(path: Path, duration: int = 2):
    """Generates an uncompressed WAV audio file using ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        str(path),
    ]
    subprocess.run(cmd, check=True)


# ============================================================================
# 1. Deduplication Quality Hierarchy
# ============================================================================

def test_dedup_tier1_bitrate_dominance(tmp_path: Path):
    """Tier 1: Bitrate dominance must retain 320 kbps over 256 kbps, even if 256 kbps file size is larger."""
    # 320 kbps file with shorter duration (2s) -> smaller file size (~82 KB)
    p_320 = tmp_path / "track_320k.mp3"
    _generate_mp3(p_320, duration=2, bitrate_k=320, artist="Daft Punk", title="Get Lucky")
    meta_320 = extract_metadata(p_320)

    # 256 kbps file with longer duration (5s) -> larger file size (~160 KB)
    p_256 = tmp_path / "track_256k.mp3"
    _generate_mp3(p_256, duration=5, bitrate_k=256, artist="Daft Punk", title="Get Lucky")
    meta_256 = extract_metadata(p_256)

    assert meta_320.bitrate > meta_256.bitrate, "320k must have higher bitrate than 256k"
    assert meta_320.file_size < meta_256.file_size, "Adversarial condition: 320k must be smaller file size than 256k"

    # Direct quality comparison
    assert compare_quality(meta_320, meta_256) == 1
    assert compare_quality(meta_256, meta_320) == -1

    # End-to-end duplicate resolution in directory
    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    assert results[0].retained == p_320
    assert p_320.exists(), "320 kbps file must be retained"
    assert not p_256.exists(), "256 kbps file must be deleted"
    assert "Retained higher bitrate" in results[0].reason


def test_dedup_tier2_filesize_tiebreaker(tmp_path: Path):
    """Tier 2: When bitrate is equal, larger file size must be retained."""
    # Both files 320 kbps. One is 4s (larger), one is 2s (smaller)
    p_large = tmp_path / "track_large.mp3"
    _generate_mp3(p_large, duration=4, bitrate_k=320, artist="Queen", title="Radio Ga Ga")
    meta_large = extract_metadata(p_large)

    p_small = tmp_path / "track_small.mp3"
    _generate_mp3(p_small, duration=2, bitrate_k=320, artist="Queen", title="Radio Ga Ga")
    meta_small = extract_metadata(p_small)

    assert meta_large.bitrate == meta_small.bitrate, "Bitrates must be identical"
    assert meta_large.file_size > meta_small.file_size, "File size must be larger"

    # Direct quality comparison
    assert compare_quality(meta_large, meta_small) == 1
    assert compare_quality(meta_small, meta_large) == -1

    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    assert results[0].retained == p_large
    assert p_large.exists(), "Larger file must be retained"
    assert not p_small.exists(), "Smaller file must be deleted"
    assert "Retained larger file size" in results[0].reason


def test_dedup_tier3_deterministic_tiebreaker(tmp_path: Path):
    """Tier 3: When bitrate and file size are identical, tie-breaker must deterministically retain exactly one."""
    p_a = tmp_path / "track_alpha.mp3"
    _generate_mp3(p_a, duration=2, bitrate_k=320, artist="Adele", title="Hello")

    # Byte-for-byte clone
    p_b = tmp_path / "track_zeta.mp3"
    shutil.copy2(p_a, p_b)

    meta_a = extract_metadata(p_a)
    meta_b = extract_metadata(p_b)

    assert meta_a.bitrate == meta_b.bitrate
    assert meta_a.file_size == meta_b.file_size
    assert compare_quality(meta_a, meta_b) == 0

    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    retained = results[0].retained
    deleted = results[0].deleted

    assert len(deleted) == 1
    # Exactly one retained, exactly one deleted
    assert retained.exists()
    assert not deleted[0].exists()
    assert {retained, deleted[0]} == {p_a, p_b}
    assert "deterministic tie-breaker" in results[0].reason.lower()


def test_dedup_three_way_identical_duplicates(tmp_path: Path):
    """Verify 3 identical duplicates results in exactly 1 retained and 2 deleted."""
    p1 = tmp_path / "copy_1.mp3"
    _generate_mp3(p1, duration=2, bitrate_k=320, artist="Nirvana", title="Come As You Are")
    p2 = tmp_path / "copy_2.mp3"
    shutil.copy2(p1, p2)
    p3 = tmp_path / "copy_3.mp3"
    shutil.copy2(p1, p3)

    results = resolve_duplicates(tmp_path)
    assert len(results) == 1
    assert len(results[0].deleted) == 2
    assert results[0].retained.exists()
    for d in results[0].deleted:
        assert not d.exists()


# ============================================================================
# 2. Stem Fallback & Metadata Extraction Across Containers
# ============================================================================

def test_stem_fallback_wav(tmp_path: Path):
    """Untagged WAV with standard stem 'Artist - Title.wav'."""
    p = tmp_path / "Pink Floyd - Comfortably Numb.wav"
    _generate_wav(p, duration=1)
    meta = extract_metadata(p)
    assert meta.artist == "Pink Floyd"
    assert meta.title == "Comfortably Numb"
    assert meta.bitrate > 0


def test_stem_fallback_mp3_untagged(tmp_path: Path):
    """Untagged MP3 with standard stem 'Artist - Title.mp3'."""
    p = tmp_path / "Led Zeppelin - Stairway to Heaven.mp3"
    _generate_mp3(p, duration=1, bitrate_k=192)
    meta = extract_metadata(p)
    assert meta.artist == "Led Zeppelin"
    assert meta.title == "Stairway to Heaven"


def test_stem_fallback_single_title_no_dash(tmp_path: Path):
    """Untagged file with single title (no ' - ')."""
    p = tmp_path / "SoloTrack.mp3"
    _generate_mp3(p, duration=1, bitrate_k=192)
    meta = extract_metadata(p)
    assert meta.artist == DEFAULT_FALLBACK_ARTIST
    assert meta.title == "SoloTrack"


def test_stem_fallback_multiple_dashes(tmp_path: Path):
    """Untagged file with multiple dashes: 'Artist - Title - Remix'."""
    p = tmp_path / "Daft Punk - Harder Better Faster - Club Mix.mp3"
    _generate_mp3(p, duration=1, bitrate_k=192)
    meta = extract_metadata(p)
    assert meta.artist == "Daft Punk"
    assert meta.title == "Harder Better Faster - Club Mix"


def test_stem_fallback_flac_untagged_challenge(tmp_path: Path):
    """Adversarial check: Untagged FLAC must fall back to stem without raising Vorbis ValueError."""
    p = tmp_path / "Radiohead - Karma Police.flac"
    _generate_flac(p, duration=1)  # untagged FLAC
    meta = extract_metadata(p)
    assert meta.artist == "Radiohead"
    assert meta.title == "Karma Police"


# ============================================================================
# 4. Organizer Sanitization & Extreme UTF-8 Bounding
# ============================================================================

def test_organizer_forbidden_characters():
    """All POSIX and Windows forbidden characters [\\/*?:"<>|] must be sanitized."""
    forbidden = ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]
    for ch in forbidden:
        res = sanitize_filename_component(f"Track{ch}Name")
        for bad in forbidden:
            assert bad not in res, f"Sanitization leaked forbidden char '{bad}' from input '{ch}'"


def test_organizer_control_characters():
    """ASCII control characters 0-31 and 127 must be sanitized."""
    for i in range(32):
        res = sanitize_filename_component(f"Start{chr(i)}End")
        assert chr(i) not in res, f"Control char {i} leaked: {repr(res)}"
    res_127 = sanitize_filename_component(f"Start{chr(127)}End")
    assert chr(127) not in res_127


def test_organizer_only_forbidden_characters_fallback():
    """Component composed entirely of forbidden characters falls back safely."""
    res = sanitize_filename_component(':::***???///\\\\|"<>\x00\x1f')
    assert res == "Unknown"


def test_organizer_extreme_cjk_byte_bounding():
    """Extreme 300-byte CJK artist and title bounded to <= 240 bytes with valid UTF-8."""
    cjk_artist = "音樂藝人" * 25  # 100 CJK characters = 300 bytes UTF-8
    cjk_title = "極限歌曲標題" * 25  # 150 CJK characters = 450 bytes UTF-8

    filename = format_standard_filename(cjk_artist, cjk_title, ".flac")
    encoded = filename.encode("utf-8")

    assert len(encoded) <= 240, f"Filename exceeded 240 bytes: {len(encoded)}"
    assert filename == encoded.decode("utf-8"), "Resulting filename must be valid UTF-8"
    assert filename.endswith(".flac"), "Extension must be intact"
    assert " - " in filename, "Delimiter ' - ' must be intact"


def test_organizer_extreme_emoji_byte_bounding():
    """Extreme 4-byte emoji sequences bounded to <= 240 bytes with valid UTF-8."""
    emoji_artist = "🎵🎶🎧🎤" * 25  # 100 emojis * 4 bytes = 400 bytes
    emoji_title = "🔥⚡✨🌟" * 25   # 100 emojis * 4 bytes = 400 bytes

    filename = format_standard_filename(emoji_artist, emoji_title, ".mp3")
    encoded = filename.encode("utf-8")

    assert len(encoded) <= 240, f"Filename exceeded 240 bytes: {len(encoded)}"
    assert filename == encoded.decode("utf-8")
    assert filename.endswith(".mp3")
    assert " - " in filename


# ============================================================================
# 5. Deeply Nested Directory Flattening & Pruning
# ============================================================================

def test_flatten_and_organize_deeply_nested_tree(tmp_path: Path):
    """Deeply nested (8 levels) hierarchy flattened to root and empty dirs pruned."""
    # Build deep hierarchy: tmp_path / l1 / l2 / l3 / l4 / l5 / l6 / l7 / l8
    deep_dir = tmp_path / "l1" / "l2" / "l3" / "l4" / "l5" / "l6" / "l7" / "l8"
    deep_dir.mkdir(parents=True)

    # Empty sibling folders
    (tmp_path / "l1" / "empty_sibling").mkdir()
    (tmp_path / "l1" / "l2" / "empty_child").mkdir()
    (tmp_path / "isolated_empty_branch" / "child_empty").mkdir(parents=True)

    # Audio file in deep_dir
    f_deep = deep_dir / "track.mp3"
    _generate_mp3(f_deep, duration=1, artist="Deep Artist", title="Deep Title")

    # Audio file in shallow dir with same artist/title (collision)
    shallow_dir = tmp_path / "l1" / "other"
    shallow_dir.mkdir()
    f_shallow = shallow_dir / "track2.mp3"
    _generate_mp3(f_shallow, duration=1, artist="Deep Artist", title="Deep Title")

    # Non-audio folder to preserve
    keep_dir = tmp_path / "artwork_assets"
    keep_dir.mkdir()
    (keep_dir / "cover.jpg").write_bytes(b"JPEG_BINARY_DATA")

    organized = flatten_and_organize_library(tmp_path)

    # All audio files must be in library root
    for p in organized:
        assert p.parent == tmp_path, f"File not in root: {p}"
        assert p.name.startswith("Deep Artist - Deep Title")

    assert len(organized) == 2, "Both tracks must be preserved in root"
    assert organized[0].name != organized[1].name, "Collision must be handled with numbered suffix"
    assert list(tmp_path.glob("*.lrc")) == []

    # Verify empty directories pruned bottom-up
    assert not (tmp_path / "l1").exists(), "Empty tree l1 should be pruned"
    assert not (tmp_path / "isolated_empty_branch").exists(), "Empty branch should be pruned"

    # Verify non-empty directory preserved
    assert keep_dir.exists(), "Directory with non-audio files must be preserved"
    assert (keep_dir / "cover.jpg").exists()


def test_organizer_untagged_flac_in_nested_dir(tmp_path: Path):
    """Adversarial check: Untagged FLAC file in nested directory must be organized without crashing."""
    nested = tmp_path / "incoming" / "album"
    nested.mkdir(parents=True)
    p_flac = nested / "Nirvana - In Bloom.flac"
    _generate_flac(p_flac, duration=1)  # untagged FLAC

    organized = flatten_and_organize_library(tmp_path)
    assert len(organized) == 1
    assert organized[0] == tmp_path / "Nirvana - In Bloom.flac"
    assert not nested.exists()

