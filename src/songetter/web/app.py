"""FastAPI web application and REST API for Songetter (Milestone 3).

Provides:
- Server-side HTML UI via Jinja2 (`GET /`)
- Library directory validation, selection, and flat track listing (`/api/library`, `/api/library/tracks`)
- CSV upload, validation, and preview (`/api/upload`)
- Multi-source metadata search, CSV export, and direct download dispatch (`/api/search*`)
- Background download, deduplication, and organization jobs (`/api/jobs*`)
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from songetter import __version__
from songetter import downloader as _downloader
from songetter import search as _search
from songetter.config import AUDIO_SUFFIXES
from songetter.csv_processor import CSVProcessorError, TrackRecord, parse_csv
from songetter.deduplicator import DuplicateResolutionResult, extract_metadata
from songetter.downloader import TrackDownloadResult
from songetter.library import (
    InvalidPathError,
    LibraryError,
    NotADirectoryError as LibraryNotADirectoryError,
    PermissionDeniedError,
    validate_library_directory,
)
from songetter.search import (
    SearchError,
    SearchResult,
    UnsupportedSearchSourceError,
)

# Expose module-level references while also honoring patches on songetter.search / songetter.downloader.
_ORIG_SEARCH_TRACKS_WITH_ERRORS = _search.search_tracks_with_errors
_ORIG_LIST_SEARCH_SOURCES = _search.list_search_sources
_ORIG_SEARCH_RESULTS_TO_CSV = _search.search_results_to_csv
_ORIG_DOWNLOADER = _downloader.Downloader

search_tracks_with_errors = _ORIG_SEARCH_TRACKS_WITH_ERRORS
list_search_sources = _ORIG_LIST_SEARCH_SOURCES
search_results_to_csv = _ORIG_SEARCH_RESULTS_TO_CSV
Downloader = _ORIG_DOWNLOADER

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class LibrarySelectRequest(BaseModel):
    """Payload for selecting and validating the active music library directory."""

    path: Optional[str] = None
    create: bool = False
    create_if_missing: bool = False


class TrackInput(BaseModel):
    """JSON representation of a track queued for download."""

    url: str
    artist: Optional[str] = None
    title: Optional[str] = None


class JobCreateRequest(BaseModel):
    """Payload for triggering a background download, dedup, and organization job."""

    tracks: Optional[List[Union[TrackInput, str]]] = None
    library_path: Optional[str] = None
    create: bool = False
    max_concurrent: int = Field(default=2, ge=1, le=16)
    skip_existing: bool = True
    auto_organize: bool = True
    auto_deduplicate: bool = True


class SearchRequest(BaseModel):
    """JSON payload for executing a multi-source metadata search."""

    query: Optional[str] = None
    q: Optional[str] = None
    sources: Optional[Union[str, List[str]]] = None
    source: Optional[Union[str, List[str]]] = None
    limit: int = 10
    search_type: Optional[str] = None
    type: Optional[str] = None
    deduplicate: bool = False


class SearchExportRequest(SearchRequest):
    """JSON payload for exporting submitted or queried search results to CSV."""

    results: Optional[List[Dict[str, Any]]] = None


class SearchDownloadRequest(SearchRequest):
    """JSON payload for downloading search results or query hits into the library."""

    results: Optional[List[Dict[str, Any]]] = None
    tracks: Optional[List[Dict[str, Any]]] = None
    library_path: Optional[str] = None
    create: bool = False
    use_preview_if_no_url: bool = False
    max_concurrent: int = Field(default=2, ge=1, le=16)
    skip_existing: bool = True
    auto_organize: bool = True
    auto_deduplicate: bool = True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invoke_list_search_sources() -> List[Dict[str, Any]]:
    if list_search_sources is not _ORIG_LIST_SEARCH_SOURCES:
        return list_search_sources()
    return _search.list_search_sources()


def _invoke_search_tracks_with_errors(
    **kwargs: Any,
) -> tuple[List[SearchResult], Dict[str, str]]:
    if search_tracks_with_errors is not _ORIG_SEARCH_TRACKS_WITH_ERRORS:
        return search_tracks_with_errors(**kwargs)
    return _search.search_tracks_with_errors(**kwargs)


def _invoke_search_results_to_csv(results: Sequence[SearchResult]) -> str:
    if search_results_to_csv is not _ORIG_SEARCH_RESULTS_TO_CSV:
        return search_results_to_csv(results)
    return _search.search_results_to_csv(results)


def _get_downloader_cls() -> Any:
    if Downloader is not _ORIG_DOWNLOADER:
        return Downloader
    return _downloader.Downloader


def _validate_directory_or_http_error(path: Union[str, Path, None], create: bool = False) -> Path:
    try:
        return validate_library_directory(path, create=create)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (InvalidPathError, LibraryNotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_target_library(
    app: FastAPI,
    library_path: Optional[str],
    *,
    create: bool = False,
) -> Path:
    if library_path is not None:
        return _validate_directory_or_http_error(library_path, create=create)
    configured = getattr(app.state, "library_dir", None)
    if configured is None:
        raise HTTPException(
            status_code=400,
            detail="No library directory selected. Configure one via POST /api/library or pass library_path.",
        )
    return _validate_directory_or_http_error(configured, create=create)


def _scan_library_payload(library_dir: Optional[Path]) -> Dict[str, Any]:
    if library_dir is None or not library_dir.exists() or not library_dir.is_dir():
        return {"library_dir": None, "tracks": []}

    tracks: List[Dict[str, Any]] = []
    audio_paths = sorted(
        (
            p
            for p in library_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        ),
        key=lambda p: (p.name.lower(), str(p).lower()),
    )
    for audio_path in audio_paths:
        meta = extract_metadata(audio_path)
        tracks.append(
            {
                "filename": audio_path.name,
                "artist": meta.artist,
                "title": meta.title,
                "bitrate": meta.bitrate,
                "file_size": meta.file_size,
                "duration": meta.duration,
                "path": str(meta.path),
            }
        )
    return {"library_dir": str(library_dir), "tracks": tracks}


def _normalize_sources(
    sources: Optional[Union[str, Sequence[str]]],
    source: Optional[Union[str, Sequence[str]]],
) -> List[str]:
    raw_input = sources if sources is not None else source
    if raw_input is None:
        return ["deezer"]

    tokens: List[str] = []
    items = [raw_input] if isinstance(raw_input, str) else list(raw_input)
    for item in items:
        for part in str(item).split(","):
            cleaned = part.strip().lower()
            if cleaned:
                tokens.append(cleaned)

    if not tokens:
        raise HTTPException(status_code=400, detail="Search source name cannot be empty.")
    return tokens


def _execute_search(
    *,
    query: str,
    sources: List[str],
    limit: int,
    search_type: str,
    deduplicate: bool,
) -> tuple[List[SearchResult], Dict[str, str]]:
    capped_limit = max(0, min(int(limit), 100))
    source_arg: Union[str, List[str]] = sources[0] if len(sources) == 1 else sources
    try:
        return _invoke_search_tracks_with_errors(
            query=query,
            sources=source_arg,
            limit=capped_limit,
            search_type=search_type,
            deduplicate=deduplicate,
        )
    except UnsupportedSearchSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run_search_payload(
    payload: SearchRequest,
    *,
    require_query: bool = False,
    missing_query_detail: str = "Search query cannot be empty.",
) -> tuple[str, List[str], List[SearchResult], Dict[str, str]]:
    effective_query = payload.query if payload.query is not None else (payload.q or "")
    if require_query and not effective_query.strip():
        raise HTTPException(status_code=400, detail=missing_query_detail)
    normalized_sources = _normalize_sources(payload.sources, payload.source)
    effective_type = (payload.search_type or payload.type or "track").strip().lower()
    results, errors = _execute_search(
        query=effective_query,
        sources=normalized_sources,
        limit=payload.limit,
        search_type=effective_type,
        deduplicate=payload.deduplicate,
    )
    return effective_query, normalized_sources, results, errors


def _dict_to_search_result(item: Dict[str, Any]) -> SearchResult:
    raw_duration = item.get("duration_seconds")
    duration_seconds: Optional[int] = None
    if raw_duration is not None and raw_duration != "":
        try:
            duration_seconds = int(raw_duration)
        except (TypeError, ValueError):
            duration_seconds = None

    raw_extra = item.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}

    return SearchResult(
        source=str(item.get("source") or ""),
        track_id=str(item.get("track_id") or ""),
        title=str(item.get("title") or ""),
        artist=str(item.get("artist") or ""),
        album=str(item["album"]) if item.get("album") is not None else None,
        duration_seconds=duration_seconds,
        url=str(item.get("url") or ""),
        preview_url=str(item["preview_url"]) if item.get("preview_url") else None,
        cover_url=str(item["cover_url"]) if item.get("cover_url") else None,
        isrc=str(item["isrc"]) if item.get("isrc") else None,
        quality_hint=str(item["quality_hint"]) if item.get("quality_hint") else None,
        extra=extra,
    )


def _serialize_track_result(track: TrackDownloadResult) -> Dict[str, Any]:
    return {
        "url": track.url,
        "artist": track.artist,
        "title": track.title,
        "status": track.status,
        "file_path": str(track.file_path) if track.file_path is not None else None,
        "error": track.error,
    }


def _serialize_duplicate_result(dup: DuplicateResolutionResult) -> Dict[str, Any]:
    return {
        "retained": str(dup.retained),
        "deleted": [str(p) for p in dup.deleted],
        "artist": dup.artist,
        "title": dup.title,
        "reason": dup.reason,
    }


def _copy_job_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **job,
        "tracks": [dict(t) for t in job.get("tracks", [])],
        "duplicates_resolved": [dict(d) for d in job.get("duplicates_resolved", [])],
        "organized_files": list(job.get("organized_files", [])),
    }


def _run_download_job(
    app: FastAPI,
    job_id: str,
    records: List[TrackRecord],
    library_dir: Path,
    max_concurrent: int,
    skip_existing: bool,
    auto_organize: bool,
    auto_deduplicate: bool,
) -> None:
    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        if job is not None:
            job["status"] = "running"

    try:
        downloader_cls = _get_downloader_cls()
        downloader = downloader_cls(
            library_dir=library_dir,
            max_concurrent=max_concurrent,
            skip_existing=skip_existing,
            auto_organize=auto_organize,
            auto_deduplicate=auto_deduplicate,
        )
        summary = downloader.download(records)
        with app.state.jobs_lock:
            job = app.state.jobs.get(job_id)
            if job is not None:
                job["status"] = "completed"
                job["downloaded"] = summary.downloaded
                job["skipped"] = summary.skipped
                job["failed"] = summary.failed
                job["tracks"] = [_serialize_track_result(t) for t in summary.tracks]
                job["duplicates_resolved"] = [
                    _serialize_duplicate_result(d) for d in summary.duplicates_resolved
                ]
                job["organized_files"] = [str(p) for p in summary.organized_files]
                job["completed_at"] = _utc_now_iso()
    except Exception as exc:
        with app.state.jobs_lock:
            job = app.state.jobs.get(job_id)
            if job is not None:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["completed_at"] = _utc_now_iso()


def _enqueue_download_job(
    app: FastAPI,
    background_tasks: BackgroundTasks,
    records: List[TrackRecord],
    library_dir: Path,
    *,
    max_concurrent: int = 2,
    skip_existing: bool = True,
    auto_organize: bool = True,
    auto_deduplicate: bool = True,
) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    job_record: Dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",
        "library_dir": str(library_dir),
        "total_tracks": len(records),
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "tracks": [],
        "duplicates_resolved": [],
        "organized_files": [],
        "error": None,
        "created_at": _utc_now_iso(),
        "completed_at": None,
    }
    with app.state.jobs_lock:
        app.state.jobs[job_id] = job_record
        snapshot = _copy_job_snapshot(job_record)

    background_tasks.add_task(
        _run_download_job,
        app,
        job_id,
        list(records),
        library_dir,
        max_concurrent,
        skip_existing,
        auto_organize,
        auto_deduplicate,
    )
    return snapshot


def create_app() -> FastAPI:
    """Create and configure the Songetter FastAPI application."""
    app = FastAPI(
        title="Songetter",
        version=__version__,
        description="Server-side music library ingestion, multi-source search, and organization API.",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    env_dir = (os.environ.get("SONGETTER_LIBRARY_DIR") or "").strip()
    initial_library_dir: Optional[Path] = None
    if env_dir:
        try:
            initial_library_dir = validate_library_directory(env_dir, create=True)
        except (LibraryError, OSError):
            initial_library_dir = None

    app.state.library_dir = initial_library_dir
    app.state.jobs = {}
    app.state.jobs_lock = threading.Lock()
    app.state.last_upload_records = []

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        library_dir = getattr(request.app.state, "library_dir", None)
        sources = _invoke_list_search_sources()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "version": __version__,
                "library_dir": str(library_dir) if library_dir is not None else "",
                "sources": sources,
            },
        )

    @app.post("/api/library")
    def select_library(payload: LibrarySelectRequest) -> Dict[str, str]:
        resolved = _validate_directory_or_http_error(
            payload.path,
            create=payload.create or payload.create_if_missing,
        )
        app.state.library_dir = resolved
        return {"path": str(resolved), "status": "ok"}

    @app.get("/api/library")
    def get_library() -> Dict[str, Any]:
        return _scan_library_payload(getattr(app.state, "library_dir", None))

    @app.get("/api/library/tracks")
    def get_library_tracks() -> Dict[str, Any]:
        return _scan_library_payload(getattr(app.state, "library_dir", None))

    @app.post("/api/upload")
    async def upload_csv(file: UploadFile = File(...)) -> Dict[str, Any]:
        content = await file.read()
        try:
            records = parse_csv(content, filename=file.filename)
        except CSVProcessorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with app.state.jobs_lock:
            app.state.last_upload_records = list(records)

        return {
            "total_tracks": len(records),
            "valid_tracks": len(records),
            "preview": [
                {"url": r.url, "artist": r.artist, "title": r.title}
                for r in records
            ],
        }

    @app.post("/api/jobs")
    def create_job(
        background_tasks: BackgroundTasks,
        payload: Optional[JobCreateRequest] = Body(default=None),
    ) -> Dict[str, Any]:
        req = payload or JobCreateRequest()
        library_dir = _resolve_target_library(app, req.library_path, create=req.create)

        if req.tracks is not None:
            records: List[TrackRecord] = []
            for item in req.tracks:
                if isinstance(item, str):
                    url = item.strip()
                    if url:
                        records.append(TrackRecord(url=url))
                else:
                    url = (item.url or "").strip()
                    if url:
                        artist = item.artist.strip() if item.artist and item.artist.strip() else None
                        title = item.title.strip() if item.title and item.title.strip() else None
                        records.append(TrackRecord(url=url, artist=artist, title=title))
        else:
            with app.state.jobs_lock:
                records = list(app.state.last_upload_records)

        if not records:
            raise HTTPException(
                status_code=400,
                detail="No tracks available. Upload a CSV via POST /api/upload or supply tracks.",
            )

        return _enqueue_download_job(
            app,
            background_tasks,
            records,
            library_dir,
            max_concurrent=req.max_concurrent,
            skip_existing=req.skip_existing,
            auto_organize=req.auto_organize,
            auto_deduplicate=req.auto_deduplicate,
        )

    @app.get("/api/jobs")
    def list_jobs() -> Dict[str, Any]:
        with app.state.jobs_lock:
            jobs = [
                _copy_job_snapshot(job)
                for job in reversed(list(app.state.jobs.values()))
            ]
        return {"jobs": jobs, "count": len(jobs)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Dict[str, Any]:
        with app.state.jobs_lock:
            job = app.state.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
            return _copy_job_snapshot(job)

    @app.get("/api/search/sources")
    def get_search_sources() -> Dict[str, Any]:
        return {"sources": _invoke_list_search_sources()}

    @app.get("/api/search")
    def search_get(
        q: str = Query(default=""),
        query: Optional[str] = Query(default=None),
        source: Optional[List[str]] = Query(default=None),
        sources: Optional[List[str]] = Query(default=None),
        limit: int = Query(default=10),
        search_type: Optional[str] = Query(default=None),
        type_param: Optional[str] = Query(default=None, alias="type"),
        deduplicate: bool = Query(default=False),
    ) -> Dict[str, Any]:
        effective_query = q if q != "" else (query or "")
        normalized_sources = _normalize_sources(sources, source)
        effective_type = (search_type or type_param or "track").strip().lower()
        results, errors = _execute_search(
            query=effective_query,
            sources=normalized_sources,
            limit=limit,
            search_type=effective_type,
            deduplicate=deduplicate,
        )
        return {
            "query": effective_query,
            "sources": normalized_sources,
            "count": len(results),
            "results": [r.to_dict() for r in results],
            "errors": errors,
        }

    @app.post("/api/search")
    def search_post(payload: SearchRequest) -> Dict[str, Any]:
        effective_query, normalized_sources, results, errors = _run_search_payload(payload)
        return {
            "query": effective_query,
            "sources": normalized_sources,
            "count": len(results),
            "results": [r.to_dict() for r in results],
            "errors": errors,
        }

    @app.post("/api/search/export")
    def export_search_csv(payload: SearchExportRequest) -> Response:
        if payload.results is not None:
            search_results = [_dict_to_search_result(item) for item in payload.results]
        else:
            _, _, search_results, _ = _run_search_payload(
                payload,
                require_query=True,
                missing_query_detail="Provide either 'results' or a non-empty 'query' to export CSV.",
            )

        csv_payload = _invoke_search_results_to_csv(search_results)
        return Response(
            content=csv_payload,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="songetter_search.csv"'},
        )

    @app.post("/api/search/download")
    def download_search_results(
        payload: SearchDownloadRequest,
        background_tasks: BackgroundTasks,
    ) -> Dict[str, Any]:
        library_dir = _resolve_target_library(app, payload.library_path, create=payload.create)

        raw_items: Optional[List[Dict[str, Any]]] = (
            payload.results if payload.results is not None else payload.tracks
        )

        if raw_items is not None:
            search_results = [_dict_to_search_result(item) for item in raw_items]
        else:
            _, _, search_results, _ = _run_search_payload(
                payload,
                require_query=True,
                missing_query_detail="Provide 'results', 'tracks', or a non-empty 'query' to download.",
            )

        records = [
            sr.to_track_record(use_preview_if_no_url=payload.use_preview_if_no_url)
            for sr in search_results
        ]
        valid_records = [r for r in records if (r.url or "").strip()]
        if not valid_records:
            raise HTTPException(
                status_code=400,
                detail="No downloadable track URLs found in search items.",
            )

        job = _enqueue_download_job(
            app,
            background_tasks,
            valid_records,
            library_dir,
            max_concurrent=payload.max_concurrent,
            skip_existing=payload.skip_existing,
            auto_organize=payload.auto_organize,
            auto_deduplicate=payload.auto_deduplicate,
        )
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "submitted_tracks": len(valid_records),
            "total_tracks": len(valid_records),
            "library_dir": job["library_dir"],
        }

    return app
