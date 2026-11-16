"""Command-line interface for Songetter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import core as _core
from . import csv_processor as _csv_processor
from . import downloader as _downloader
from . import search as _search
from .core import DEFAULT_SERVICES
from .csv_processor import CSVProcessorError
from .library import LibraryError
from .search import SearchError

_ORIG_CONFIGURE_MORNYE_REGISTRY = _core.configure_mornye_registry
_ORIG_PARSE_SOURCE_LINKS = _core.parse_source_links
_ORIG_RUN_DOWNLOAD = _core.run_download
_ORIG_WRITE_M3U8 = _core.write_m3u8
_ORIG_PARSE_CSV = _csv_processor.parse_csv
_ORIG_DOWNLOADER = _downloader.Downloader
_ORIG_LIST_SEARCH_SOURCES = _search.list_search_sources
_ORIG_SEARCH_TRACKS_WITH_ERRORS = _search.search_tracks_with_errors

configure_mornye_registry = _ORIG_CONFIGURE_MORNYE_REGISTRY
parse_source_links = _ORIG_PARSE_SOURCE_LINKS
run_download = _ORIG_RUN_DOWNLOAD
write_m3u8 = _ORIG_WRITE_M3U8
parse_csv = _ORIG_PARSE_CSV
Downloader = _ORIG_DOWNLOADER
list_search_sources = _ORIG_LIST_SEARCH_SOURCES
search_tracks_with_errors = _ORIG_SEARCH_TRACKS_WITH_ERRORS


def _pick(cli_obj: Any, module_obj: Any, orig_obj: Any) -> Any:
    """Resolve patched symbols regardless of whether tests patch cli or source module."""
    return cli_obj if cli_obj is not orig_obj else module_obj


def _parse_search_sources(raw_sources: list[str] | None) -> list[str]:
    """Normalize repeatable/comma-separated --search-source flags."""
    if not raw_sources:
        return ["deezer"]
    tokens: list[str] = []
    for entry in raw_sources:
        for part in entry.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
    return tokens or ["deezer"]


def _parse_services(raw_services: list[str] | None) -> list[str] | None:
    """Normalize repeatable or comma-separated --service flags."""
    if raw_services is None:
        return None
    tokens: list[str] = []
    for entry in raw_services:
        for part in entry.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
    return tokens


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
) -> Any:
    """Invoke Downloader while remaining compatible with minimal test doubles."""
    downloader_cls = _pick(Downloader, _downloader.Downloader, _ORIG_DOWNLOADER)
    if services is not None:
        instance = downloader_cls(output, max_concurrent=max_concurrent, services=services)
    else:
        instance = downloader_cls(output, max_concurrent=max_concurrent)
    return instance.download(records)


def _handle_serve(host: str, port: int) -> int:
    """Start the FastAPI web application via uvicorn."""
    import uvicorn

    uvicorn.run(
        "songetter.web.app:create_app",
        factory=True,
        host=host,
        port=port,
    )
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


def _handle_search(
    args: argparse.Namespace,
    services: list[str] | None,
) -> int:
    """Run multi-source search and optionally download matched results."""
    sources = _parse_search_sources(args.search_source)
    search_fn = _pick(
        search_tracks_with_errors,
        _search.search_tracks_with_errors,
        _ORIG_SEARCH_TRACKS_WITH_ERRORS,
    )
    results, errors = search_fn(
        args.search,
        sources=sources,
        limit=args.search_limit,
        search_type=args.search_type,
        deduplicate=args.deduplicate_search,
    )

    download_summary = None
    if args.download_search:
        if results:
            output = Path(args.output).expanduser().resolve()
            records = [item.to_track_record() for item in results]
            download_summary = _run_downloader(
                records,
                output=output,
                max_concurrent=args.max_concurrent,
                services=services,
            )

    if args.json:
        payload: dict[str, Any] = {
            "query": args.search,
            "sources": sources,
            "search_type": args.search_type,
            "count": len(results),
            "results": [item.to_dict() for item in results],
            "errors": errors,
        }
        if args.download_search:
            payload["download"] = (
                _summary_to_dict(download_summary)
                if download_summary is not None
                else {"downloaded": 0, "skipped": 0, "failed": 0, "tracks": []}
            )
        print(json.dumps(payload, indent=2))
        return 0

    if not results:
        print(f"No results found for '{args.search}'.")
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

    if args.download_search:
        if download_summary is None:
            print("No search results to download.")
        else:
            _print_download_summary(download_summary)

    return 0


def _handle_csv(
    args: argparse.Namespace,
    services: list[str] | None,
) -> int:
    """Parse a CSV track list and execute Downloader."""
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if csv_path.is_dir():
        raise IsADirectoryError(f"Expected CSV file but found directory: {csv_path}")

    parse_csv_fn = _pick(parse_csv, _csv_processor.parse_csv, _ORIG_PARSE_CSV)
    records = parse_csv_fn(csv_path.read_bytes(), filename=csv_path.name)
    output = Path(args.output).expanduser().resolve()
    summary = _run_downloader(
        records,
        output=output,
        max_concurrent=args.max_concurrent,
        services=services,
    )

    if args.json:
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


def _handle_links(
    args: argparse.Namespace,
    services: list[str] | None,
) -> int:
    """Process positional and -i/--input music links through Mornye."""
    chunks = list(args.links)
    if args.input:
        input_path = Path(args.input).expanduser()
        chunks.append(input_path.read_text(encoding="utf-8"))
    raw = "\n".join(chunks)

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


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for Songetter."""
    parser = argparse.ArgumentParser(
        prog="songetter",
        description=(
            "Batch-process music links, search catalogs across multiple providers, "
            "ingest CSV libraries, or serve the FastAPI web UI."
        ),
        epilog=(
            "Examples:\n"
            "  songetter https://open.spotify.com/track/abc123\n"
            "  songetter -i links.txt -o ./Music --max-concurrent 4\n"
            "  songetter --list-sources\n"
            "  songetter --search 'Daft Punk' --search-source deezer,itunes\n"
            "  songetter --csv tracks.csv -o ./Music\n"
            "  songetter serve --host 0.0.0.0 --port 8000\n"
            "  songetter --serve --host 0.0.0.0 --port 8000"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "links",
        nargs="*",
        help="Music links (Spotify, Deezer, Tidal, Qobuz, Apple Music, SoundCloud, YouTube Music, JioSaavn) or 'serve'",
    )
    parser.add_argument("-i", "--input", help="Text file containing one link per line")
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

    # Web server mode
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the FastAPI web application with uvicorn (same as `songetter serve`)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Server bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Server bind port (default: 8000)")

    # Search & CSV modes
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
        help="Emit machine-readable JSON output for --list-sources, --search, or --csv",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="Ingest a CSV track file and download/deduplicate/organize into --output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``songetter`` CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_concurrent < 1:
        parser.error("--max-concurrent must be at least 1")
    if args.search_limit < 1:
        parser.error("--search-limit must be at least 1")
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")

    is_serve_subcommand = bool(args.links) and args.links[0] == "serve"
    is_serve = bool(args.serve) or is_serve_subcommand
    is_list_sources = bool(args.list_sources)
    is_search = args.search is not None
    is_csv = args.csv is not None
    is_links = (not is_serve_subcommand and bool(args.links)) or bool(args.input)

    if is_serve_subcommand and len(args.links) > 1:
        parser.error("Unexpected positional arguments after 'serve'.")
    if args.serve and args.links and not is_serve_subcommand:
        parser.error("--serve cannot be combined with positional link arguments.")

    active_modes = sum([is_serve, is_list_sources, is_search, is_csv, is_links])
    if active_modes == 0:
        parser.error(
            "Provide music links, -i/--input, --csv, --search, --list-sources, or serve."
        )
    if active_modes > 1:
        parser.error(
            "Specify only one mode at a time: links/-i, --csv, --search, --list-sources, or serve."
        )

    if args.download_search and not is_search:
        parser.error("--download-search requires --search <QUERY>.")

    services = _parse_services(args.service)

    try:
        if is_serve:
            return _handle_serve(args.host, args.port)
        if is_list_sources:
            return _handle_list_sources(as_json=args.json)
        if is_search:
            return _handle_search(args, services)
        if is_csv:
            return _handle_csv(args, services)
        return _handle_links(args, services)
    except (ValueError, SearchError, CSVProcessorError, LibraryError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
