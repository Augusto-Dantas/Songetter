"""Library directory validation and in-memory audio indexing for Songetter.

Validates filesystem paths, verifies genuine writability via probe files, and
maintains a dual in-memory index by normalized stem and metadata tags.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from songetter.config import AUDIO_SUFFIXES, PROBE_PREFIX
from songetter.deduplicator import extract_metadata, normalize_text


class LibraryError(Exception):
    """Base exception for library errors."""


class InvalidPathError(LibraryError):
    """Raised when directory path is invalid, empty, or nonexistent."""


class NotADirectoryError(LibraryError):
    """Raised when specified path is a regular file, not a directory."""


class PermissionDeniedError(LibraryError):
    """Raised when directory is read-only or probe write test fails."""


def validate_library_directory(
    path: Union[str, Path, None],
    create_if_missing: bool = False,
    create: bool = False,
) -> Path:
    """Validates that path is an accessible, writable directory on the server.

    Args:
        path: Filesystem path to validate.
        create_if_missing: If True, attempts to create directory if nonexistent.
        create: Alias for create_if_missing.

    Returns:
        Resolved absolute Path object.

    Raises:
        InvalidPathError: If path is empty, restricted root, or nonexistent without create=True.
        NotADirectoryError: If path is a regular file.
        PermissionDeniedError: If path lacks write permissions or probe file fails.
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        raise InvalidPathError("Directory path cannot be empty.")

    p = Path(path).expanduser().resolve()

    if str(p) in ("/", "/root"):
        raise InvalidPathError(f"Cannot use restricted system root path: {p}")

    if not p.exists():
        if create or create_if_missing:
            try:
                p.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise PermissionDeniedError(
                    f"Permission denied creating directory: {p} ({exc})"
                ) from exc
            except OSError as exc:
                raise InvalidPathError(
                    f"Failed to create directory: {p} ({exc})"
                ) from exc
        else:
            raise InvalidPathError(f"Directory does not exist: {p}")

    if p.is_file():
        raise NotADirectoryError(f"Specified path is a file, not a directory: {p}")

    if not p.is_dir():
        raise NotADirectoryError(f"Specified path is not a directory: {p}")

    # Genuine write probe test
    probe_file = p / f"{PROBE_PREFIX}{uuid.uuid4().hex}"
    try:
        with open(probe_file, "wb") as f:
            f.write(b"songetter_probe\n")
            f.flush()
    except (PermissionError, OSError) as exc:
        raise PermissionDeniedError(
            f"Directory lacks write permissions: {p} ({exc})"
        ) from exc
    finally:
        try:
            if probe_file.exists():
                probe_file.unlink()
        except OSError:
            pass

    return p


class LibraryIndex:
    """In-memory index of audio files in a library directory."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.root = directory
        self._stem_map: Dict[str, List[Path]] = defaultdict(list)
        self._track_map: Dict[Tuple[str, str], List[Path]] = defaultdict(list)

    @classmethod
    def build(cls, directory: Union[str, Path]) -> "LibraryIndex":
        """Builds an index by scanning all audio files matching AUDIO_SUFFIXES."""
        valid_dir = validate_library_directory(directory)
        index = cls(valid_dir)

        for item in valid_dir.rglob("*"):
            if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES:
                # 1. Index by normalized stem
                stem_norm = normalize_text(item.stem)
                index._stem_map[stem_norm].append(item)

                # 2. Index by extracted metadata tags
                meta = extract_metadata(item)
                track_key = (normalize_text(meta.artist), normalize_text(meta.title))
                index._track_map[track_key].append(item)

        return index

    def contains_stem(self, stem: str) -> bool:
        """Checks if a normalized filename stem exists in the library."""
        if not stem:
            return False
        p = Path(stem)
        clean_stem = p.stem if p.suffix.lower() in AUDIO_SUFFIXES else stem
        return normalize_text(clean_stem) in self._stem_map

    def contains_track(self, artist: str, title: str) -> bool:
        """Checks if a track exists by metadata tags or formatted stem."""
        key = (normalize_text(artist), normalize_text(title))
        if key in self._track_map:
            return True

        stem_key = normalize_text(f"{artist} - {title}")
        if stem_key in self._stem_map:
            return True

        reverse_stem_key = normalize_text(f"{title} - {artist}")
        if reverse_stem_key in self._stem_map:
            return True

        return False

    def find_track_files(self, artist: str, title: str) -> List[Path]:
        """Returns all matching file paths for given artist and title."""
        key = (normalize_text(artist), normalize_text(title))
        matches = list(self._track_map.get(key, []))
        if not matches:
            stem_key = normalize_text(f"{artist} - {title}")
            matches = list(self._stem_map.get(stem_key, []))
            if not matches:
                reverse_stem_key = normalize_text(f"{title} - {artist}")
                matches = list(self._stem_map.get(reverse_stem_key, []))

        seen = set()
        unique_matches: List[Path] = []
        for p in matches:
            if p not in seen:
                seen.add(p)
                unique_matches.append(p)
        return unique_matches
