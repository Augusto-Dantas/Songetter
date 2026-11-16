"""Unified command-line interface for Songetter."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import inspect
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from . import core as _core
from . import csv_processor as _csv_processor
from . import deduplicator as _deduplicator
from . import downloader as _downloader
from . import library as _library
from . import organizer as _organizer
from . import search as _search
from . import tui as _tui
from .config import AUDIO_SUFFIXES, SUPPORTED_SOURCE_HOSTS
from .core import DEFAULT_SERVICES
from .csv_processor import CSVProcessorError, TrackRecord
from .library import LibraryError
from .search import SearchError

_ORIG_CONFIGURE_MORNYE_REGISTRY = _core.configure_mornye_registry
_ORIG_PARSE_LINKS = _core.parse_links
_ORIG_PARSE_SOURCE_LINKS = _core.parse_source_links
_ORIG_RUN_DOWNLOAD = _core.run_download
_ORIG_WRITE_M3U8 = _core.write_m3u8
_ORIG_PARSE_CSV = _csv_processor.parse_csv
_ORIG_DOWNLOADER = _downloader.Downloader
_ORIG_LIST_SEARCH_SOURCES = _search.list_search_sources
_ORIG_SEARCH_TRACKS_WITH_ERRORS = _search.search_tracks_with_errors
_ORIG_SEARCH_RESULTS_TO_CSV = _search.search_results_to_csv
_ORIG_VALIDATE_LIBRARY_DIRECTORY = _library.validate_library_directory
_ORIG_RESOLVE_DUPLICATES = _deduplicator.resolve_duplicates
_ORIG_FLATTEN_AND_ORGANIZE_LIBRARY = _organizer.flatten_and_organize_library
_ORIG_RUN_TUI = _tui.run_tui

configure_mornye_registry = _ORIG_CONFIGURE_MORNYE_REGISTRY
parse_links = _ORIG_PARSE_LINKS
parse_source_links = _ORIG_PARSE_SOURCE_LINKS
run_download = _ORIG_RUN_DOWNLOAD
write_m3u8 = _ORIG_WRITE_M3U8
parse_csv = _ORIG_PARSE_CSV
Downloader = _ORIG_DOWNLOADER
list_search_sources = _ORIG_LIST_SEARCH_SOURCES
search_tracks_with_errors = _ORIG_SEARCH_TRACKS_WITH_ERRORS
search_results_to_csv = _ORIG_SEARCH_RESULTS_TO_CSV
validate_library_directory = _ORIG_VALIDATE_LIBRARY_DIRECTORY
resolve_duplicates = _ORIG_RESOLVE_DUPLICATES
flatten_and_organize_library = _ORIG_FLATTEN_AND_ORGANIZE_LIBRARY
run_tui = _ORIG_RUN_TUI

SUBCOMMANDS = ("tui", "serve", "web", "download", "organize", "dedup", "search", "doctor")


def _pick(cli_obj: Any, module_obj: Any, orig_obj: Any) -> Any:
    """Resolve patched symbols regardless of whether tests patch cli or source module."""
    return cli_obj if cli_obj is not orig_obj else module_obj


def _parse_comma_tokens(raw_values: list[str] | None) -> list[str] | None:
    """Normalize repeatable or comma-separated flag values while preserving order."""
    if raw_values is None:
        return None
    tokens: list[str] = []
    for entry in raw_values:
        for part in entry.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
    return tokens


def _parse_search_sources(raw_sources: list[str] | None) -> list[str]:
    """Normalize repeatable/comma-separated search source flags."""
    tokens = _parse_comma_tokens(raw_sources)
    return tokens if tokens else ["deezer"]


def _parse_services(raw_services: list[str] | None) -> list[str] | None:
    """Normalize repeatable or comma-separated --service flags."""
    return _parse_comma_tokens(raw_services)


def _format_duration(seconds: int | None) -> str:
    """Format an integer duration in seconds as M:SS or H:MM:SS."""
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _print_download_summary(summary: Any) -> None:
    """Print batch download metrics, organization counts, and per-track errors."""
    print(
        f"Completed: downloaded={summary.downloaded} "
        f"skipped={summary.skipped} failed={summary.failed}"
    )
    duplicates_resolved = getattr(summary, "duplicates_resolved", None)
    if duplicates_resolved:
        print(f"Deduplicated groups: {len(duplicates_resolved)}")
    organized_files = getattr(summary, "organized_files", None)
    if organized_files:
        print(f"Organized files: {len(organized_files)}")
    for track in getattr(summary, "tracks", []) or []:
        error = getattr(track, "error", None)
        if error:
            label = getattr(track, "title", None) or getattr(track, "url", "unknown")
            print(f"  ERROR [{label}]: {error}")


def _summary_to_dict(summary: Any) -> dict[str, Any]:
    """Serialize a DownloadSummary (or test fake) to a JSON-safe dictionary."""
    tracks_payload = []
    for track in getattr(summary, "tracks", []) or []:
        file_path = getattr(track, "file_path", None)
        tracks_payload.append(
            {
                "url": getattr(track, "url", ""),
                "artist": getattr(track, "artist", None),
                "title": getattr(track, "title", None),
                "status": getattr(track, "status", None),
                "file_path": str(file_path) if file_path is not None else None,
                "error": getattr(track, "error", None),
            }
        )
    return {
        "downloaded": getattr(summary, "downloaded", 0),
        "skipped": getattr(summary, "skipped", 0),
        "failed": getattr(summary, "failed", 0),
        "duplicates_resolved": len(getattr(summary, "duplicates_resolved", []) or []),
        "organized_files": [
            str(p) for p in (getattr(summary, "organized_files", []) or [])
        ],
        "tracks": tracks_payload,
    }


def _run_downloader(
    records: list[Any],
    output: Path,
    max_concurrent: int,
    services: list[str] | None,
    *,
    skip_existing: bool = True,
    auto_deduplicate: bool = True,
    auto_organize: bool = True,
) -> Any:
    """Invoke Downloader while remaining compatible with minimal test doubles."""
    downloader_cls = _pick(Downloader, _downloader.Downloader, _ORIG_DOWNLOADER)
    kwargs: dict[str, Any] = {"max_concurrent": max_concurrent}
    if services is not None:
        kwargs["services"] = services

    try:
        sig = inspect.signature(downloader_cls)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params = {}
        accepts_var_kw = True

    for key, value in (
        ("skip_existing", skip_existing),
        ("auto_deduplicate", auto_deduplicate),
        ("auto_organize", auto_organize),
    ):
        if accepts_var_kw or key in params:
            # Omit defaults so minimal mocks that only expect (output, max_concurrent) still work.
            if not value or key in params:
                kwargs[key] = value

    if not accepts_var_kw:
        kwargs = {k: v for k, v in kwargs.items() if k in params}

    instance = downloader_cls(output, **kwargs)
    return instance.download(records)


def _binary_search_path() -> str:
    """Build a search PATH including the active Python virtualenv bin directory."""
    candidates: list[str] = []
    for candidate in (
        str(Path(sys.executable).parent),
        str(Path(sys.prefix) / "bin"),
        str(Path(sys.prefix) / "Scripts"),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    env_path = os.environ.get("PATH", "")
    if env_path:
        candidates.append(env_path)
    return os.pathsep.join(candidates)


def _package_version(dist_name: str, import_name: str | None = None) -> str | None:
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        pass
    mod_name = import_name or dist_name.replace("-", "_")
    try:
        mod = importlib.import_module(mod_name)
        ver = getattr(mod, "__version__", None)
        return str(ver) if ver is not None else "installed"
    except Exception:
        return None


def collect_doctor_report(library_dir: Path | str | None = None) -> dict[str, Any]:
    """Inspect runtime binaries, Python packages, and library directory writability."""
    search_path = _binary_search_path()

    def _binary_info(names: tuple[str, ...], dist_name: str | None = None) -> dict[str, Any]:
        resolved: str | None = None
        for cmd in names:
            resolved = shutil.which(cmd, path=search_path)
            if resolved:
                break
        pkg_ver = _package_version(dist_name) if dist_name else None
        available = bool(resolved or pkg_ver)
        info: dict[str, Any] = {
            "available": available,
            "ok": available,
            "path": resolved,
        }
        if dist_name:
            info["version"] = pkg_ver
        return info

    def _pkg_info(dist_name: str, import_name: str | None = None) -> dict[str, Any]:
        ver = _package_version(dist_name, import_name)
        return {
            "available": ver is not None,
            "ok": ver is not None,
            "version": ver,
        }

    raw_lib = (
        library_dir
        if library_dir is not None
        else (os.environ.get("SONGETTER_LIBRARY_DIR") or "Music")
    )
    target_lib = Path(raw_lib).expanduser().resolve()
    validate_fn = _pick(
        validate_library_directory,
        _library.validate_library_directory,
        _ORIG_VALIDATE_LIBRARY_DIRECTORY,
    )

    exists = target_lib.exists()
    writable = False
    parent_writable = False
    lib_error: str | None = None

    if exists:
        try:
            validate_fn(target_lib, create=False)
            writable = True
            parent_writable = True
        except LibraryError as exc:
            lib_error = str(exc)
    else:
        ancestor = target_lib.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        parent_writable = ancestor.exists() and ancestor.is_dir() and os.access(
            ancestor, os.W_OK | os.X_OK
        )
        lib_error = f"Directory does not exist: {target_lib}"

    report: dict[str, Any] = {
        "python": {
            "available": True,
            "ok": True,
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "mornye": _binary_info(("mornye",), dist_name="mornye"),
        "ffmpeg": _binary_info(("ffmpeg",)),
        "ffprobe": _binary_info(("ffprobe",)),
        "node": _binary_info(("node", "nodejs")),
        "yt-dlp": _binary_info(("yt-dlp",), dist_name="yt-dlp"),
        "textual": _pkg_info("textual"),
        "httpx": _pkg_info("httpx"),
        "fastapi": _pkg_info("fastapi"),
        "mutagen": _pkg_info("mutagen"),
        "library_dir": {
            "path": str(target_lib),
            "exists": exists,
            "writable": writable,
            "parent_writable": parent_writable,
            "ok": writable,
            "error": lib_error,
        },
    }
    report["ok"] = all(
        report[key]["ok"]
        for key in ("python", "mornye", "ffmpeg", "ffprobe", "textual", "httpx", "fastapi", "mutagen")
    ) and (writable or (not exists and parent_writable))
    return report


def _handle_tui(output: str | None = None) -> int:
    """Launch the interactive Textual TUI."""
    tui_fn = _pick(run_tui, _tui.run_tui, _ORIG_RUN_TUI)
    lib_arg = Path(output).expanduser() if output else None
    result = tui_fn(lib_arg)
    return int(result) if isinstance(result, int) else 0


def _handle_serve(
    host: str,
    port: int,
    output: str | None = None,
    reload: bool = False,
) -> int:
    """Start the FastAPI web application via uvicorn."""
    if output:
        os.environ["SONGETTER_LIBRARY_DIR"] = str(Path(output).expanduser().resolve())
    import uvicorn

    uvicorn.run(
        "songetter.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )
    return 0


def _handle_doctor(output: str | None, as_json: bool) -> int:
    """Run and display environment, binary, package, and library diagnostics."""
    target = Path(output).expanduser().resolve() if output else None
    report = collect_doctor_report(target)
    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    print("Songetter Doctor")
    print(f"  python   : OK ({report['python']['version']} - {report['python']['executable']})")
    for key in ("mornye", "ffmpeg", "ffprobe", "node", "yt-dlp"):
        item = report[key]
        status = "OK" if item["available"] else "MISSING"
        detail = item.get("path") or item.get("version") or "not found"
        print(f"  {key:<9}: {status} ({detail})")
    for key in ("textual", "httpx", "fastapi", "mutagen"):
        item = report[key]
        status = "OK" if item["available"] else "MISSING"
        detail = item.get("version") or "not installed"
        print(f"  {key:<9}: {status} ({detail})")
    lib = report["library_dir"]
    lib_status = "WRITABLE" if lib["writable"] else ("CREABLE" if lib["parent_writable"] else "ERROR")
    print(f"  library  : {lib_status} ({lib['path']})")
    if lib.get("error"):
        print(f"             note: {lib['error']}")
    return 0


def _handle_list_sources(as_json: bool) -> int:
    """Display registered search sources and authentication/configuration state."""
    list_fn = _pick(list_search_sources, _search.list_search_sources, _ORIG_LIST_SEARCH_SOURCES)
    sources = list_fn()
    if as_json:
        print(json.dumps(sources, indent=2))
        return 0

    for src in sources:
        src_id = src.get("id", "")
        display_name = src.get("name", src_id)
        requires_auth = bool(src.get("requires_auth", False))
        configured = bool(src.get("configured", True))
        if requires_auth:
            auth_status = (
                "auth required (configured)" if configured else "auth required (not configured)"
            )
        else:
            auth_status = "no auth required (configured)"
        aliases = src.get("aliases") or []
        alias_text = f" [aliases: {', '.join(aliases)}]" if aliases else ""
        print(f"{src_id}: {display_name} — {auth_status}{alias_text}")
        description = src.get("description")
        if description:
            print(f"  {description}")
    return 0


def _handle_search_common(
    *,
    query: str,
    raw_sources: list[str] | None,
    limit: int,
    search_type: str,
    deduplicate: bool,
    download: bool,
    export_csv: str | None,
    output: str,
    max_concurrent: int,
    services: list[str] | None,
    as_json: bool,
) -> int:
    """Shared handler for legacy --search and `songetter search` subcommand."""
    sources = _parse_search_sources(raw_sources)
    search_fn = _pick(
        search_tracks_with_errors,
        _search.search_tracks_with_errors,
        _ORIG_SEARCH_TRACKS_WITH_ERRORS,
    )
    results, errors = search_fn(
        query,
        sources=sources,
        limit=limit,
        search_type=search_type,
        deduplicate=deduplicate,
    )

    exported_csv_path: Path | None = None
    if export_csv:
        to_csv_fn = _pick(
            search_results_to_csv,
            _search.search_results_to_csv,
            _ORIG_SEARCH_RESULTS_TO_CSV,
        )
        exported_csv_path = Path(export_csv).expanduser().resolve()
        exported_csv_path.parent.mkdir(parents=True, exist_ok=True)
        exported_csv_path.write_text(to_csv_fn(results), encoding="utf-8")

    download_summary = None
    if download and results:
        resolved_output = Path(output).expanduser().resolve()
        records = [item.to_track_record() for item in results]
        download_summary = _run_downloader(
            records,
            output=resolved_output,
            max_concurrent=max_concurrent,
            services=services,
        )

    if as_json:
        payload: dict[str, Any] = {
            "query": query,
            "sources": sources,
            "search_type": search_type,
            "count": len(results),
            "results": [item.to_dict() for item in results],
            "errors": errors,
        }
        if exported_csv_path is not None:
            payload["exported_csv"] = str(exported_csv_path)
        if download:
            payload["download"] = (
                _summary_to_dict(download_summary)
                if download_summary is not None
                else {"downloaded": 0, "skipped": 0, "failed": 0, "tracks": []}
            )
        print(json.dumps(payload, indent=2))
        return 0

    if not results:
        print(f"No results found for '{query}'.")
    else:
        for idx, item in enumerate(results, 1):
            artist = item.artist or "Unknown Artist"
            title = item.title or "Unknown Title"
            parts = [f"[{idx}] [{item.source}] {artist} - {title}"]
            if item.album:
                parts.append(f"({item.album})")
            duration_str = _format_duration(item.duration_seconds)
            if duration_str:
                parts.append(f"[{duration_str}]")
            if item.quality_hint:
                parts.append(f"[{item.quality_hint}]")
            if item.url:
                parts.append(f"- {item.url}")
            print(" ".join(parts))

    for src, err in errors.items():
        print(f"Warning [{src}]: {err}")

    if exported_csv_path is not None:
        print(f"Exported CSV: {exported_csv_path}")

    if download:
        if download_summary is None:
            print("No search results to download.")
        else:
            _print_download_summary(download_summary)

    return 0


def _handle_csv(
    csv_file: str | Path,
    output: str,
    max_concurrent: int,
    services: list[str] | None,
    as_json: bool,
) -> int:
    """Parse a CSV track list and execute Downloader."""
    csv_path = Path(csv_file).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if csv_path.is_dir():
        raise IsADirectoryError(f"Expected CSV file but found directory: {csv_path}")

    parse_csv_fn = _pick(parse_csv, _csv_processor.parse_csv, _ORIG_PARSE_CSV)
    records = parse_csv_fn(csv_path.read_bytes(), filename=csv_path.name)
    resolved_output = Path(output).expanduser().resolve()
    summary = _run_downloader(
        records,
        output=resolved_output,
        max_concurrent=max_concurrent,
        services=services,
    )

    if as_json:
        payload = {
            "csv": str(csv_path),
            "parsed_records": len(records),
            **_summary_to_dict(summary),
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Parsed {len(records)} track(s) from {csv_path.name}")
        _print_download_summary(summary)
    return 0


def _is_direct_http_audio_url(candidate: str) -> bool:
    """Return True for localhost HTTP URLs or URLs pointing directly to audio extensions."""
    parsed = urlparse(candidate.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    suffix = Path(parsed.path).suffix.lower()
    return suffix in AUDIO_SUFFIXES and host not in SUPPORTED_SOURCE_HOSTS


def _normalize_url_token(candidate: str) -> str:
    cleaned = candidate.strip()
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "spotify" and parsed.path:
        return cleaned
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid music link: {cleaned}")
    if host == "open.spotify.com":
        parse_fn = _pick(parse_links, _core.parse_links, _ORIG_PARSE_LINKS)
        return parse_fn(cleaned)[0]
    return cleaned


def _parse_download_inputs(links: list[str], input_file: str | None) -> list[TrackRecord]:
    """Parse positional links and/or a CSV or text link file into TrackRecords."""
    records: list[TrackRecord] = []
    seen_urls: set[str] = set()

    if input_file:
        in_path = Path(input_file).expanduser().resolve()
        if not in_path.exists():
            raise FileNotFoundError(f"Input file not found: {in_path}")
        if in_path.is_dir():
            raise IsADirectoryError(f"Expected input file but found directory: {in_path}")

        raw_bytes = in_path.read_bytes()
        is_csv_file = in_path.suffix.lower() == ".csv"
        if not is_csv_file:
            preview = raw_bytes[:2048].decode("utf-8", errors="ignore")
            first_line = next(
                (ln.strip() for ln in preview.splitlines() if ln.strip() and not ln.strip().startswith("#")),
                "",
            )
            if any(delim in first_line for delim in (",", ";", "\t", "|")):
                is_csv_file = True

        if is_csv_file:
            parse_csv_fn = _pick(parse_csv, _csv_processor.parse_csv, _ORIG_PARSE_CSV)
            for rec in parse_csv_fn(raw_bytes, filename=in_path.name):
                norm = _normalize_url_token(rec.url)
                if norm not in seen_urls:
                    seen_urls.add(norm)
                    records.append(
                        TrackRecord(url=norm, artist=rec.artist, title=rec.title, raw_row=rec.raw_row)
                    )
        else:
            text = raw_bytes.decode("utf-8-sig")
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                for token in re.split(r"[\s,;]+", stripped):
                    token = token.strip()
                    if not token:
                        continue
                    norm = _normalize_url_token(token)
                    if norm not in seen_urls:
                        seen_urls.add(norm)
                        records.append(TrackRecord(url=norm))

    for raw_link in links:
        for token in re.split(r"[\s,;]+", raw_link.strip()):
            token = token.strip()
            if not token:
                continue
            norm = _normalize_url_token(token)
            if norm not in seen_urls:
                seen_urls.add(norm)
                records.append(TrackRecord(url=norm))

    if not records:
        raise ValueError("Provide at least one music URL or -i/--input file.")
    return records


def _handle_download_subcommand(args: argparse.Namespace, services: list[str] | None) -> int:
    """Execute the `songetter download` subcommand using Downloader."""
    records = _parse_download_inputs(list(args.links), args.input)
    resolved_output = Path(args.output).expanduser().resolve()
    summary = _run_downloader(
        records,
        output=resolved_output,
        max_concurrent=args.max_concurrent,
        services=services,
        skip_existing=not args.no_skip,
        auto_deduplicate=not args.no_dedup,
        auto_organize=not args.no_flatten,
    )

    m3u_entries: int | None = None
    m3u_path: Path | None = None
    if args.m3u:
        m3u_fn = _pick(write_m3u8, _core.write_m3u8, _ORIG_WRITE_M3U8)
        candidate = Path(args.m3u).expanduser()
        m3u_path = candidate if candidate.is_absolute() else (resolved_output / candidate)
        track_dicts = [
            {
                "status": item.status,
                "file_path": str(item.file_path) if item.file_path else "",
                "title": item.title or (item.file_path.stem if item.file_path else ""),
                "artists": item.artist or "",
            }
            for item in summary.tracks
        ]
        m3u_entries = m3u_fn(track_dicts, m3u_path, resolved_output)

    if args.json:
        payload = _summary_to_dict(summary)
        if m3u_path is not None:
            payload["m3u"] = str(m3u_path)
            payload["m3u_entries"] = m3u_entries
        print(json.dumps(payload, indent=2))
    else:
        _print_download_summary(summary)
        if m3u_path is not None:
            print(f"Playlist: {m3u_path} ({m3u_entries} tracks)")
    return 0


def _handle_organize_subcommand(args: argparse.Namespace) -> int:
    """Execute `songetter organize` / `songetter dedup`."""
    validate_fn = _pick(
        validate_library_directory,
        _library.validate_library_directory,
        _ORIG_VALIDATE_LIBRARY_DIRECTORY,
    )
    dedup_fn = _pick(
        resolve_duplicates,
        _deduplicator.resolve_duplicates,
        _ORIG_RESOLVE_DUPLICATES,
    )
    flatten_fn = _pick(
        flatten_and_organize_library,
        _organizer.flatten_and_organize_library,
        _ORIG_FLATTEN_AND_ORGANIZE_LIBRARY,
    )

    target = validate_fn(Path(args.output).expanduser(), create=False)
    dups = [] if args.flatten_only else dedup_fn(target)
    organized = [] if args.dedup_only else flatten_fn(target)

    if args.json:
        payload = {
            "library_dir": str(target),
            "duplicates_resolved": len(dups),
            "deleted_files": [str(p) for group in dups for p in group.deleted],
            "organized_files": [str(p) for p in organized],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Library: {target}")
        if not args.flatten_only:
            deleted_count = sum(len(group.deleted) for group in dups)
            print(f"Deduplication: resolved {len(dups)} group(s), removed {deleted_count} duplicate(s)")
        if not args.dedup_only:
            print(f"Organization: {len(organized)} track(s) at library root")
    return 0


def _handle_links(
    args: argparse.Namespace,
    services: list[str] | None,
) -> int:
    """Process positional and -i/--input music links (legacy mode)."""
    if args.input and Path(args.input).expanduser().suffix.lower() == ".csv" and not args.links:
        return _handle_csv(
            args.input,
            args.output,
            args.max_concurrent,
            services,
            as_json=args.json,
        )

    chunks = list(args.links)
    if args.input:
        input_path = Path(args.input).expanduser()
        chunks.append(input_path.read_text(encoding="utf-8"))
    raw = "\n".join(chunks)

    tokens = [tok for tok in re.split(r"[\s,;]+", raw.strip()) if tok]
    if tokens and all(_is_direct_http_audio_url(tok) for tok in tokens):
        records = [TrackRecord(url=tok) for tok in dict.fromkeys(tokens)]
        resolved_output = Path(args.output).expanduser().resolve()
        summary = _run_downloader(
            records,
            output=resolved_output,
            max_concurrent=args.max_concurrent,
            services=services,
        )
        _print_download_summary(summary)
        return 0

    parse_fn = _pick(parse_source_links, _core.parse_source_links, _ORIG_PARSE_SOURCE_LINKS)
    links = parse_fn(raw)

    output = Path(args.output).expanduser().resolve()
    active_services = services if services is not None else list(DEFAULT_SERVICES)
    if not active_services:
        raise ValueError("Select at least one Mornye provider.")

    config_fn = _pick(
        configure_mornye_registry,
        _core.configure_mornye_registry,
        _ORIG_CONFIGURE_MORNYE_REGISTRY,
    )
    run_fn = _pick(run_download, _core.run_download, _ORIG_RUN_DOWNLOAD)
    m3u_fn = _pick(write_m3u8, _core.write_m3u8, _ORIG_WRITE_M3U8)

    config_fn()
    all_tracks: list[dict] = []
    totals = {"downloaded": 0, "skipped": 0, "failed": 0}

    for index, url in enumerate(links, 1):
        print(f"[{index}/{len(links)}] Processing {url}")
        result = run_fn(
            url,
            output,
            active_services,
            max_concurrent_downloads=args.max_concurrent,
        )
        all_tracks.extend(result.tracks)
        for key in totals:
            totals[key] += getattr(result, key)
        if result.error:
            print(f"  ERROR: {result.error}")
        print(f"  downloaded={result.downloaded} existing={result.skipped} failed={result.failed}")

    playlist = output / args.m3u
    entries = m3u_fn(all_tracks, playlist, output)
    print(f"\nCompleted: {totals}")
    print(f"Providers: {', '.join(active_services)}")
    print(f"Concurrent track downloads: {args.max_concurrent}")
    print(f"Playlist: {playlist} ({entries} tracks)")
    return 0


def _build_subcommand_parser() -> argparse.ArgumentParser:
    """Build parser for explicit CLI subcommands."""
    parser = argparse.ArgumentParser(prog="songetter", description="Songetter unified CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    tui_parser = subparsers.add_parser("tui", help="Launch the interactive Textual TUI")
    tui_parser.add_argument("-o", "--output", default=None, help="Initial music library folder")

    for name in ("serve", "web"):
        web_parser = subparsers.add_parser(name, help="Start the FastAPI web UI and API server")
        web_parser.add_argument("--host", default="0.0.0.0", help="Server bind host (default: 0.0.0.0)")
        web_parser.add_argument("--port", type=int, default=8000, help="Server bind port (default: 8000)")
        web_parser.add_argument("-o", "--output", default=None, help="Music library directory (sets SONGETTER_LIBRARY_DIR)")
        web_parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload")

    dl_parser = subparsers.add_parser("download", help="Download tracks from URLs or a CSV file")
    dl_parser.add_argument("links", nargs="*", help="Track, album, playlist, or direct audio URLs")
    dl_parser.add_argument("-i", "--input", default=None, help="CSV or text file of URLs")
    dl_parser.add_argument("-o", "--output", default="Music", help="Music library destination folder")
    dl_parser.add_argument("--max-concurrent", type=int, default=2, help="Concurrent downloads (default: 2)")
    dl_parser.add_argument("--service", action="append", default=None, help="Mornye extension ID (repeatable)")
    dl_parser.add_argument("--no-skip", action="store_true", help="Disable pre-download skip check")
    dl_parser.add_argument("--no-dedup", action="store_true", help="Skip post-download deduplication")
    dl_parser.add_argument("--no-flatten", action="store_true", help="Skip post-download flat organization")
    dl_parser.add_argument("--m3u", default=None, help="Optional M3U8 playlist filename")
    dl_parser.add_argument("--json", action="store_true", help="Output JSON summary")

    for name in ("organize", "dedup"):
        org_parser = subparsers.add_parser(
            name,
            help="Deduplicate tracks by quality and flatten filenames to {Artist} - {Title}.{ext}",
        )
        org_parser.add_argument("-o", "--output", default="Music", help="Music library folder")
        org_parser.add_argument("--dedup-only", action="store_true", help="Only remove inferior duplicates")
        org_parser.add_argument("--flatten-only", action="store_true", help="Only flatten and standardize names")
        org_parser.add_argument("--json", action="store_true", help="Output JSON summary")

    search_parser = subparsers.add_parser("search", help="Federated multi-source track search")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Search source ID (repeatable or comma-separated; accepts 'all'; default: deezer)",
    )
    search_parser.add_argument("--type", dest="search_type", choices=["track", "album", "artist"], default="track")
    search_parser.add_argument("--limit", type=int, default=10, help="Results per source (default: 10)")
    search_parser.add_argument("--deduplicate", action="store_true", help="Deduplicate search results")
    search_parser.add_argument("--export-csv", default=None, help="Write search results to a CSV file")
    search_parser.add_argument("--download", action="store_true", help="Download search results")
    search_parser.add_argument("-o", "--output", default="Music", help="Destination folder when --download is set")
    search_parser.add_argument("--max-concurrent", type=int, default=2, help="Concurrent downloads (default: 2)")
    search_parser.add_argument("--service", action="append", default=None, help="Mornye extension ID")
    search_parser.add_argument("--json", action="store_true", help="Output JSON results")

    doc_parser = subparsers.add_parser("doctor", help="Check binaries, packages, and library writability")
    doc_parser.add_argument("-o", "--output", default=None, help="Library folder to validate")
    doc_parser.add_argument("--json", action="store_true", help="Output JSON diagnostics")

    return parser


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI argument parser (backward-compatible flags + subcommand docs)."""
    parser = argparse.ArgumentParser(
        prog="songetter",
        description=(
            "Songetter unified CLI: launch the Textual TUI, serve the FastAPI web UI, "
            "download/ingest URLs or CSVs, search catalogs, organize libraries, or run doctor."
        ),
        epilog=(
            "Subcommands:\n"
            "  songetter tui [-o DIR]\n"
            "  songetter serve|web [--host 0.0.0.0] [--port 8000] [-o DIR] [--reload]\n"
            "  songetter download [links...] [-i FILE] [-o DIR] [--max-concurrent N] [--m3u FILE]\n"
            "  songetter organize|dedup [-o DIR] [--dedup-only] [--flatten-only]\n"
            "  songetter search <query> [--source SRC] [--type track|album|artist] [--limit N] [--export-csv FILE] [--download]\n"
            "  songetter doctor [-o DIR] [--json]\n\n"
            "Legacy flags:\n"
            "  songetter https://open.spotify.com/track/abc123\n"
            "  songetter -i links.txt -o ./Music --max-concurrent 4\n"
            "  songetter --list-sources\n"
            "  songetter --search 'Daft Punk' --search-source deezer,itunes\n"
            "  songetter --csv tracks.csv -o ./Music\n"
            "  songetter --serve --host 0.0.0.0 --port 8000"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "links",
        nargs="*",
        help="Music links (Spotify, Deezer, Tidal, Qobuz, Apple Music, SoundCloud, YouTube Music, JioSaavn) or subcommand",
    )
    parser.add_argument("-i", "--input", help="Text or CSV file containing links/tracks")
    parser.add_argument("-o", "--output", default="Music", help="Music library destination folder")
    parser.add_argument(
        "--service",
        action="append",
        default=None,
        help="Mornye extension ID; replaces the default list (repeatable)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=2,
        help="Number of tracks downloaded simultaneously (default: 2)",
    )
    parser.add_argument("--m3u", default="Songetter.m3u8", help="M3U8 playlist filename")

    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the FastAPI web application with uvicorn (same as `songetter serve`)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Server bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Server bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload")

    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="List registered music search sources and configuration status",
    )
    parser.add_argument(
        "--search",
        default=None,
        metavar="QUERY",
        help="Search tracks, albums, or artists across configured search sources",
    )
    parser.add_argument(
        "--search-source",
        action="append",
        default=None,
        help="Search source ID (repeatable or comma-separated; accepts 'all'; default: deezer)",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=10,
        help="Maximum search results per source (default: 10)",
    )
    parser.add_argument(
        "--search-type",
        choices=["track", "album", "artist"],
        default="track",
        help="Search entity type (default: track)",
    )
    parser.add_argument(
        "--deduplicate-search",
        action="store_true",
        help="Deduplicate federated search results by ISRC or normalized artist/title",
    )
    parser.add_argument(
        "--download-search",
        action="store_true",
        help="Download results returned by --search into the destination library",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="Ingest a CSV track file and download/deduplicate/organize into --output",
    )
    return parser


def _dispatch_subcommand(argv_list: list[str]) -> int:
    parser = _build_subcommand_parser()
    args = parser.parse_args(argv_list)
    cmd = args.subcommand

    try:
        if cmd == "tui":
            return _handle_tui(args.output)
        if cmd in {"serve", "web"}:
            if not (1 <= args.port <= 65535):
                parser.error("--port must be between 1 and 65535")
            return _handle_serve(args.host, args.port, output=args.output, reload=args.reload)
        if cmd == "doctor":
            return _handle_doctor(args.output, as_json=args.json)
        if cmd == "organize" or cmd == "dedup":
            if args.dedup_only and args.flatten_only:
                parser.error("Cannot combine --dedup-only and --flatten-only.")
            return _handle_organize_subcommand(args)
        if cmd == "download":
            if args.max_concurrent < 1:
                parser.error("--max-concurrent must be at least 1")
            return _handle_download_subcommand(args, _parse_services(args.service))
        if cmd == "search":
            if args.limit < 1:
                parser.error("--limit must be at least 1")
            if args.max_concurrent < 1:
                parser.error("--max-concurrent must be at least 1")
            return _handle_search_common(
                query=args.query,
                raw_sources=args.source,
                limit=args.limit,
                search_type=args.search_type,
                deduplicate=args.deduplicate,
                download=args.download,
                export_csv=args.export_csv,
                output=args.output,
                max_concurrent=args.max_concurrent,
                services=_parse_services(args.service),
                as_json=args.json,
            )
    except (ValueError, SearchError, CSVProcessorError, LibraryError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown subcommand: {cmd}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``songetter`` CLI."""
    argv_list = list(sys.argv[1:] if argv is None else argv)

    if not argv_list:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return _handle_tui(None)
        parser = build_parser()
        parser.print_help()
        parser.exit(2)

    if argv_list[0] in SUBCOMMANDS:
        return _dispatch_subcommand(argv_list)

    parser = build_parser()
    args = parser.parse_args(argv_list)

    if args.max_concurrent < 1:
        parser.error("--max-concurrent must be at least 1")
    if args.search_limit < 1:
        parser.error("--search-limit must be at least 1")
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")

    is_serve = bool(args.serve)
    is_list_sources = bool(args.list_sources)
    is_search = args.search is not None
    is_csv = args.csv is not None
    is_links = bool(args.links) or bool(args.input)

    if args.serve and args.links:
        parser.error("--serve cannot be combined with positional link arguments.")

    active_modes = sum([is_serve, is_list_sources, is_search, is_csv, is_links])
    if active_modes == 0:
        parser.error(
            "Provide music links, -i/--input, --csv, --search, --list-sources, or a subcommand."
        )
    if active_modes > 1:
        parser.error(
            "Specify only one mode at a time: links/-i, --csv, --search, --list-sources, or --serve."
        )

    if args.download_search and not is_search:
        parser.error("--download-search requires --search <QUERY>.")

    services = _parse_services(args.service)

    try:
        if is_serve:
            return _handle_serve(args.host, args.port, reload=args.reload)
        if is_list_sources:
            return _handle_list_sources(as_json=args.json)
        if is_search:
            return _handle_search_common(
                query=args.search,
                raw_sources=args.search_source,
                limit=args.search_limit,
                search_type=args.search_type,
                deduplicate=args.deduplicate_search,
                download=args.download_search,
                export_csv=None,
                output=args.output,
                max_concurrent=args.max_concurrent,
                services=services,
                as_json=args.json,
            )
        if is_csv:
            return _handle_csv(
                args.csv,
                args.output,
                args.max_concurrent,
                services,
                as_json=args.json,
            )
        return _handle_links(args, services)
    except (ValueError, SearchError, CSVProcessorError, LibraryError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main_tui(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``songetter-tui``."""
    extra = list(sys.argv[1:] if argv is None else argv)
    return main(["tui", *extra])


def main_web(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``songetter-web``."""
    extra = list(sys.argv[1:] if argv is None else argv)
    return main(["serve", *extra])


if __name__ == "__main__":
    raise SystemExit(main())
