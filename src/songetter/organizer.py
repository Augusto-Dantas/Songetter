"""Filesystem organization and standardized naming module for Songetter.

Enforces a flat directory structure in the library root, standardizes audio filenames
to '{Artist} - {Title}.{ext}', sanitizes forbidden POSIX/Windows characters, safely
bounds filename byte length to 240 bytes in UTF-8, and recursively prunes empty folders.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

from songetter.config import (
    AUDIO_SUFFIXES,
    DEFAULT_FALLBACK_ARTIST,
    DEFAULT_FALLBACK_TITLE,
    FALLBACK_ARTIST,
    MAX_FILENAME_BYTES,
)
from songetter.deduplicator import extract_metadata


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Safely truncates text to max_bytes in UTF-8 without splitting multi-byte code points."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(". ")
    return truncated if truncated else "Unknown"


def sanitize_filename_component(
    component: str,
    fallback: str = "Unknown",
    max_bytes: int = MAX_FILENAME_BYTES,
) -> str:
    """Sanitizes a filename component for cross-platform filesystem safety.

    Args:
        component: Raw string for artist, title, or filename part.
        fallback: String to return if sanitization results in an empty string.
        max_bytes: Maximum byte length in UTF-8.

    Returns:
        Sanitized and bounded string.
    """
    if not component or not isinstance(component, str):
        return fallback

    # 1. Normalize linebreaks and tabs to spaces so words don't get joined
    normalized = re.sub(r"[\r\n\t]+", " ", component)

    # 2. Strip forbidden characters: [\\/*?:"<>|] and control characters 0x00-0x1F, 0x7F
    stripped = re.sub(r'[\x00-\x1f\x7f/\\?%*:|"<>]', "", normalized)

    # 3. Collapse contiguous whitespace
    collapsed = re.sub(r"\s+", " ", stripped)

    # 4. Strip leading/trailing dots and whitespace
    cleaned = collapsed.strip(". ")

    if not cleaned:
        return fallback

    # 5. Truncate to max_bytes UTF-8
    bounded = truncate_utf8(cleaned, max_bytes)
    return bounded if bounded else fallback


def format_standard_filename(
    artist: str,
    title: str,
    ext: str,
    max_bytes: int = MAX_FILENAME_BYTES,
) -> str:
    """Formats standardized filename: {Artist} - {Title}.{ext} bounded to max_bytes in UTF-8.

    Args:
        artist: Track artist name.
        title: Track title name.
        ext: Audio file extension (with or without leading dot).
        max_bytes: Maximum filename length in UTF-8 bytes (default 240).

    Returns:
        Standardized, sanitized filename string.
    """
    clean_artist = sanitize_filename_component(
        artist, fallback=DEFAULT_FALLBACK_ARTIST, max_bytes=max_bytes
    )
    clean_title = sanitize_filename_component(
        title, fallback=DEFAULT_FALLBACK_TITLE, max_bytes=max_bytes
    )

    clean_ext = ext if ext.startswith(".") else f".{ext}"
    clean_ext = clean_ext.lower()

    # Calculate byte budget for artist and title
    delimiter_bytes = len(" - ".encode("utf-8"))
    ext_bytes = len(clean_ext.encode("utf-8"))
    remaining_budget = max_bytes - delimiter_bytes - ext_bytes

    if remaining_budget < 10:
        # Fallback if remaining budget is extremely constrained
        remaining_budget = 10

    artist_bytes = len(clean_artist.encode("utf-8"))
    title_bytes = len(clean_title.encode("utf-8"))

    if artist_bytes + title_bytes > remaining_budget:
        half_budget = max(5, remaining_budget // 2)
        if artist_bytes > half_budget:
            clean_artist = truncate_utf8(clean_artist, half_budget)
        remaining_for_title = max(5, remaining_budget - len(clean_artist.encode("utf-8")))
        clean_title = truncate_utf8(clean_title, remaining_for_title)

    return f"{clean_artist} - {clean_title}{clean_ext}"


def flatten_and_organize_library(library_dir: Path) -> List[Path]:
    """Flattens nested directory structures and standardizes audio filenames.

    Discovers all audio files recursively, moves them to the library root formatted
    as '{Artist} - {Title}.{ext}', resolves name collisions with numbered suffixes,
    and prunes empty directories bottom-up.

    Args:
        library_dir: Path to the target music library.

    Returns:
        Sorted list of organized audio file Paths in the library root.
    """
    library_dir = Path(library_dir).resolve()
    if not library_dir.exists() or not library_dir.is_dir():
        return []

    # Gather audio files recursively
    audio_files: List[Path] = [
        p
        for p in library_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ]

    organized_paths: List[Path] = []

    for src_path in audio_files:
        meta = extract_metadata(src_path)
        std_filename = format_standard_filename(meta.artist, meta.title, src_path.suffix)
        target_path = library_dir / std_filename

        if src_path == target_path:
            organized_paths.append(target_path)
            continue

        # Handle collisions with existing files in library root
        if target_path.exists() and target_path != src_path:
            base_stem = target_path.stem
            ext = target_path.suffix
            counter = 1
            while target_path.exists() and target_path != src_path:
                target_path = library_dir / f"{base_stem} ({counter}){ext}"
                counter += 1

        # Move audio file
        shutil.move(str(src_path), str(target_path))
        organized_paths.append(target_path)

    # Prune empty directories bottom-up
    for root, dirs, files in os.walk(str(library_dir), topdown=False):
        p_root = Path(root)
        if p_root != library_dir:
            try:
                if not any(p_root.iterdir()):
                    p_root.rmdir()
            except OSError:
                pass

    return sorted(
        [p for p in library_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]
    )
