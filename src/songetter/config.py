"""Central configuration constants for Songetter domain modules."""

from __future__ import annotations

from typing import Final, List, Set, Tuple

# Audio formats recognized by library scanning, deduplication, and organization
AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset({
    ".flac",
    ".mp3",
    ".m4a",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".wv",
    ".tta",
})

# Probe file prefix for filesystem writability testing
PROBE_PREFIX: Final[str] = ".songetter_probe_"

# Filesystem bounds and naming safety
MAX_FILENAME_BYTES: Final[int] = 240
FORBIDDEN_CHARS_PATTERN: Final[str] = r'[\x00-\x1f\x7f/\\?%*:|"<>]'

# Supported delimiters for CSV sniffing
DEFAULT_CSV_DELIMITERS: Final[List[str]] = [",", ";", "\t", "|"]
SUPPORTED_DELIMITERS: Final[List[str]] = DEFAULT_CSV_DELIMITERS

# Column aliases for CSV parsing (case-insensitive, whitespace-normalized)
URL_COLUMN_ALIASES: Final[frozenset[str]] = frozenset({
    "url",
    "link",
    "download_url",
    "track_url",
    "spotify_url",
    "uri",
    "song_url",
    "track_uri",
    "stream_url",
    "source_url",
    "href",
    "trackuri",
    "permalink",
    "track link",
    "spotify url",
})
URL_ALIASES: Final[frozenset[str]] = URL_COLUMN_ALIASES

ARTIST_COLUMN_ALIASES: Final[frozenset[str]] = frozenset({
    "artist",
    "artists",
    "track_artist",
    "singer",
    "author",
    "performer",
    "artist_name",
    "artist_names",
    "albumartist",
    "artist(s)",
    "track artist",
})
ARTIST_ALIASES: Final[frozenset[str]] = ARTIST_COLUMN_ALIASES

TITLE_COLUMN_ALIASES: Final[frozenset[str]] = frozenset({
    "title",
    "track",
    "track_title",
    "song",
    "name",
    "song_title",
    "track_name",
    "track title",
    "track name",
})
TITLE_ALIASES: Final[frozenset[str]] = TITLE_COLUMN_ALIASES

# Fallback artist/title constants
DEFAULT_FALLBACK_ARTIST: Final[str] = "Unknown Artist"
DEFAULT_FALLBACK_TITLE: Final[str] = "Unknown Title"
FALLBACK_ARTIST: Final[str] = "Unknown"
FALLBACK_TITLE: Final[str] = "Unknown"
