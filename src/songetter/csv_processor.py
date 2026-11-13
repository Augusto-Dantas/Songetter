"""CSV ingestion and parsing module for Songetter.

Supports RFC 4180 multiline records, delimiter sniffing (',', ';', '\t', '|'),
UTF-8-sig BOM stripping, flexible column alias mapping, and headerless URL ingestion.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union
from urllib.parse import urlparse

from songetter.config import (
    ARTIST_COLUMN_ALIASES,
    DEFAULT_CSV_DELIMITERS,
    TITLE_COLUMN_ALIASES,
    URL_COLUMN_ALIASES,
)


class CSVProcessorError(Exception):
    """Base exception for all CSV processor errors."""


class EmptyCSVError(CSVProcessorError):
    """Raised when CSV content is empty or contains only whitespace."""


class MalformedCSVError(CSVProcessorError):
    """Raised when CSV is structurally malformed or contains binary data."""


class NoValidUrlsError(CSVProcessorError):
    """Raised when no valid URLs can be extracted from the CSV file."""


@dataclass
class TrackRecord:
    """Represents a validated track entry extracted from a CSV file."""

    url: str
    artist: Optional[str] = None
    title: Optional[str] = None
    raw_row: Dict[str, str] = field(default_factory=dict)


def _is_valid_url(candidate: str) -> bool:
    """Checks whether a string is a valid HTTP/HTTPS URL or Spotify URI."""
    if not candidate or not isinstance(candidate, str):
        return False
    candidate = candidate.strip()
    try:
        parsed = urlparse(candidate)
        if parsed.scheme in ("http", "https") and bool(parsed.netloc):
            return True
        if parsed.scheme == "spotify" and bool(parsed.path):
            return True
        return False
    except Exception:
        return False


def _normalize_header_name(header: str) -> str:
    """Normalizes header field names for alias matching."""
    cleaned = re.sub(r"[\s\-_]+", "_", header.lower().strip())
    return cleaned.strip("_")


def _detect_delimiter(sample: str, delimiters: Sequence[str] = DEFAULT_CSV_DELIMITERS) -> str:
    """Sniffs the delimiter from a text sample, restricted to supported delimiters."""
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample, delimiters=delimiters)
        return dialect.delimiter
    except csv.Error:
        # Fallback: count frequency of candidate delimiters in sample
        delimiter_counts = {delim: sample.count(delim) for delim in delimiters}
        best_delim, max_count = max(delimiter_counts.items(), key=lambda item: item[1])
        if max_count > 0:
            return best_delim
        return ","


def parse_csv(
    file_content: Union[str, bytes, None],
    filename: Optional[str] = None,
) -> List[TrackRecord]:
    """Parses uploaded CSV content into a list of TrackRecord instances.

    Args:
        file_content: Raw CSV string or bytes payload.
        filename: Optional source filename for diagnostic logging.

    Returns:
        List of TrackRecord instances with validated URLs.

    Raises:
        EmptyCSVError: If file content is empty or whitespace-only.
        MalformedCSVError: If CSV is corrupt, contains binary data, or lacks required URL column.
        NoValidUrlsError: If no valid URLs are found in the CSV.
    """
    if file_content is None:
        raise EmptyCSVError("Uploaded CSV is empty.")

    # Step 1: Decode content
    if isinstance(file_content, bytes):
        if b"\x00" in file_content:
            raise MalformedCSVError("CSV contains invalid binary data.")
        try:
            text = file_content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = file_content.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = file_content.decode("latin-1")
                except UnicodeDecodeError as exc:
                    raise MalformedCSVError(f"Failed to decode CSV content: {exc}") from exc
    elif isinstance(file_content, str):
        if "\x00" in file_content:
            raise MalformedCSVError("CSV contains invalid binary data.")
        text = file_content.lstrip("\ufeff")
    else:
        raise MalformedCSVError(f"Unsupported file content type: {type(file_content)}")

    if not text.strip():
        raise EmptyCSVError("Uploaded CSV is empty.")

    # Step 2: Sniff delimiter
    sample = text[:4096]
    delimiter = _detect_delimiter(sample, delimiters=DEFAULT_CSV_DELIMITERS)

    # Step 3: RFC 4180 parsing with csv.reader
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True)
        raw_rows = [row for row in reader if any(cell.strip() for cell in row)]
    except csv.Error as exc:
        raise MalformedCSVError(f"Malformed CSV content: {exc}") from exc

    if not raw_rows:
        raise EmptyCSVError("Uploaded CSV contains no non-empty rows.")

    # Step 4: Header detection
    first_row = [c.strip() for c in raw_rows[0]]
    if any(cell.startswith("#") for cell in first_row):
        # First row is a comment, filter out comments and re-check
        raw_rows = [r for r in raw_rows if not (r and r[0].strip().startswith("#"))]
        if not raw_rows:
            raise NoValidUrlsError("CSV contains only comment lines.")
        first_row = [c.strip() for c in raw_rows[0]]

    # If first row contains an actual valid URL, it cannot be a header
    first_row_has_url = any(_is_valid_url(cell) for cell in first_row)

    url_idx: Optional[int] = None
    artist_idx: Optional[int] = None
    title_idx: Optional[int] = None
    has_header = False

    if not first_row_has_url:
        # Check if first row matches known aliases
        normalized_headers = [_normalize_header_name(c) for c in first_row]
        for idx, h in enumerate(normalized_headers):
            raw_h = first_row[idx].lower().strip()
            if (h in URL_COLUMN_ALIASES or raw_h in URL_COLUMN_ALIASES) and url_idx is None:
                url_idx = idx
            elif (h in ARTIST_COLUMN_ALIASES or raw_h in ARTIST_COLUMN_ALIASES) and artist_idx is None:
                artist_idx = idx
            elif (h in TITLE_COLUMN_ALIASES or raw_h in TITLE_COLUMN_ALIASES) and title_idx is None:
                title_idx = idx

        if url_idx is not None:
            has_header = True
        else:
            # Header detected with metadata aliases but no URL alias
            if artist_idx is not None or title_idx is not None:
                raise MalformedCSVError(
                    "CSV contains metadata headers but lacks a required URL column."
                )

    records: List[TrackRecord] = []

    if has_header:
        headers = first_row
        data_rows = raw_rows[1:]
        for row in data_rows:
            if not row or (row and row[0].strip().startswith("#")):
                continue
            raw_row = {
                headers[i]: row[i].strip()
                for i in range(min(len(headers), len(row)))
            }
            raw_url = row[url_idx].strip() if url_idx < len(row) else ""
            if not _is_valid_url(raw_url):
                continue

            raw_artist = (
                row[artist_idx].strip()
                if artist_idx is not None and artist_idx < len(row)
                else None
            )
            raw_title = (
                row[title_idx].strip()
                if title_idx is not None and title_idx < len(row)
                else None
            )

            records.append(
                TrackRecord(
                    url=raw_url,
                    artist=raw_artist if raw_artist else None,
                    title=raw_title if raw_title else None,
                    raw_row=raw_row,
                )
            )
    else:
        # Headerless ingestion
        for row in raw_rows:
            if not row or (row and row[0].strip().startswith("#")):
                continue
            cells = [c.strip() for c in row]
            raw_row = {str(i): cell for i, cell in enumerate(cells)}

            # Locate URL cell
            found_url_idx: Optional[int] = None
            for idx, cell in enumerate(cells):
                if _is_valid_url(cell):
                    found_url_idx = idx
                    break

            if found_url_idx is None:
                continue

            target_url = cells[found_url_idx]
            other_cells = [cells[i] for i in range(len(cells)) if i != found_url_idx and cells[i]]

            parsed_artist: Optional[str] = None
            parsed_title: Optional[str] = None

            if len(other_cells) >= 2:
                parsed_artist = other_cells[0]
                parsed_title = other_cells[1]
            elif len(other_cells) == 1:
                # If 1 remaining column, treat as artist
                parsed_artist = other_cells[0]

            records.append(
                TrackRecord(
                    url=target_url,
                    artist=parsed_artist,
                    title=parsed_title,
                    raw_row=raw_row,
                )
            )

    if not records:
        raise NoValidUrlsError("No valid URLs found in CSV.")

    return records
