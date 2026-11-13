"""Audio deduplication and quality-based retention module for Songetter.

Extracts Mutagen tags, normalizes artist and title metadata, evaluates quality
across bitrate and file size tiers, and resolves duplicates by deleting inferior replicates.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import mutagen

from songetter.config import (
    AUDIO_SUFFIXES,
    DEFAULT_FALLBACK_ARTIST,
    DEFAULT_FALLBACK_TITLE,
)


@dataclass
class AudioMetadata:
    """Audio metadata extracted from file tags and filesystem stats."""

    path: Path
    artist: str
    title: str
    bitrate: int  # in bits per second (bps)
    file_size: int  # in bytes
    duration: float  # in seconds


@dataclass
class DuplicateResolutionResult:
    """Result of resolving a duplicate group in the library."""

    retained: Path
    deleted: List[Path]
    artist: str = ""
    title: str = ""
    reason: str = ""


def normalize_text(text: str) -> str:
    """Normalizes text for deterministic duplicate matching.

    Performs Unicode NFKD normalization, strips combining diacritics,
    lowercases, normalizes quotes/dashes, strips non-alphanumeric punctuation,
    and collapses whitespace.
    """
    if not text:
        return ""

    # 1. Unicode NFKD decomposition
    normalized = unicodedata.normalize("NFKD", text)

    # 2. Strip diacritical marks
    stripped_diacritics = "".join(
        c for c in normalized if not unicodedata.combining(c)
    )

    # 3. Lowercase
    lowered = stripped_diacritics.lower()

    # 4. Straighten quotes and dashes
    standardized = (
        lowered.replace("–", "-")
        .replace("—", "-")
        .replace("“", '"')
        .replace("”", '"')
        .replace("„", '"')
        .replace("‘", "'")
        .replace("’", "'")
        .replace("‚", "'")
    )

    # 5. Strip punctuation except alphanumeric and whitespace
    no_punctuation = re.sub(r"[^\w\s]", "", standardized)

    # 6. Collapse contiguous whitespace
    collapsed = re.sub(r"\s+", " ", no_punctuation).strip()
    return collapsed


def extract_metadata(path: Path) -> AudioMetadata:
    """Extracts audio metadata from path using mutagen with stem fallback.

    Args:
        path: Path to the audio file.

    Returns:
        Populated AudioMetadata instance.
    """
    path = Path(path).resolve()
    file_size = path.stat().st_size if path.exists() else 0

    audio = None
    try:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:
            audio = mutagen.File(str(path))
    except Exception:
        audio = None

    duration = 0.0
    bitrate = 0

    if audio is not None and getattr(audio, "info", None) is not None:
        duration = float(getattr(audio.info, "length", 0.0) or 0.0)
        bitrate = int(getattr(audio.info, "bitrate", 0) or 0)

        # Fallback for uncompressed audio (WAV, AIFF) where bitrate is 0 or absent
        if bitrate <= 0:
            sample_rate = getattr(audio.info, "sample_rate", 0)
            channels = getattr(audio.info, "channels", 0)
            bits_per_sample = getattr(audio.info, "bits_per_sample", 16)
            if sample_rate > 0 and channels > 0:
                bitrate = int(sample_rate * channels * bits_per_sample)
            elif duration > 0 and file_size > 0:
                bitrate = int((file_size * 8) / duration)
    elif duration > 0 and file_size > 0:
        bitrate = int((file_size * 8) / duration)

    artist: Optional[str] = None
    title: Optional[str] = None

    if audio is not None and getattr(audio, "tags", None) is not None:
        # Easy tags
        try:
            if hasattr(audio, "get"):
                artist_val = audio.get("artist")
                if artist_val:
                    artist = artist_val[0] if isinstance(artist_val, list) else str(artist_val)
                title_val = audio.get("title")
                if title_val:
                    title = title_val[0] if isinstance(title_val, list) else str(title_val)
        except Exception:
            pass

        # Raw tags
        if not artist or not title:
            tags = audio.tags
            if hasattr(tags, "get"):
                if not artist:
                    for tag_key in ("artist", "ARTIST", "TPE1", "\xa9ART"):
                        try:
                            val = tags.get(tag_key)
                        except (ValueError, KeyError, TypeError):
                            continue
                        if val:
                            if hasattr(val, "text"):
                                artist = str(val.text[0])
                            elif isinstance(val, list):
                                artist = str(val[0])
                            else:
                                artist = str(val)
                            break
                if not title:
                    for tag_key in ("title", "TITLE", "TIT2", "\xa9nam"):
                        try:
                            val = tags.get(tag_key)
                        except (ValueError, KeyError, TypeError):
                            continue
                        if val:
                            if hasattr(val, "text"):
                                title = str(val.text[0])
                            elif isinstance(val, list):
                                title = str(val[0])
                            else:
                                title = str(val)
                            break

    # Clean extracted tags
    artist = artist.strip() if artist and artist.strip() else None
    title = title.strip() if title and title.strip() else None

    # Fallback to filename stem
    stem = path.stem
    if not artist or not title:
        if " - " in stem:
            parts = stem.split(" - ", 1)
            if not artist:
                artist = parts[0].strip()
            if not title:
                title = parts[1].strip()
        else:
            if not title:
                title = stem.strip()
            if not artist:
                artist = DEFAULT_FALLBACK_ARTIST

    if not artist:
        artist = DEFAULT_FALLBACK_ARTIST
    if not title:
        title = DEFAULT_FALLBACK_TITLE

    return AudioMetadata(
        path=path,
        artist=artist,
        title=title,
        bitrate=bitrate,
        file_size=file_size,
        duration=duration,
    )


def compare_quality(meta_a: AudioMetadata, meta_b: AudioMetadata) -> int:
    """Compares two audio files using the multi-tier quality algorithm.

    Returns:
        +1 if meta_a is superior to meta_b
        -1 if meta_b is superior to meta_a
         0 if quality metrics (bitrate and file_size) are identical
    """
    # Tier 1: Bitrate (bps)
    if meta_a.bitrate > meta_b.bitrate:
        return 1
    if meta_a.bitrate < meta_b.bitrate:
        return -1

    # Tier 2: File size (bytes)
    if meta_a.file_size > meta_b.file_size:
        return 1
    if meta_a.file_size < meta_b.file_size:
        return -1

    # Tier 3: Identical quality metrics
    return 0


def resolve_duplicates(library_dir: Path) -> List[DuplicateResolutionResult]:
    """Scans library_dir for duplicates matching (normalized artist, normalized title).

    Retains the highest-quality file in each duplicate group and deletes inferior
    replicates from disk.
    """
    library_dir = Path(library_dir).resolve()
    if not library_dir.exists() or not library_dir.is_dir():
        return []

    # Gather audio files
    audio_files: List[Path] = [
        p
        for p in library_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ]

    # Group tracks by normalized (artist, title)
    groups: Dict[Tuple[str, str], List[AudioMetadata]] = defaultdict(list)
    for f in audio_files:
        meta = extract_metadata(f)
        group_key = (normalize_text(meta.artist), normalize_text(meta.title))
        groups[group_key].append(meta)

    results: List[DuplicateResolutionResult] = []

    for (norm_artist, norm_title), group in groups.items():
        if len(group) <= 1:
            continue

        # Sort group descending by:
        # 1. Bitrate (bps)
        # 2. File size (bytes)
        # 3. Deterministic tie-breaker: path string
        def _sort_key(m: AudioMetadata) -> Tuple[int, int, str]:
            return (m.bitrate, m.file_size, str(m.path))

        sorted_group = sorted(group, key=_sort_key, reverse=True)
        winner = sorted_group[0]
        losers = sorted_group[1:]

        deleted_paths: List[Path] = []
        for loser in losers:
            try:
                if loser.path.exists():
                    loser.path.unlink()
                    deleted_paths.append(loser.path)
            except OSError:
                pass

        # Determine reason
        if winner.bitrate > losers[0].bitrate:
            reason = f"Retained higher bitrate ({winner.bitrate} bps vs {losers[0].bitrate} bps)"
        elif winner.file_size > losers[0].file_size:
            reason = f"Retained larger file size ({winner.file_size} bytes vs {losers[0].file_size} bytes)"
        else:
            reason = "Identical quality metrics; deterministic tie-breaker applied"

        results.append(
            DuplicateResolutionResult(
                retained=winner.path,
                deleted=deleted_paths,
                artist=winner.artist,
                title=winner.title,
                reason=reason,
            )
        )

    return results
