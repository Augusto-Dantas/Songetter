"""Streamlit multi-tab web UI for Songetter."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
from typing import Any

import streamlit as st

_STRETCH: dict[str, Any] = (
    {"width": "stretch"}
    if "width" in inspect.signature(st.button).parameters
    else {"use_container_width": True}
)

from songetter.config import AUDIO_SUFFIXES, SUPPORTED_SEARCH_SOURCES
from songetter.core import (
    DEFAULT_SERVICES,
    MORNYE_REGISTRY,
    configure_mornye_registry,
    parse_source_links,
    run_download,
    write_m3u8,
)
from songetter.csv_processor import CSVProcessorError, parse_csv
from songetter.deduplicator import extract_metadata, normalize_text, resolve_duplicates
from songetter.downloader import DownloadSummary, Downloader
from songetter.library import LibraryError, validate_library_directory
from songetter.organizer import flatten_and_organize_library
from songetter.search import (
    SearchResult,
    list_search_sources,
    search_results_to_csv,
    search_tracks_with_errors,
)


def format_bytes_per_second(value: float) -> str:
    """Format byte throughput in human-readable units."""
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    amount = max(0.0, float(value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GB/s"


def format_duration(seconds: float | None) -> str:
    """Format elapsed or remaining seconds for live metrics."""
    if seconds is None:
        return "Calculating..."
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_track_duration(seconds: float | int | None) -> str:
    """Format track length as M:SS or H:MM:SS for tables."""
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _parse_comma_list(raw: str) -> list[str]:
    """Split comma- or newline-separated provider identifiers."""
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _refresh_mornye_track_paths(tracks: list[dict[str, Any]], library_dir: Path) -> None:
    """Update track file_path entries to match flattened library filenames."""
    by_key: dict[tuple[str, str], Path] = {}
    for path in sorted(library_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
            meta = extract_metadata(path)
            by_key.setdefault((normalize_text(meta.artist), normalize_text(meta.title)), path)

    for track in tracks:
        existing = track.get("file_path")
        if existing and Path(existing).is_file():
            continue
        artist = str(track.get("artists") or track.get("artist") or "")
        title = str(track.get("title") or "")
        key = (normalize_text(artist), normalize_text(title))
        if key in by_key:
            track["file_path"] = str(by_key[key])


def _scan_library_rows(library_dir: Path) -> list[dict[str, Any]]:
    """Collect metadata rows for all audio files inside library_dir."""
    rows: list[dict[str, Any]] = []
    audio_paths = sorted(
        p
        for p in library_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    )
    for path in audio_paths:
        meta = extract_metadata(path)
        try:
            rel_name = str(path.relative_to(library_dir))
        except ValueError:
            rel_name = path.name
        rows.append(
            {
                "File": rel_name,
                "Artist": meta.artist,
                "Title": meta.title,
                "Format": path.suffix.lstrip(".").upper(),
                "Bitrate (kbps)": round(meta.bitrate / 1000) if meta.bitrate else 0,
                "Size (KB)": round(meta.file_size / 1024, 1),
                "Duration": _format_track_duration(meta.duration),
            }
        )
    return rows


def _render_download_summary(summary: DownloadSummary, heading: str) -> None:
    """Render standardized Downloader summary metrics, deduplication, and track table."""
    st.success(heading)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Downloaded", summary.downloaded)
    c2.metric("Skipped", summary.skipped)
    c3.metric("Failed", summary.failed)
    c4.metric("Deduplicated", len(summary.duplicates_resolved))
    c5.metric("Organized Files", len(summary.organized_files))

    if summary.duplicates_resolved:
        with st.expander(f"Deduplication decisions ({len(summary.duplicates_resolved)})"):
            for dup in summary.duplicates_resolved:
                deleted_names = ", ".join(p.name for p in dup.deleted)
                st.write(
                    f"**{dup.artist} - {dup.title}**: kept `{dup.retained.name}`, "
                    f"removed `{deleted_names}` ({dup.reason})"
                )

    if summary.tracks:
        st.dataframe(
            [
                {
                    "Status": track.status,
                    "Artist": track.artist or "",
                    "Title": track.title or "",
                    "File": str(track.file_path.name) if track.file_path else "",
                    "URL": track.url,
                    "Error": track.error or "",
                }
                for track in summary.tracks
            ],
            **_STRETCH,
        )


def _render_links_tab(
    output_value: str,
    services_value: str,
    lyrics_value: str,
    concurrency_value: int,
) -> None:
    """Render Tab 1: Multi-source URL batch download via Mornye."""
    st.subheader("Music links")
    raw_links = st.text_area(
        "Paste track, album, playlist, or artist links (one per line)",
        height=200,
        placeholder=(
            "https://open.spotify.com/playlist/...\n"
            "https://www.deezer.com/album/...\n"
            "https://tidal.com/browse/track/..."
        ),
        key="links_input",
    )
    post_organize = st.checkbox(
        "Flatten & deduplicate library after download",
        value=False,
        help="Retain the highest-quality copy of duplicate tracks and organize files as {Artist} - {Title}.{ext}.",
        key="links_post_organize",
    )
    start = st.button("Start processing", type="primary", **_STRETCH, key="links_start_btn")

    if not start:
        return

    try:
        links = parse_source_links(raw_links)
        configure_mornye_registry()
        output = validate_library_directory(output_value, create=True)
        services = _parse_comma_list(services_value)
        lyrics = _parse_comma_list(lyrics_value)
        if not services:
            raise ValueError("Select at least one Mornye provider.")

        all_tracks: list[dict[str, Any]] = []
        totals = {"downloaded": 0, "skipped": 0, "failed": 0}
        progress = st.progress(0, text="Starting Mornye...")
        status = st.empty()
        log_panel = st.empty()
        speed_metric, eta_metric, elapsed_metric = st.columns(3)
        speed_value = speed_metric.empty()
        eta_value = eta_metric.empty()
        elapsed_value = elapsed_metric.empty()
        progress_lines: list[str] = []

        def show_progress(message: str) -> None:
            progress_lines.append(message)
            del progress_lines[:-12]
            log_panel.code("\n".join(progress_lines), language="text")
            match = re.search(r"Track\s+\[(\d+)/(\d+)\]", message)
            if match:
                current_track, current_total = map(int, match.groups())
                if current_total:
                    progress.progress(
                        min(current_track / current_total, 1.0),
                        text=f"Processing track {current_track} of {current_total}",
                    )
            elif message.startswith("[RUN]"):
                progress.progress(0, text="Mornye is resolving the link...")
            if "verification paused" in message.lower() or "next try in" in message.lower():
                status.warning(
                    "A provider cooldown was detected. Songetter is stopping that provider and moving to the next one."
                )

        def show_metrics(metrics: dict[str, Any]) -> None:
            speed_value.metric("Download speed", format_bytes_per_second(metrics.get("speed_bps", 0)))
            eta_value.metric("Estimated time remaining", format_duration(metrics.get("eta_seconds")))
            elapsed_value.metric("Elapsed", format_duration(metrics.get("elapsed_seconds", 0)))

        for index, url in enumerate(links, 1):
            status.info(f"Processing link {index} of {len(links)}: {url}")
            try:
                result = run_download(
                    url,
                    output,
                    services,
                    lyrics,
                    max_concurrent_downloads=concurrency_value,
                    progress_callback=show_progress,
                    metrics_callback=show_metrics,
                )
            except Exception as exc:
                status.error(f"Could not process {url}: {type(exc).__name__}: {exc}")
                totals["failed"] += 1
                continue

            all_tracks.extend(result.tracks)
            for key in totals:
                totals[key] += getattr(result, key)

            for provider_result in result.provider_status:
                provider_name = provider_result.get("provider", "Unknown provider")
                if provider_result.get("status") == "cooldown":
                    remaining = int(provider_result.get("retry_after_seconds", 0) or 0)
                    minutes, seconds = divmod(max(0, remaining), 60)
                    st.warning(
                        f"{provider_name} is temporarily paused ({minutes}m {seconds}s remaining). "
                        "It has been skipped for this run and its cooldown is saved for future runs."
                    )
                elif provider_result.get("status") == "completed" and provider_result.get("failed", 0):
                    st.caption(
                        f"{provider_name}: {provider_result.get('failed')} track(s) still need another provider."
                    )

            if result.error:
                st.error(f"{url}: {result.error}")
            for track in result.tracks:
                if track.get("status") == "failed" or track.get("error"):
                    title = track.get("title") or "Unknown track"
                    detail = (
                        track.get("error")
                        or track.get("message")
                        or "The provider did not return a failure reason."
                    )
                    st.warning(f"{title}: {detail}")

            if index == len(links):
                progress.progress(1.0, text=f"Completed {index} of {len(links)} links")

        if post_organize:
            dedup_results = resolve_duplicates(output)
            organized = flatten_and_organize_library(output)
            dedup_results.extend(resolve_duplicates(output))
            _refresh_mornye_track_paths(all_tracks, output)
            st.info(
                f"Library organized: {len(organized)} flat audio file(s), "
                f"{len(dedup_results)} duplicate group(s) resolved."
            )

        playlist = output / "Songetter.m3u8"
        count = write_m3u8(all_tracks, playlist, output)
        status.success(
            f"Processing complete. Processed {len(links)} link(s) with up to "
            f"{concurrency_value} tracks downloading simultaneously."
        )
        col1, col2, col3 = st.columns(3)
        col1.metric("Downloaded", totals["downloaded"])
        col2.metric("Already existing", totals["skipped"])
        col3.metric("Failed", totals["failed"])
        st.write(f"Playlist created: `{playlist}` — {count} track(s).")
        if all_tracks:
            st.dataframe(
                [
                    {
                        "Track": track.get("title"),
                        "Artist": track.get("artists"),
                        "Album": track.get("album"),
                        "Status": track.get("status"),
                        "File": track.get("file_path"),
                        "Error": track.get("error") or track.get("message"),
                    }
                    for track in all_tracks
                ],
                **_STRETCH,
            )
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")


def _render_search_tab(
    output_value: str,
    services_value: str,
    concurrency_value: int,
) -> None:
    """Render Tab 2: Multi-source catalog search, audio previews, CSV export, and download."""
    st.subheader("Multi-source catalog search")

    sources_meta = list_search_sources()
    with st.expander("Search source status", expanded=False):
        st.dataframe(
            [
                {
                    "ID": src.get("id"),
                    "Name": src.get("name"),
                    "Auth Required": bool(src.get("requires_auth")),
                    "Configured": bool(src.get("configured")),
                    "Description": src.get("description", ""),
                }
                for src in sources_meta
            ],
            **_STRETCH,
        )

    query = st.text_input(
        "Search query",
        placeholder="Artist, track title, or album...",
        key="search_query_input",
    )
    col_src, col_opts = st.columns(2)
    with col_src:
        all_sources = st.checkbox(
            "All configured sources",
            value=False,
            key="search_all_sources",
        )
        selected_sources = st.multiselect(
            "Search sources",
            options=list(SUPPORTED_SEARCH_SOURCES),
            default=["deezer"],
            disabled=all_sources,
            key="search_sources_multiselect",
        )
    with col_opts:
        search_type = st.selectbox(
            "Search type",
            options=["track", "album", "artist"],
            index=0,
            key="search_type_select",
        )
        search_limit = st.slider(
            "Results per source",
            min_value=1,
            max_value=50,
            value=10,
            key="search_limit_slider",
        )

    deduplicate_search = st.checkbox(
        "Deduplicate results across sources",
        value=True,
        key="search_deduplicate",
    )

    if st.button("Search catalogs", type="primary", **_STRETCH, key="search_btn"):
        if not query.strip():
            st.warning("Enter a search query.")
        else:
            sources_arg: str | list[str] = "all" if all_sources else (selected_sources or ["deezer"])
            try:
                with st.spinner("Searching catalogs..."):
                    results, errors = search_tracks_with_errors(
                        query.strip(),
                        sources=sources_arg,
                        limit=search_limit,
                        search_type=search_type,
                        deduplicate=deduplicate_search,
                    )
                st.session_state["search_results"] = results
                st.session_state["search_errors"] = errors
                st.session_state.pop("search_download_summary", None)
            except Exception as exc:
                st.error(f"{type(exc).__name__}: {exc}")

    search_errors: dict[str, str] = st.session_state.get("search_errors") or {}
    for src, err in search_errors.items():
        st.warning(f"{src}: {err}")

    results: list[SearchResult] = st.session_state.get("search_results") or []
    if not results:
        if "search_results" in st.session_state:
            st.info("No matching results returned.")
        return

    st.caption(f"Showing {len(results)} result(s)")
    table_rows = [
        {
            "Source": item.source,
            "Artist": item.artist,
            "Title": item.title,
            "Album": item.album or "",
            "Duration": _format_track_duration(item.duration_seconds),
            "URL": item.url,
        }
        for item in results
    ]
    url_column = getattr(st.column_config, "LinkColumn", None)
    column_config = {"URL": url_column("URL")} if url_column else None
    st.dataframe(table_rows, **_STRETCH, column_config=column_config)

    preview_items = [item for item in results if item.preview_url]
    if preview_items:
        with st.expander(f"Audio previews ({len(preview_items)})", expanded=False):
            for item in preview_items:
                st.caption(f"[{item.source}] {item.artist} - {item.title}")
                st.audio(item.preview_url)

    selected_indices = st.multiselect(
        "Select specific results to download",
        options=list(range(len(results))),
        format_func=lambda idx: f"[{results[idx].source}] {results[idx].artist} - {results[idx].title}",
        key="search_selected_indices",
    )
    use_preview_fallback = st.checkbox(
        "Fallback to preview URL when direct URL is unavailable",
        value=False,
        key="search_preview_fallback",
    )

    col_csv, col_sel, col_all = st.columns(3)
    with col_csv:
        st.download_button(
            "Download CSV",
            data=search_results_to_csv(results),
            file_name="songetter_search.csv",
            mime="text/csv",
            **_STRETCH,
            key="search_export_csv_btn",
        )
    with col_sel:
        download_selected = st.button(
            "Download Selected to Library",
            disabled=not selected_indices,
            **_STRETCH,
            key="search_download_selected_btn",
        )
    with col_all:
        download_all = st.button(
            "Download All Results to Library",
            type="primary",
            **_STRETCH,
            key="search_download_all_btn",
        )

    if download_selected or download_all:
        chosen = [results[idx] for idx in selected_indices] if download_selected else results
        records = [item.to_track_record(use_preview_if_no_url=use_preview_fallback) for item in chosen]
        try:
            lib_dir = validate_library_directory(output_value, create=True)
            services = _parse_comma_list(services_value) or None
            with st.spinner(f"Downloading {len(records)} track(s) to {lib_dir}..."):
                summary = Downloader(
                    lib_dir,
                    max_concurrent=concurrency_value,
                    services=services,
                ).download(records)
            st.session_state["search_download_summary"] = summary
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")

    search_summary: DownloadSummary | None = st.session_state.get("search_download_summary")
    if search_summary is not None:
        _render_download_summary(search_summary, "Search results processed.")


def _render_csv_tab(
    output_value: str,
    services_value: str,
    concurrency_value: int,
) -> None:
    """Render Tab 3: Server-side library validation, CSV ingestion, and flat browser."""
    st.subheader("CSV library ingestion")

    if st.button("Validate library folder", **_STRETCH, key="csv_validate_btn"):
        try:
            resolved = validate_library_directory(output_value, create=True)
            st.success(f"Writable library directory ready: `{resolved}`")
        except (LibraryError, OSError) as exc:
            st.error(f"{type(exc).__name__}: {exc}")

    uploaded_csv = st.file_uploader(
        "Upload CSV track list",
        type=["csv", "txt"],
        key="csv_file_uploader",
    )
    if uploaded_csv is not None:
        try:
            records = parse_csv(uploaded_csv.getvalue(), filename=uploaded_csv.name)
            st.success(f"Parsed {len(records)} valid track record(s) from `{uploaded_csv.name}`.")
            st.dataframe(
                [
                    {
                        "#": idx,
                        "URL": rec.url,
                        "Artist": rec.artist or "",
                        "Title": rec.title or "",
                    }
                    for idx, rec in enumerate(records, 1)
                ],
                **_STRETCH,
            )

            skip_existing = st.checkbox(
                "Skip tracks already present in library",
                value=True,
                key="csv_skip_existing",
            )
            if st.button(
                "Run CSV Download, Deduplication & Organization",
                type="primary",
                **_STRETCH,
                key="csv_run_btn",
            ):
                try:
                    lib_dir = validate_library_directory(output_value, create=True)
                    services = _parse_comma_list(services_value) or None
                    with st.spinner(f"Ingesting {len(records)} track(s) into {lib_dir}..."):
                        summary = Downloader(
                            lib_dir,
                            max_concurrent=concurrency_value,
                            skip_existing=skip_existing,
                            services=services,
                        ).download(records)
                    st.session_state["csv_ingest_summary"] = summary
                except Exception as exc:
                    st.error(f"{type(exc).__name__}: {exc}")
        except CSVProcessorError as exc:
            st.error(f"{type(exc).__name__}: {exc}")

    csv_summary: DownloadSummary | None = st.session_state.get("csv_ingest_summary")
    if csv_summary is not None:
        _render_download_summary(csv_summary, "CSV library ingestion complete.")

    st.divider()
    st.markdown("#### Flat Library Browser")
    col_refresh, col_info = st.columns([1, 3])
    col_refresh.button("Refresh library view", key="csv_refresh_browser_btn")

    candidate = Path(output_value.strip()).expanduser().resolve() if output_value.strip() else None
    if candidate is not None and candidate.exists() and candidate.is_dir():
        rows = _scan_library_rows(candidate)
        col_info.caption(f"Folder: `{candidate}` — {len(rows)} audio file(s)")
        if rows:
            st.dataframe(rows, **_STRETCH)
        else:
            st.info("No audio files found in the library folder yet.")
    else:
        col_info.caption("Library folder has not been created yet.")
        st.info("Click 'Validate library folder' or run a download to initialize the directory.")


st.set_page_config(page_title="Songetter", page_icon="♫", layout="wide")
st.title("Songetter")
st.caption("Multi-source music search, batch downloader, and flat library organizer.")

with st.sidebar:
    st.subheader("Library")
    default_library = os.environ.get("SONGETTER_LIBRARY_DIR") or "./Music"
    output_value = st.text_input("Destination folder", value=default_library)
    services_value = st.text_area(
        "Mornye providers (priority order)",
        value=", ".join(DEFAULT_SERVICES),
        help="Mornye installs the required providers from the integrated catalog. Remove or reorder IDs as needed.",
    )
    lyrics_value = st.text_input("Lyrics providers", value="apple,lrclib")
    concurrency_value = st.slider(
        "Concurrent track downloads",
        min_value=1,
        max_value=8,
        value=2,
        help="Maximum number of tracks downloaded simultaneously.",
    )
    st.caption("Registry catalog: " + MORNYE_REGISTRY)
    st.info(
        "Songetter preserves existing registries and adds the Mornye catalog. "
        "Extensions are third-party code; review their requirements and permissions."
    )

tab_links, tab_search, tab_csv = st.tabs(
    ["Links & Batch Download", "Multi-Source Search", "CSV Library Ingestion"]
)

with tab_links:
    _render_links_tab(output_value, services_value, lyrics_value, concurrency_value)

with tab_search:
    _render_search_tab(output_value, services_value, concurrency_value)

with tab_csv:
    _render_csv_tab(output_value, services_value, concurrency_value)

st.divider()
st.caption("Only download content you are authorized to access, and comply with each service's terms.")
