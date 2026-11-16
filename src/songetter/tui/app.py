"""Textual TUI application for Songetter."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from songetter import core as _core
from songetter import downloader as _downloader
from songetter import search as _search
from songetter.config import AUDIO_SUFFIXES
from songetter.core import DEFAULT_SERVICES, parse_links, write_m3u8
from songetter.csv_processor import CSVProcessorError, TrackRecord, parse_csv
from songetter.deduplicator import DuplicateResolutionResult, extract_metadata, resolve_duplicates
from songetter.downloader import DownloadSummary
from songetter.library import LibraryError, validate_library_directory
from songetter.organizer import flatten_and_organize_library
from songetter.search import SearchError, SearchResult

_ORIG_DOWNLOADER = _downloader.Downloader
_ORIG_SEARCH_TRACKS_WITH_ERRORS = _search.search_tracks_with_errors
_ORIG_SEARCH_RESULTS_TO_CSV = _search.search_results_to_csv

Downloader = _ORIG_DOWNLOADER
search_tracks_with_errors = _ORIG_SEARCH_TRACKS_WITH_ERRORS
search_results_to_csv = _ORIG_SEARCH_RESULTS_TO_CSV

__all__ = ["SongetterTUI", "run_tui"]


def _pick(local_obj: Any, module_obj: Any, orig_obj: Any) -> Any:
    """Resolve patched symbol whether mocked on tui.app or source module."""
    return local_obj if local_obj is not orig_obj else module_obj


def _format_size(num_bytes: int) -> str:
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def _is_valid_url(candidate: str) -> bool:
    parsed = urlparse(candidate.strip())
    if parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc):
        return True
    if parsed.scheme.lower() == "spotify" and bool(parsed.path):
        return True
    return False


def _normalize_input_url(candidate: str) -> str:
    cleaned = candidate.strip()
    if not _is_valid_url(cleaned):
        raise ValueError(f"Invalid URL: {cleaned}")
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if host == "open.spotify.com":
        return parse_links(cleaned)[0]
    return cleaned


class SongetterTUI(App[int]):
    """Interactive Textual terminal UI for downloading, searching, and managing music libraries."""

    TITLE = "Songetter"
    SUB_TITLE = "Music Library Downloader, Search & Organizer"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-tabs {
        height: 1fr;
    }

    TabPane {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }

    .toolbar-row {
        height: auto;
        margin-bottom: 0;
    }

    .toolbar-row Input {
        width: 1fr;
    }

    #input-concurrency {
        width: 10;
    }

    #input-links {
        height: 3;
        min-height: 3;
    }

    #download-progress {
        height: auto;
    }

    #lbl-status, #lbl-metrics, #lbl-search-status, #lbl-library-summary, #lbl-doctor-report {
        height: auto;
        padding: 0 1;
    }

    #tbl-download-results {
        height: 1fr;
        min-height: 4;
    }

    #log-download {
        height: 3;
        min-height: 3;
        border: solid $surface-lighten-2;
    }

    #tbl-search-results, #tbl-library, #tbl-providers {
        height: 1fr;
        min-height: 5;
    }
    """

    BINDINGS = [
        Binding("d", "switch_tab('tab-download')", "Download"),
        Binding("s", "switch_tab('tab-search')", "Search"),
        Binding("l", "switch_tab('tab-library')", "Library"),
        Binding("p", "switch_tab('tab-doctor')", "Providers/Doctor"),
        Binding("r", "refresh_library", "Refresh library"),
        Binding("o", "organize_library", "Organize & Deduplicate"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, library_dir: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        raw_dir = (
            str(library_dir)
            if library_dir is not None and str(library_dir).strip()
            else (os.environ.get("SONGETTER_LIBRARY_DIR") or "./Music")
        )
        self.default_library_dir = raw_dir
        self._csv_records_by_url: dict[str, TrackRecord] = {}
        self._search_results: list[SearchResult] = []
        self._last_summary: Optional[DownloadSummary] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-download", id="main-tabs"):
            with TabPane("Download & Ingest", id="tab-download"):
                with Horizontal(classes="toolbar-row"):
                    yield Input(
                        value=self.default_library_dir,
                        placeholder="Library directory",
                        id="input-library-dir",
                    )
                    yield Input(
                        value="2",
                        placeholder="Conc.",
                        id="input-concurrency",
                    )
                    yield Input(
                        value=", ".join(DEFAULT_SERVICES),
                        placeholder="Mornye services (comma-separated)",
                        id="input-services",
                    )
                with Horizontal(classes="toolbar-row"):
                    yield Input(
                        value="",
                        placeholder="CSV or links file path...",
                        id="input-csv-path",
                    )
                    yield Button("Load CSV", id="btn-load-csv")
                    yield Button("Start Download", variant="primary", id="btn-start-download")
                yield TextArea(
                    "",
                    placeholder="Paste URLs (one per line) or load a CSV above...",
                    id="input-links",
                )
                yield ProgressBar(total=100, show_eta=False, id="download-progress")
                yield Static("Ready.", id="lbl-status")
                yield Static("Metrics: waiting for job", id="lbl-metrics")
                yield DataTable(id="tbl-download-results")
                yield RichLog(id="log-download", wrap=True)

            with TabPane("Search", id="tab-search"):
                with Horizontal(classes="toolbar-row"):
                    yield Input(
                        value="",
                        placeholder="Search tracks, albums, or artists...",
                        id="input-search-query",
                    )
                    yield Input(
                        value="deezer",
                        placeholder="Sources (deezer,itunes or all)",
                        id="input-search-sources",
                    )
                with Horizontal(classes="toolbar-row"):
                    yield Button("Search", variant="primary", id="btn-search")
                    yield Button("Download Results", id="btn-download-search")
                    yield Button("Export Search CSV", id="btn-export-search-csv")
                yield Static("Enter a query and press Search.", id="lbl-search-status")
                yield DataTable(id="tbl-search-results")

            with TabPane("Library & Organizer", id="tab-library"):
                with Horizontal(classes="toolbar-row"):
                    yield Button("Refresh Library", id="btn-refresh-lib")
                    yield Button("Organize & Deduplicate", variant="primary", id="btn-organize-lib")
                    yield Button("Export M3U8", id="btn-export-m3u8")
                yield Static("Library not scanned yet.", id="lbl-library-summary")
                yield DataTable(id="tbl-library")

            with TabPane("Providers & Doctor", id="tab-doctor"):
                with Horizontal(classes="toolbar-row"):
                    yield Button("Run Doctor", variant="primary", id="btn-run-doctor")
                    yield Button("Clear Cooldowns", id="btn-clear-cooldowns")
                yield Static("System diagnostics ready.", id="lbl-doctor-report")
                yield DataTable(id="tbl-providers")
        yield Footer()

    def on_mount(self) -> None:
        self._init_tables()
        self.refresh_providers_table()
        self.refresh_library_view(quiet_missing=True)
        self.run_doctor_check()

    def _init_tables(self) -> None:
        dl_tbl = self.query_one("#tbl-download-results", DataTable)
        dl_tbl.add_columns("Status", "Artist", "Title", "File / Error", "URL")

        search_tbl = self.query_one("#tbl-search-results", DataTable)
        search_tbl.add_columns("Source", "Artist", "Title", "Album", "Duration", "URL")

        lib_tbl = self.query_one("#tbl-library", DataTable)
        lib_tbl.add_columns("Filename", "Artist", "Title", "Bitrate", "Size", "Duration", "Format")

        prov_tbl = self.query_one("#tbl-providers", DataTable)
        prov_tbl.add_columns("Provider", "Status", "Retry After (s)", "Reason")

    def _is_app_thread(self) -> bool:
        app_tid = getattr(self, "_thread_id", None)
        return app_tid is None or app_tid == threading.get_ident()

    def _run_on_ui(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if self._is_app_thread():
            return fn(*args, **kwargs)
        return self.call_from_thread(fn, *args, **kwargs)

    def _active_library_dir(self) -> Path:
        raw = self.query_one("#input-library-dir", Input).value.strip()
        return Path(raw or self.default_library_dir or "./Music").expanduser()

    def _parse_concurrency(self) -> int:
        raw = self.query_one("#input-concurrency", Input).value.strip() or "2"
        value = int(raw)
        if value < 1:
            raise ValueError("Concurrency must be at least 1.")
        return value

    def _parse_services(self) -> Optional[List[str]]:
        raw = self.query_one("#input-services", Input).value.strip()
        if not raw:
            return None
        services = [part.strip() for part in re.split(r"[\n,;]+", raw) if part.strip()]
        return services or None

    # ------------------------------------------------------------------
    # Actions & Keybindings
    # ------------------------------------------------------------------

    def action_switch_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#main-tabs", TabbedContent)
        tabs.active = tab_id
        if tab_id == "tab-library":
            self.refresh_library_view(quiet_missing=True)
        elif tab_id == "tab-doctor":
            self.refresh_providers_table()
            self.run_doctor_check()
        # Keep focus on tab bar when switching via shortcut so consecutive bindings work.
        try:
            content_tabs = tabs.query_one("ContentTabs")
            content_tabs.focus()
        except Exception:
            pass

    def action_refresh_library(self) -> None:
        self.refresh_library_view(quiet_missing=False)

    def action_organize_library(self) -> None:
        self.organize_and_deduplicate_library()

    # ------------------------------------------------------------------
    # Button Handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-load-csv":
            self._handle_load_csv_click()
        elif btn_id == "btn-start-download":
            self._start_download_worker()
        elif btn_id == "btn-search":
            self._start_search_worker()
        elif btn_id == "btn-download-search":
            self._start_download_search_worker()
        elif btn_id == "btn-export-search-csv":
            self.export_search_csv()
        elif btn_id == "btn-refresh-lib":
            self.refresh_library_view(quiet_missing=False)
        elif btn_id == "btn-organize-lib":
            self.organize_and_deduplicate_library()
        elif btn_id == "btn-export-m3u8":
            self.export_library_m3u8()
        elif btn_id == "btn-run-doctor":
            self.run_doctor_check()
        elif btn_id == "btn-clear-cooldowns":
            self.clear_provider_cooldowns()

    # ------------------------------------------------------------------
    # Tab 1: Download & Ingest
    # ------------------------------------------------------------------

    def load_csv_file(self, csv_path: str | Path) -> List[TrackRecord]:
        """Parse a CSV file and populate links and status widgets."""
        path = Path(csv_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")
        if path.is_dir():
            raise IsADirectoryError(f"Expected CSV file, got directory: {path}")

        records = parse_csv(path.read_bytes(), filename=path.name)
        self._csv_records_by_url = {rec.url.strip(): rec for rec in records}

        links_widget = self.query_one("#input-links", TextArea)
        links_widget.text = "\n".join(rec.url for rec in records)

        tbl = self.query_one("#tbl-download-results", DataTable)
        tbl.clear()
        for rec in records:
            tbl.add_row("queued", rec.artist or "-", rec.title or "-", "pending", rec.url)

        msg = f"Loaded {len(records)} track(s) from {path.name}."
        self.query_one("#lbl-status", Static).update(msg)
        self.query_one("#log-download", RichLog).write(msg)
        return records

    def _handle_load_csv_click(self) -> None:
        raw_path = self.query_one("#input-csv-path", Input).value.strip()
        if not raw_path:
            self.query_one("#lbl-status", Static).update("Enter a CSV file path first.")
            return
        try:
            self.load_csv_file(raw_path)
        except (OSError, CSVProcessorError, ValueError) as exc:
            err = f"CSV load error: {exc}"
            self.query_one("#lbl-status", Static).update(err)
            self.query_one("#log-download", RichLog).write(err)

    def _collect_download_records(self) -> List[TrackRecord]:
        raw_links = self.query_one("#input-links", TextArea).text.strip()
        if raw_links:
            records: List[TrackRecord] = []
            for line in raw_links.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                for token in re.split(r"[\s,;]+", stripped):
                    token = token.strip()
                    if not token:
                        continue
                    if token in self._csv_records_by_url:
                        records.append(self._csv_records_by_url[token])
                    else:
                        records.append(TrackRecord(url=_normalize_input_url(token)))
            if not records:
                raise ValueError("No valid URLs found in input links.")
            return records

        csv_raw = self.query_one("#input-csv-path", Input).value.strip()
        if csv_raw:
            return self.load_csv_file(csv_raw)
        raise ValueError("Provide at least one URL or CSV file path.")

    def _apply_download_summary(self, summary: DownloadSummary, target_dir: Path) -> None:
        self._last_summary = summary
        total = max(1, len(summary.tracks))
        self.query_one("#download-progress", ProgressBar).update(
            total=total,
            progress=len(summary.tracks),
        )
        status_text = (
            f"Completed: downloaded={summary.downloaded} "
            f"skipped={summary.skipped} failed={summary.failed}"
        )
        metrics_text = (
            f"Library: {target_dir} | "
            f"Dedup groups: {len(summary.duplicates_resolved)} | "
            f"Organized: {len(summary.organized_files)}"
        )
        self.query_one("#lbl-status", Static).update(status_text)
        self.query_one("#lbl-metrics", Static).update(metrics_text)

        tbl = self.query_one("#tbl-download-results", DataTable)
        tbl.clear()
        log = self.query_one("#log-download", RichLog)
        log.write(status_text)

        for item in summary.tracks:
            detail = item.error or (str(item.file_path) if item.file_path else "-")
            tbl.add_row(
                item.status,
                item.artist or "-",
                item.title or "-",
                detail,
                item.url,
            )
            if item.error:
                log.write(f"ERROR [{item.title or item.url}]: {item.error}")

        self.refresh_library_view(target_dir, quiet_missing=True)
        self.refresh_providers_table()

    def execute_download_batch(
        self,
        records: Sequence[TrackRecord],
        library_dir: str | Path | None = None,
        *,
        max_concurrent: int | None = None,
        services: list[str] | None = None,
        skip_existing: bool = True,
        auto_organize: bool = True,
        auto_deduplicate: bool = True,
    ) -> DownloadSummary:
        """Synchronously run Downloader on records and update TUI state."""
        target_dir = Path(library_dir).expanduser() if library_dir is not None else self._active_library_dir()
        conc = max_concurrent if max_concurrent is not None else self._parse_concurrency()
        svcs = services if services is not None else self._parse_services()

        downloader_cls = _pick(Downloader, _downloader.Downloader, _ORIG_DOWNLOADER)
        kwargs: dict[str, Any] = {
            "max_concurrent": conc,
            "skip_existing": skip_existing,
            "auto_organize": auto_organize,
            "auto_deduplicate": auto_deduplicate,
        }
        if svcs is not None:
            kwargs["services"] = svcs

        instance = downloader_cls(target_dir, **kwargs)
        summary = instance.download(list(records))
        self._last_summary = summary
        if self.is_mounted:
            self._run_on_ui(self._apply_download_summary, summary, target_dir)
        return summary

    def _start_download_worker(self) -> None:
        try:
            records = self._collect_download_records()
            target_dir = self._active_library_dir()
            conc = self._parse_concurrency()
            svcs = self._parse_services()
        except (OSError, CSVProcessorError, ValueError) as exc:
            err = f"Download setup error: {exc}"
            self.query_one("#lbl-status", Static).update(err)
            self.query_one("#log-download", RichLog).write(err)
            return

        self.query_one("#lbl-status", Static).update(f"Downloading {len(records)} track(s)...")
        self.query_one("#download-progress", ProgressBar).update(total=max(1, len(records)), progress=0)
        self.query_one("#log-download", RichLog).write(
            f"Starting download batch ({len(records)} item(s)) -> {target_dir}"
        )

        def _worker() -> Optional[DownloadSummary]:
            try:
                return self.execute_download_batch(
                    records,
                    target_dir,
                    max_concurrent=conc,
                    services=svcs,
                )
            except Exception as exc:
                self._run_on_ui(self._report_download_error, str(exc))
                return None

        self.run_worker(_worker, thread=True, exclusive=True, name="download-batch")

    def _report_download_error(self, message: str) -> None:
        err = f"Download failed: {message}"
        self.query_one("#lbl-status", Static).update(err)
        self.query_one("#log-download", RichLog).write(err)

    # ------------------------------------------------------------------
    # Tab 2: Search
    # ------------------------------------------------------------------

    def _apply_search_results(
        self,
        query: str,
        results: List[SearchResult],
        errors: dict[str, str],
    ) -> None:
        self._search_results = list(results)
        tbl = self.query_one("#tbl-search-results", DataTable)
        tbl.clear()
        for item in results:
            tbl.add_row(
                item.source,
                item.artist or "-",
                item.title or "-",
                item.album or "-",
                _format_duration(item.duration_seconds),
                item.url or item.preview_url or "",
            )

        status = f"Query '{query}': {len(results)} result(s)"
        if errors:
            err_parts = ", ".join(f"{src}: {msg}" for src, msg in sorted(errors.items()))
            status = f"{status} | Errors: {err_parts}"
        self.query_one("#lbl-search-status", Static).update(status)

    def execute_search(
        self,
        query: str | None = None,
        sources: str | Sequence[str] | None = None,
        limit: int = 10,
        search_type: str = "track",
        deduplicate: bool = True,
    ) -> Tuple[List[SearchResult], dict[str, str]]:
        """Execute federated search and populate the Search results table."""
        q = (
            query
            if query is not None
            else self.query_one("#input-search-query", Input).value
        ).strip()
        if not q:
            raise ValueError("Enter a search query.")

        if sources is None:
            raw_sources = self.query_one("#input-search-sources", Input).value.strip() or "deezer"
            resolved_sources: str | List[str] = [
                tok.strip() for tok in raw_sources.split(",") if tok.strip()
            ] or ["deezer"]
        elif isinstance(sources, str):
            resolved_sources = [tok.strip() for tok in sources.split(",") if tok.strip()] or ["deezer"]
        else:
            resolved_sources = [str(tok).strip() for tok in sources if str(tok).strip()] or ["deezer"]

        search_fn = _pick(
            search_tracks_with_errors,
            _search.search_tracks_with_errors,
            _ORIG_SEARCH_TRACKS_WITH_ERRORS,
        )
        results, errors = search_fn(
            q,
            sources=resolved_sources,
            limit=limit,
            search_type=search_type,
            deduplicate=deduplicate,
        )
        self._run_on_ui(self._apply_search_results, q, results, errors)
        return results, errors

    def _start_search_worker(self) -> None:
        query = self.query_one("#input-search-query", Input).value.strip()
        raw_sources = self.query_one("#input-search-sources", Input).value.strip() or "deezer"
        if not query:
            self.query_one("#lbl-search-status", Static).update("Enter a search query.")
            return
        self.query_one("#lbl-search-status", Static).update(f"Searching '{query}'...")

        def _worker() -> None:
            try:
                self.execute_search(query=query, sources=raw_sources)
            except (SearchError, ValueError) as exc:
                self._run_on_ui(
                    self.query_one("#lbl-search-status", Static).update,
                    f"Search error: {exc}",
                )

        self.run_worker(_worker, thread=True, exclusive=True, name="search-tracks")

    def download_search_results(
        self,
        library_dir: str | Path | None = None,
    ) -> Optional[DownloadSummary]:
        """Download current search results into the active library."""
        if not self._search_results:
            self._run_on_ui(
                self.query_one("#lbl-search-status", Static).update,
                "No search results to download.",
            )
            return None

        records = [
            rec
            for item in self._search_results
            for rec in [item.to_track_record(use_preview_if_no_url=True)]
            if (rec.url or "").strip()
        ]
        if not records:
            self._run_on_ui(
                self.query_one("#lbl-search-status", Static).update,
                "Search results have no downloadable URLs.",
            )
            return None

        summary = self.execute_download_batch(records, library_dir=library_dir)
        self._run_on_ui(
            self.query_one("#lbl-search-status", Static).update,
            f"Search download: downloaded={summary.downloaded} "
            f"skipped={summary.skipped} failed={summary.failed}",
        )
        return summary

    def _start_download_search_worker(self) -> None:
        if not self._search_results:
            self.query_one("#lbl-search-status", Static).update("No search results to download.")
            return
        target_dir = self._active_library_dir()
        self.query_one("#lbl-search-status", Static).update(
            f"Downloading {len(self._search_results)} search result(s)..."
        )

        def _worker() -> Optional[DownloadSummary]:
            try:
                return self.download_search_results(library_dir=target_dir)
            except Exception as exc:
                self._run_on_ui(
                    self.query_one("#lbl-search-status", Static).update,
                    f"Search download error: {exc}",
                )
                return None

        self.run_worker(_worker, thread=True, exclusive=True, name="download-search")

    def export_search_csv(self, destination: str | Path | None = None) -> Optional[Path]:
        """Export current search results to an RFC 4180 CSV file."""
        if not self._search_results:
            self.query_one("#lbl-search-status", Static).update("No search results to export.")
            return None

        if destination is not None:
            dest = Path(destination).expanduser().resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            lib_dir = validate_library_directory(self._active_library_dir(), create=True)
            dest = lib_dir / "search_results.csv"

        to_csv_fn = _pick(
            search_results_to_csv,
            _search.search_results_to_csv,
            _ORIG_SEARCH_RESULTS_TO_CSV,
        )
        dest.write_text(to_csv_fn(self._search_results), encoding="utf-8")
        self.query_one("#lbl-search-status", Static).update(
            f"Exported {len(self._search_results)} result(s) to {dest}"
        )
        return dest

    # ------------------------------------------------------------------
    # Tab 3: Library & Organizer
    # ------------------------------------------------------------------

    def refresh_library_view(
        self,
        library_dir: str | Path | None = None,
        *,
        create: bool = False,
        quiet_missing: bool = False,
    ) -> List[Path]:
        """Scan library audio files and populate the Library DataTable."""
        target = Path(library_dir).expanduser() if library_dir is not None else self._active_library_dir()
        tbl = self.query_one("#tbl-library", DataTable)
        summary_lbl = self.query_one("#lbl-library-summary", Static)

        if quiet_missing and not target.exists():
            tbl.clear()
            summary_lbl.update(f"Library folder does not exist yet: {target}")
            return []

        try:
            valid_dir = validate_library_directory(target, create=create)
        except LibraryError as exc:
            tbl.clear()
            summary_lbl.update(f"Library error: {exc}")
            return []

        files = sorted(
            p
            for p in valid_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )
        tbl.clear()
        total_bytes = 0
        for audio_path in files:
            meta = extract_metadata(audio_path)
            total_bytes += meta.file_size
            try:
                rel_name = str(audio_path.relative_to(valid_dir))
            except ValueError:
                rel_name = audio_path.name
            bitrate_str = f"{meta.bitrate // 1000} kbps" if meta.bitrate else "-"
            tbl.add_row(
                rel_name,
                meta.artist,
                meta.title,
                bitrate_str,
                _format_size(meta.file_size),
                _format_duration(meta.duration),
                audio_path.suffix.lstrip(".").upper(),
            )

        summary_lbl.update(
            f"Library: {valid_dir} | {len(files)} track(s) | Total: {_format_size(total_bytes)}"
        )
        return files

    def organize_and_deduplicate_library(
        self,
        library_dir: str | Path | None = None,
    ) -> Tuple[List[DuplicateResolutionResult], List[Path]]:
        """Run quality-based deduplication and flat organization without touching .lrc files."""
        target = Path(library_dir).expanduser() if library_dir is not None else self._active_library_dir()
        summary_lbl = self.query_one("#lbl-library-summary", Static)
        try:
            valid_dir = validate_library_directory(target, create=False)
        except LibraryError as exc:
            summary_lbl.update(f"Organize error: {exc}")
            return [], []

        duplicates = resolve_duplicates(valid_dir)
        organized = flatten_and_organize_library(valid_dir)
        self.refresh_library_view(valid_dir, quiet_missing=False)
        summary_lbl.update(
            f"Library: {valid_dir} | Organized {len(organized)} track(s) | "
            f"Resolved {len(duplicates)} duplicate group(s)"
        )
        return duplicates, organized

    def export_library_m3u8(
        self,
        library_dir: str | Path | None = None,
        filename: str = "Songetter.m3u8",
    ) -> Optional[Path]:
        """Export current library audio files to an M3U8 playlist."""
        target = Path(library_dir).expanduser() if library_dir is not None else self._active_library_dir()
        summary_lbl = self.query_one("#lbl-library-summary", Static)
        try:
            valid_dir = validate_library_directory(target, create=False)
        except LibraryError as exc:
            summary_lbl.update(f"Export error: {exc}")
            return None

        files = sorted(
            p
            for p in valid_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )
        tracks_payload: list[dict[str, Any]] = []
        for audio_path in files:
            meta = extract_metadata(audio_path)
            tracks_payload.append(
                {
                    "status": "downloaded",
                    "file_path": str(audio_path),
                    "title": meta.title,
                    "artists": meta.artist,
                    "duration_ms": int(meta.duration * 1000),
                }
            )

        dest = valid_dir / filename
        count = write_m3u8(tracks_payload, dest, valid_dir)
        summary_lbl.update(f"Exported M3U8 playlist ({count} track(s)) to {dest}")
        return dest

    # ------------------------------------------------------------------
    # Tab 4: Providers & Doctor
    # ------------------------------------------------------------------

    def refresh_providers_table(self) -> dict[str, Any]:
        """Load Mornye provider cooldown state and update #tbl-providers."""
        cooldowns = _core._load_cooldowns()
        tbl = self.query_one("#tbl-providers", DataTable)
        tbl.clear()
        now = time.time()

        providers = list(DEFAULT_SERVICES)
        for extra in sorted(cooldowns):
            if extra not in providers:
                providers.append(extra)

        for provider in providers:
            entry = cooldowns.get(provider)
            if isinstance(entry, dict):
                until = float(entry.get("until", 0) or 0)
                remaining = int(until - now)
                if remaining > 0:
                    tbl.add_row(
                        provider,
                        "COOLDOWN",
                        str(remaining),
                        str(entry.get("reason", ""))[:80],
                    )
                    continue
            tbl.add_row(provider, "READY", "0", "-")
        return cooldowns

    def clear_provider_cooldowns(self) -> None:
        """Clear all persisted provider cooldowns and refresh the provider table."""
        _core._save_cooldowns({})
        self.refresh_providers_table()
        self.query_one("#lbl-doctor-report", Static).update(
            "Provider cooldowns cleared. All providers are READY."
        )

    def run_doctor_check(self, library_dir: str | Path | None = None) -> dict[str, Any]:
        """Run system diagnostics and render report into #lbl-doctor-report."""
        from songetter.cli import collect_doctor_report

        target = Path(library_dir).expanduser() if library_dir is not None else self._active_library_dir()
        report = collect_doctor_report(target)

        lines = [
            f"Python: {report['python']['version']} ({report['python']['executable']})",
            "Binaries: "
            + ", ".join(
                f"{name}={'OK' if report[name]['available'] else 'MISSING'}"
                for name in ("mornye", "ffmpeg", "ffprobe", "node", "yt-dlp")
            ),
            "Packages: "
            + ", ".join(
                f"{name}={report[name]['version'] or 'MISSING'}"
                for name in ("textual", "httpx", "fastapi", "mutagen")
            ),
        ]
        lib_info = report["library_dir"]
        lib_status = "WRITABLE" if lib_info["writable"] else (lib_info.get("error") or "NOT WRITABLE")
        lines.append(f"Library ({lib_info['path']}): {lib_status}")
        self.query_one("#lbl-doctor-report", Static).update("\n".join(lines))
        self.refresh_providers_table()
        return report


def run_tui(library_dir: str | Path | None = None) -> int:
    """Launch the Songetter Textual TUI application."""
    app = SongetterTUI(library_dir=library_dir)
    result = app.run()
    return int(result) if isinstance(result, int) else 0
