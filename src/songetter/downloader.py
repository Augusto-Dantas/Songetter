"""Multi-protocol download pipeline with pre-download indexing, staging, and post-processing.

Supports:
- Batch URL deduplication preserving first-seen order.
- Pre-download skip checks against ``LibraryIndex`` (zero network calls for existing tracks).
- Streaming platform delegation to ``songetter.core.run_download``.
- Direct HTTP/HTTPS streaming via ``httpx.Client`` with temporary ``.part`` staging,
  ``Content-Length`` verification, atomic ``os.replace`` promotion, and guaranteed
  ``.part`` cleanup on error.
- Optional ``yt-dlp`` fallback when a non-localhost, non-audio URL serves HTML.
- Post-download duplicate quality resolution and flat library organization.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Final, List, Optional, Sequence, Tuple, Union
from urllib.parse import unquote, urlparse

import httpx

from songetter import core as _core
from songetter.config import AUDIO_SUFFIXES, MAX_FILENAME_BYTES
from songetter.csv_processor import TrackRecord
from songetter.deduplicator import (
    DuplicateResolutionResult,
    extract_metadata,
    normalize_text,
    resolve_duplicates,
)
from songetter.library import LibraryIndex, extract_url_stem, validate_library_directory
from songetter.organizer import (
    flatten_and_organize_library,
    format_standard_filename,
    sanitize_filename_component,
    truncate_utf8,
)

# Expose run_download at module level as well while supporting patches on either
# songetter.core.run_download or songetter.downloader.run_download.
_ORIG_RUN_DOWNLOAD = _core.run_download
run_download = _ORIG_RUN_DOWNLOAD

_MORNYE_HOSTS: Final[Tuple[str, ...]] = (
    "spotify.com",
    "spotify.link",
    "deezer.com",
    "tidal.com",
    "qobuz.com",
    "music.apple.com",
    "soundcloud.com",
    "jiosaavn.com",
    "music.youtube.com",
)

_LOCAL_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

_CONTENT_TYPE_TO_EXT: Final[Dict[str, str]] = {
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
}

_COLLISION_SUFFIX_RE = re.compile(r"^(?P<base>.+) \(\d+\)$")

__all__ = [
    "Downloader",
    "DownloadSummary",
    "TrackDownloadResult",
    "deduplicate_records",
    "download_tracks",
]


@dataclass
class TrackDownloadResult:
    """Outcome of processing a single ``TrackRecord`` in a download batch."""

    url: str
    artist: Optional[str] = None
    title: Optional[str] = None
    status: str = "downloaded"
    file_path: Optional[Path] = None
    error: Optional[str] = None


@dataclass
class DownloadSummary:
    """Summary of a batch download, deduplication, and organization run."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    tracks: List[TrackDownloadResult] = field(default_factory=list)
    duplicates_resolved: List[DuplicateResolutionResult] = field(default_factory=list)
    organized_files: List[Path] = field(default_factory=list)


def deduplicate_records(records: Sequence[TrackRecord]) -> List[TrackRecord]:
    """Removes duplicate URLs (trimmed) within a batch while preserving first-seen order."""
    seen: set[str] = set()
    unique: List[TrackRecord] = []
    for record in records:
        key = (record.url or "").strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _is_localhost_url(url: str) -> bool:
    host = (urlparse(url.strip()).hostname or "").lower()
    return host in _LOCAL_HOSTS


def _has_audio_extension(url: str) -> bool:
    path = unquote(urlparse(url.strip()).path)
    return Path(path).suffix.lower() in AUDIO_SUFFIXES


def _is_mornye_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme == "spotify":
        return True
    if scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _LOCAL_HOSTS:
        return False
    if _has_audio_extension(url):
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in _MORNYE_HOSTS)


def _parse_content_disposition_filename(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    ext_match = re.search(r"filename\*\s*=\s*([^';]+)'[^']*'([^;]+)", header_value, re.IGNORECASE)
    if ext_match:
        raw_name = unquote(ext_match.group(2).strip().strip('"'))
        return Path(raw_name).name or None
    plain_match = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)', header_value, re.IGNORECASE)
    if plain_match:
        raw_name = (plain_match.group(1) or plain_match.group(2) or "").strip()
        return Path(unquote(raw_name)).name or None
    return None


def _infer_audio_extension(
    original_url: str,
    final_url: str,
    content_disposition: Optional[str],
    content_type: str,
) -> str:
    for candidate_url in (original_url, final_url):
        suffix = Path(unquote(urlparse(candidate_url.strip()).path)).suffix.lower()
        if suffix in AUDIO_SUFFIXES:
            return suffix

    cd_name = _parse_content_disposition_filename(content_disposition)
    if cd_name:
        suffix = Path(cd_name).suffix.lower()
        if suffix in AUDIO_SUFFIXES:
            return suffix

    if content_type in _CONTENT_TYPE_TO_EXT:
        return _CONTENT_TYPE_TO_EXT[content_type]

    return ".mp3"


def _build_initial_filename(
    record: TrackRecord,
    ext: str,
    content_disposition: Optional[str],
    final_url: str,
) -> str:
    artist = record.artist.strip() if record.artist and record.artist.strip() else None
    title = record.title.strip() if record.title and record.title.strip() else None
    if artist and title:
        return format_standard_filename(artist, title, ext)

    stem = extract_url_stem(record.url) or extract_url_stem(final_url)
    if not stem and content_disposition:
        cd_name = _parse_content_disposition_filename(content_disposition)
        if cd_name:
            stem = extract_url_stem(cd_name)

    ext_clean = ext if ext.startswith(".") else f".{ext}"
    ext_clean = ext_clean.lower()
    max_stem_bytes = max(16, MAX_FILENAME_BYTES - len(ext_clean.encode("utf-8")))
    clean_stem = sanitize_filename_component(
        stem,
        fallback=f"track_{uuid.uuid4().hex[:8]}",
        max_bytes=max_stem_bytes,
    )
    return f"{clean_stem}{ext_clean}"


def _rename_surviving_collisions(
    library_dir: Path,
    collision_paths: set[Path],
    duplicates_resolved: List[DuplicateResolutionResult],
) -> None:
    """Renames surviving ' (N)' collision files back to '{Artist} - {Title}.{ext}' when unique."""
    resolved_keys = {
        (normalize_text(d.artist), normalize_text(d.title))
        for d in duplicates_resolved
        if d.artist or d.title
    }
    audio_files = sorted(
        p
        for p in library_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    )
    for path in audio_files:
        match = _COLLISION_SUFFIX_RE.match(path.stem)
        if not match:
            continue

        meta = extract_metadata(path)
        std_name = format_standard_filename(meta.artist, meta.title, path.suffix)
        target_path = library_dir / std_name
        if target_path != path and not target_path.exists():
            os.replace(path, target_path)
            continue

        base_stem = match.group("base")
        base_target = library_dir / f"{base_stem}{path.suffix.lower()}"
        if path in collision_paths or any(
            normalize_text(base_stem) == normalize_text(f"{a} - {t}")
            for a, t in resolved_keys
        ):
            if base_target != path and not base_target.exists():
                os.replace(path, base_target)


class Downloader:
    """Coordinates pre-download library checking, concurrent downloads, and library cleanup."""

    def __init__(
        self,
        library_dir: Union[str, Path],
        max_concurrent: int = 2,
        timeout: float = 30.0,
        skip_existing: bool = True,
        auto_organize: bool = True,
        auto_deduplicate: bool = True,
        services: Optional[List[str]] = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1.")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0.")
        self._library_dir_input = library_dir
        self.library_dir: Path = (
            Path(library_dir)
            if isinstance(library_dir, Path) or (isinstance(library_dir, str) and library_dir.strip())
            else Path("")
        )
        self.max_concurrent = int(max_concurrent)
        self.timeout = float(timeout)
        self.skip_existing = bool(skip_existing)
        self.auto_organize = bool(auto_organize)
        self.auto_deduplicate = bool(auto_deduplicate)
        self.services = list(services) if services is not None else None

    def _invoke_run_download(self, url: str, library_dir: Path) -> _core.RunResult:
        if run_download is not _ORIG_RUN_DOWNLOAD:
            return run_download(
                url,
                library_dir,
                services=self.services,
                max_concurrent_downloads=self.max_concurrent,
            )
        return _core.run_download(
            url,
            library_dir,
            services=self.services,
            max_concurrent_downloads=self.max_concurrent,
        )

    def _try_ytdlp_fallback(
        self,
        url: str,
        record: TrackRecord,
        library_dir: Path,
        reserve_fn: Callable[[str], Tuple[Path, Path, bool]],
        release_fn: Callable[[Optional[Path], Optional[Path], bool], None],
    ) -> Optional[Path]:
        ytdlp_bin = shutil.which("yt-dlp")
        if not ytdlp_bin:
            return None

        tmp_dir = Path(tempfile.mkdtemp(prefix=".songetter_ytdlp_", dir=str(library_dir)))
        dest_path: Optional[Path] = None
        part_path: Optional[Path] = None
        promoted = False
        try:
            cmd = [
                ytdlp_bin,
                "--no-playlist",
                "--no-warnings",
                "--no-write-subs",
                "--no-write-auto-subs",
                "-x",
                "--audio-format",
                "mp3",
                "-o",
                str(tmp_dir / "%(title)s.%(ext)s"),
                url,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(60, int(self.timeout)),
                check=False,
            )
            if proc.returncode != 0:
                return None

            candidates = sorted(
                p
                for p in tmp_dir.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            )
            if not candidates:
                return None

            produced = candidates[0]
            initial_name = _build_initial_filename(record, produced.suffix.lower(), None, url)
            dest_path, part_path, _ = reserve_fn(initial_name)
            shutil.move(str(produced), str(part_path))
            os.replace(part_path, dest_path)
            promoted = True
            return dest_path
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            if not promoted and part_path is not None and part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            if dest_path is not None or part_path is not None:
                release_fn(dest_path, part_path, promoted)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _download_http(
        self,
        record: TrackRecord,
        library_dir: Path,
        reserve_fn: Callable[[str], Tuple[Path, Path, bool]],
        release_fn: Callable[[Optional[Path], Optional[Path], bool], None],
    ) -> Path:
        clean_url = (record.url or "").strip()
        parsed = urlparse(clean_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Unsupported or invalid download URL: {record.url}")

        dest_path: Optional[Path] = None
        part_path: Optional[Path] = None
        promoted = False

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                with client.stream("GET", clean_url) as response:
                    if response.status_code >= 400:
                        response.raise_for_status()

                    content_type = (
                        (response.headers.get("content-type") or "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        if not _is_localhost_url(clean_url) and not _has_audio_extension(clean_url):
                            fallback_path = self._try_ytdlp_fallback(
                                clean_url, record, library_dir, reserve_fn, release_fn
                            )
                            if fallback_path is not None:
                                return fallback_path
                        raise ValueError(
                            f"Expected audio stream but received HTML ({content_type}) from {clean_url}"
                        )

                    ext = _infer_audio_extension(
                        clean_url,
                        str(response.url),
                        response.headers.get("content-disposition"),
                        content_type,
                    )
                    initial_name = _build_initial_filename(
                        record,
                        ext,
                        response.headers.get("content-disposition"),
                        str(response.url),
                    )
                    dest_path, part_path, _ = reserve_fn(initial_name)

                    expected_length: Optional[int] = None
                    raw_cl = response.headers.get("content-length")
                    if raw_cl is not None:
                        try:
                            expected_length = int(raw_cl.strip())
                        except ValueError:
                            expected_length = None

                    bytes_written = 0
                    with open(part_path, "wb") as out_file:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            if chunk:
                                out_file.write(chunk)
                                bytes_written += len(chunk)
                        out_file.flush()

                    if expected_length is not None and bytes_written < expected_length:
                        raise IOError(
                            f"Truncated stream from {clean_url}: "
                            f"received {bytes_written} of {expected_length} expected bytes"
                        )
                    if bytes_written == 0:
                        raise IOError(f"Empty response body (0 bytes) from {clean_url}")

            assert part_path is not None and dest_path is not None
            os.replace(part_path, dest_path)
            promoted = True
            return dest_path
        finally:
            if not promoted and part_path is not None and part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            release_fn(dest_path, part_path, promoted)

    def _download_mornye(self, record: TrackRecord, library_dir: Path) -> TrackDownloadResult:
        clean_url = (record.url or "").strip()
        run_res = self._invoke_run_download(clean_url, library_dir)

        first_file: Optional[Path] = None
        for item in getattr(run_res, "tracks", []) or []:
            if isinstance(item, dict) and item.get("file_path"):
                first_file = Path(item["file_path"])
                break

        if run_res.downloaded > 0:
            return TrackDownloadResult(
                url=record.url,
                artist=record.artist,
                title=record.title,
                status="downloaded",
                file_path=first_file,
                error=run_res.error if run_res.failed > 0 else None,
            )
        if run_res.skipped > 0 and run_res.failed == 0:
            return TrackDownloadResult(
                url=record.url,
                artist=record.artist,
                title=record.title,
                status="skipped",
                file_path=first_file,
            )
        return TrackDownloadResult(
            url=record.url,
            artist=record.artist,
            title=record.title,
            status="failed",
            file_path=first_file,
            error=run_res.error or "Mornye download failed.",
        )

    def _download_one(
        self,
        record: TrackRecord,
        library_dir: Path,
        reserve_fn: Callable[[str], Tuple[Path, Path, bool]],
        release_fn: Callable[[Optional[Path], Optional[Path], bool], None],
    ) -> TrackDownloadResult:
        clean_url = (record.url or "").strip()
        try:
            if _is_mornye_url(clean_url):
                return self._download_mornye(record, library_dir)
            dest_path = self._download_http(record, library_dir, reserve_fn, release_fn)
            return TrackDownloadResult(
                url=record.url,
                artist=record.artist,
                title=record.title,
                status="downloaded",
                file_path=dest_path,
            )
        except Exception as exc:
            return TrackDownloadResult(
                url=record.url,
                artist=record.artist,
                title=record.title,
                status="failed",
                error=str(exc),
            )

    def download(self, records: Sequence[TrackRecord]) -> DownloadSummary:
        """Executes pre-download checks, concurrent downloads, dedup, and flat organization."""
        lib_dir = validate_library_directory(self._library_dir_input, create=True)
        self.library_dir = lib_dir

        unique_records = deduplicate_records(records)
        if not unique_records:
            return DownloadSummary()

        index = LibraryIndex.build(lib_dir)

        track_results_by_idx: Dict[int, TrackDownloadResult] = {}
        pending: List[Tuple[int, TrackRecord]] = []

        for idx, record in enumerate(unique_records):
            artist = record.artist.strip() if record.artist else None
            title = record.title.strip() if record.title else None
            if self.skip_existing:
                matched_files: List[Path] = []
                matched = False
                if artist and title and index.contains_track(artist, title):
                    matched = True
                    matched_files = index.find_track_files(artist, title)
                elif index.contains_stem(record.url):
                    matched = True
                    extracted = extract_url_stem(record.url)
                    if extracted:
                        stem_map = getattr(index, "_stem_map", {})
                        matched_files = list(stem_map.get(normalize_text(extracted), []))
                if matched:
                    track_results_by_idx[idx] = TrackDownloadResult(
                        url=record.url,
                        artist=record.artist,
                        title=record.title,
                        status="skipped",
                        file_path=matched_files[0] if matched_files else None,
                    )
                    continue
            pending.append((idx, record))

        reservation_lock = threading.Lock()
        reserved_paths: set[Path] = set()
        reserved_parts: set[Path] = set()
        collision_paths: set[Path] = set()

        def reserve_dest(initial_filename: str) -> Tuple[Path, Path, bool]:
            with reservation_lock:
                base_path = lib_dir / initial_filename
                base_part = lib_dir / f"{initial_filename}.part"
                if (
                    not base_path.exists()
                    and base_path not in reserved_paths
                    and not base_part.exists()
                    and base_part not in reserved_parts
                ):
                    reserved_paths.add(base_path)
                    reserved_parts.add(base_part)
                    return base_path, base_part, False

                p = Path(initial_filename)
                stem = p.stem
                suffix = p.suffix
                counter = 1
                while True:
                    suffix_tag = f" ({counter}){suffix}"
                    max_stem = max(5, MAX_FILENAME_BYTES - len(suffix_tag.encode("utf-8")))
                    bounded_stem = truncate_utf8(stem, max_stem)
                    candidate_name = f"{bounded_stem}{suffix_tag}"
                    candidate = lib_dir / candidate_name
                    part_candidate = lib_dir / f"{candidate_name}.part"
                    if (
                        not candidate.exists()
                        and candidate not in reserved_paths
                        and not part_candidate.exists()
                        and part_candidate not in reserved_parts
                    ):
                        reserved_paths.add(candidate)
                        reserved_parts.add(part_candidate)
                        collision_paths.add(candidate)
                        return candidate, part_candidate, True
                    counter += 1

        def release_dest(
            dest_path: Optional[Path],
            part_path: Optional[Path],
            promoted: bool,
        ) -> None:
            with reservation_lock:
                if part_path is not None:
                    reserved_parts.discard(part_path)
                if not promoted and dest_path is not None:
                    reserved_paths.discard(dest_path)
                    collision_paths.discard(dest_path)

        mornye_invoked = any(_is_mornye_url((rec.url or "").strip()) for _, rec in pending)

        if pending:
            worker_count = min(self.max_concurrent, len(pending))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_idx = {
                    executor.submit(
                        self._download_one,
                        record,
                        lib_dir,
                        reserve_dest,
                        release_dest,
                    ): idx
                    for idx, record in pending
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    rec = unique_records[idx]
                    try:
                        track_results_by_idx[idx] = future.result()
                    except Exception as exc:
                        track_results_by_idx[idx] = TrackDownloadResult(
                            url=rec.url,
                            artist=rec.artist,
                            title=rec.title,
                            status="failed",
                            error=str(exc),
                        )

        ordered_results = [track_results_by_idx[i] for i in range(len(unique_records))]
        downloaded_count = sum(1 for r in ordered_results if r.status == "downloaded")
        skipped_count = sum(1 for r in ordered_results if r.status == "skipped")
        failed_count = sum(1 for r in ordered_results if r.status == "failed")

        pre_org_keys: Dict[int, Tuple[str, str]] = {}
        for idx, res in enumerate(ordered_results):
            if res.status == "downloaded" and res.file_path is not None and res.file_path.exists():
                meta = extract_metadata(res.file_path)
                pre_org_keys[idx] = (normalize_text(meta.artist), normalize_text(meta.title))

        duplicates_resolved: List[DuplicateResolutionResult] = []
        organized_files: List[Path] = []

        if downloaded_count > 0 or mornye_invoked:
            if self.auto_deduplicate:
                duplicates_resolved.extend(resolve_duplicates(lib_dir))
            if self.auto_organize:
                organized_files = flatten_and_organize_library(lib_dir)
            if self.auto_deduplicate and self.auto_organize:
                duplicates_resolved.extend(resolve_duplicates(lib_dir))
                _rename_surviving_collisions(lib_dir, collision_paths, duplicates_resolved)
                organized_files = sorted(
                    p
                    for p in lib_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
                )

            self._refresh_result_paths(
                lib_dir,
                ordered_results,
                pre_org_keys,
                duplicates_resolved,
            )

        return DownloadSummary(
            downloaded=downloaded_count,
            skipped=skipped_count,
            failed=failed_count,
            tracks=ordered_results,
            duplicates_resolved=duplicates_resolved,
            organized_files=organized_files,
        )

    @staticmethod
    def _refresh_result_paths(
        library_dir: Path,
        results: List[TrackDownloadResult],
        pre_org_keys: Dict[int, Tuple[str, str]],
        duplicates_resolved: List[DuplicateResolutionResult],
    ) -> None:
        final_by_key: Dict[Tuple[str, str], Path] = {}
        for path in sorted(library_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
                meta = extract_metadata(path)
                key = (normalize_text(meta.artist), normalize_text(meta.title))
                final_by_key.setdefault(key, path)

        deleted_to_retained: Dict[Path, Path] = {}
        for dup in duplicates_resolved:
            retained = dup.retained.resolve()
            for del_path in dup.deleted:
                deleted_to_retained[del_path.resolve()] = retained

        for idx, res in enumerate(results):
            if res.status != "downloaded":
                continue
            if res.file_path is not None and res.file_path.exists():
                continue

            if res.file_path is not None:
                cur = res.file_path.resolve()
                while cur in deleted_to_retained:
                    cur = deleted_to_retained[cur]
                if cur.exists():
                    res.file_path = cur
                    continue

            key = pre_org_keys.get(idx)
            if key is None and res.artist and res.title:
                key = (normalize_text(res.artist), normalize_text(res.title))
            if key is not None and key in final_by_key:
                res.file_path = final_by_key[key]


def download_tracks(
    records: Sequence[TrackRecord],
    library_dir: Union[str, Path],
    **kwargs: Any,
) -> DownloadSummary:
    """Convenience function to instantiate ``Downloader`` and execute ``download(records)``."""
    downloader = Downloader(library_dir=library_dir, **kwargs)
    return downloader.download(records)
