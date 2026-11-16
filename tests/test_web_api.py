"""Comprehensive offline API and UI tests for songetter.web (Milestone 3)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from songetter import search as search_mod
from songetter.csv_processor import parse_csv
from songetter.search import (
    SearchProvider,
    SearchProviderError,
    SearchResult,
    register_search_provider,
)
from songetter.web import create_app
from tests.e2e.conftest import MockAudioServer, create_synthetic_audio, create_synthetic_csv


@pytest.fixture
def mock_audio_server():
    """Provide an offline background MockAudioServer with guaranteed teardown."""
    server = MockAudioServer()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture
def clean_library_dir(tmp_path: Path) -> Path:
    """Create a clean, writable directory for web library tests."""
    lib = tmp_path / "clean_library"
    lib.mkdir(parents=True, exist_ok=True)
    return lib


@pytest.fixture
def unwritable_library_dir(tmp_path: Path):
    """Create a read-only directory (0o555) and restore permissions on teardown."""
    ro_dir = tmp_path / "unwritable_library"
    ro_dir.mkdir(parents=True, exist_ok=True)
    ro_dir.chmod(0o555)
    try:
        yield ro_dir
    finally:
        ro_dir.chmod(0o755)


@pytest.fixture
def restore_search_registry():
    """Preserve and restore songetter.search provider registry across tests."""
    with search_mod._REGISTRY_LOCK:
        original_providers = dict(search_mod._PROVIDERS)
        original_aliases = dict(search_mod._ALIASES)
    try:
        yield
    finally:
        with search_mod._REGISTRY_LOCK:
            search_mod._PROVIDERS.clear()
            search_mod._PROVIDERS.update(original_providers)
            search_mod._ALIASES.clear()
            search_mod._ALIASES.update(original_aliases)


class _StubSearchProvider(SearchProvider):
    """Deterministic in-memory search provider for offline web API tests."""

    requires_auth = False

    def __init__(
        self,
        name: str,
        display_name: str,
        results: Optional[List[SearchResult]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.description = f"Stub provider for {display_name}"
        self._results = list(results or [])
        self._error = error
        self.calls: List[Dict[str, Any]] = []

    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "track",
        client: Optional[httpx.Client] = None,
    ) -> List[SearchResult]:
        self.calls.append(
            {"query": query, "limit": limit, "search_type": search_type}
        )
        if self._error is not None:
            raise self._error
        return list(self._results[:limit])


def test_web_ui_renders_all_panels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    client = TestClient(create_app())

    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    html = resp.text
    lower = html.lower()

    # Library input, CSV upload input, search panel, job monitor, library table
    assert "library" in lower
    assert 'type="file"' in lower
    assert 'accept=".csv"' in html
    assert "search" in lower
    for src in ("deezer", "tidal", "itunes", "musicbrainz", "qobuz", "spotify", "all"):
        assert src in lower
    assert "/api/search/export" in html or "export csv" in lower
    assert "/api/search/download" in html or "download to library" in lower
    assert "/api/jobs" in html
    assert "/api/library/tracks" in html


def test_startup_env_library_dir_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_lib = tmp_path / "env_music_lib"
    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(env_lib))
    app = create_app()
    assert env_lib.is_dir()
    assert app.state.library_dir == env_lib.resolve()

    client = TestClient(app)
    resp = client.get("/api/library")
    assert resp.status_code == 200
    assert resp.json() == {"library_dir": str(env_lib.resolve()), "tracks": []}

    # Invalid env path (regular file) safely falls back to None without crashing startup
    bad_file = tmp_path / "not_a_dir.txt"
    bad_file.write_text("file", encoding="utf-8")
    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(bad_file))
    bad_app = create_app()
    assert bad_app.state.library_dir is None


def test_library_selection_and_validation_errors(
    tmp_path: Path,
    clean_library_dir: Path,
    unwritable_library_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    client = TestClient(create_app())

    # Initially None
    init_resp = client.get("/api/library/tracks")
    assert init_resp.status_code == 200
    assert init_resp.json() == {"library_dir": None, "tracks": []}

    # Valid existing directory
    ok_resp = client.post("/api/library", json={"path": str(clean_library_dir)})
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"path": str(clean_library_dir.resolve()), "status": "ok"}

    # Auto-create nonexistent directory when create=True
    new_dir = tmp_path / "nested" / "created_lib"
    create_resp = client.post("/api/library", json={"path": str(new_dir), "create": True})
    assert create_resp.status_code == 200
    assert new_dir.is_dir()
    assert Path(create_resp.json()["path"]) == new_dir.resolve()

    # Nonexistent without create -> 400
    missing_resp = client.post("/api/library", json={"path": str(tmp_path / "missing_lib")})
    assert missing_resp.status_code == 400

    # Empty path -> 400
    empty_resp = client.post("/api/library", json={"path": "   "})
    assert empty_resp.status_code == 400

    # Regular file -> 400
    regular_file = tmp_path / "regular.txt"
    regular_file.write_text("hello", encoding="utf-8")
    file_resp = client.post("/api/library", json={"path": str(regular_file)})
    assert file_resp.status_code == 400

    # Restricted root -> 400
    root_resp = client.post("/api/library", json={"path": "/root"})
    assert root_resp.status_code == 400

    # Read-only directory -> 403
    ro_resp = client.post("/api/library", json={"path": str(unwritable_library_dir)})
    assert ro_resp.status_code == 403


def test_library_tracks_listing_returns_extracted_metadata(
    clean_library_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    create_synthetic_audio(
        clean_library_dir / "Daft Punk - Discovery.mp3",
        format="mp3",
        artist="Daft Punk",
        title="Discovery",
        bitrate_kbps=320,
    )
    create_synthetic_audio(
        clean_library_dir / "Queen - Time.flac",
        format="flac",
        artist="Queen",
        title="Time",
    )
    # Non-audio file and .part file must be ignored
    (clean_library_dir / "notes.txt").write_text("ignore", encoding="utf-8")
    (clean_library_dir / "pending.mp3.part").write_bytes(b"partial")

    client = TestClient(create_app())
    assert client.post("/api/library", json={"path": str(clean_library_dir)}).status_code == 200

    for endpoint in ("/api/library", "/api/library/tracks"):
        resp = client.get(endpoint)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["library_dir"] == str(clean_library_dir.resolve())
        tracks = payload["tracks"]
        assert len(tracks) == 2
        filenames = {t["filename"] for t in tracks}
        assert filenames == {"Daft Punk - Discovery.mp3", "Queen - Time.flac"}
        mp3_track = next(t for t in tracks if t["filename"] == "Daft Punk - Discovery.mp3")
        assert mp3_track["artist"] == "Daft Punk"
        assert mp3_track["title"] == "Discovery"
        assert mp3_track["bitrate"] == 320000
        assert mp3_track["file_size"] > 0
        assert mp3_track["duration"] > 0
        assert Path(mp3_track["path"]).name == "Daft Punk - Discovery.mp3"


def test_csv_upload_valid_and_error_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    app = create_app()
    client = TestClient(app)

    csv_bytes = create_synthetic_csv(
        [
            {"url": "http://127.0.0.1/t1.mp3", "artist": "Artist 1", "title": "Song 1"},
            {"url": "http://127.0.0.1/t2.flac", "artist": "Artist 2", "title": "Song 2"},
        ],
        delimiter=";",
        bom=True,
    )
    ok_resp = client.post(
        "/api/upload",
        files={"file": ("playlist.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert ok_resp.status_code == 200
    data = ok_resp.json()
    assert data["total_tracks"] == 2
    assert data["valid_tracks"] == 2
    assert data["preview"] == [
        {"url": "http://127.0.0.1/t1.mp3", "artist": "Artist 1", "title": "Song 1"},
        {"url": "http://127.0.0.1/t2.flac", "artist": "Artist 2", "title": "Song 2"},
    ]
    assert len(app.state.last_upload_records) == 2

    # Empty CSV -> 400
    empty_resp = client.post(
        "/api/upload",
        files={"file": ("empty.csv", io.BytesIO(b"   \n"), "text/csv")},
    )
    assert empty_resp.status_code == 400

    # Binary data -> 400
    binary_resp = client.post(
        "/api/upload",
        files={"file": ("bad.csv", io.BytesIO(b"\x00\xff\x00binary"), "text/csv")},
    )
    assert binary_resp.status_code == 400

    # Header-only / no valid URLs -> 400
    no_urls_resp = client.post(
        "/api/upload",
        files={"file": ("nourl.csv", io.BytesIO(b"url,artist,title\nnot_a_url,A,B\n"), "text/csv")},
    )
    assert no_urls_resp.status_code == 400

    # Metadata headers without URL column -> 400
    missing_url_col = client.post(
        "/api/upload",
        files={"file": ("missing_col.csv", io.BytesIO(b"artist,title\nA,B\n"), "text/csv")},
    )
    assert missing_url_col.status_code == 400


def test_jobs_lifecycle_upload_skip_dedup_and_errors(
    tmp_path: Path,
    clean_library_dir: Path,
    unwritable_library_dir: Path,
    mock_audio_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    client = TestClient(create_app())

    # Unknown job -> 404
    assert client.get("/api/jobs/nonexistent").status_code == 404

    # No library selected -> 400
    assert client.post("/api/jobs", json={"tracks": [{"url": "http://127.0.0.1/a.mp3"}]}).status_code == 400

    # Select library
    assert client.post("/api/library", json={"path": str(clean_library_dir)}).status_code == 200

    # No tracks uploaded or supplied -> 400
    assert client.post("/api/jobs").status_code == 400

    # Pre-populate existing track in library for skip verification
    create_synthetic_audio(
        clean_library_dir / "Existing Artist - Old Track.mp3",
        format="mp3",
        artist="Existing Artist",
        title="Old Track",
        bitrate_kbps=128,
    )

    # Prepare synthetic audio bytes in temp dir outside library
    staging = tmp_path / "src_audio"
    staging.mkdir()
    new_mp3 = create_synthetic_audio(
        staging / "new.mp3",
        format="mp3",
        artist="New Artist",
        title="Fresh Song",
        bitrate_kbps=320,
    ).read_bytes()
    low_dup = create_synthetic_audio(
        staging / "dup_low.mp3",
        format="mp3",
        artist="Dup Band",
        title="Same Track",
        bitrate_kbps=128,
    ).read_bytes()
    high_dup = create_synthetic_audio(
        staging / "dup_high.mp3",
        format="mp3",
        artist="Dup Band",
        title="Same Track",
        bitrate_kbps=320,
    ).read_bytes()

    url_existing = mock_audio_server.serve_bytes("existing.mp3", b"SHOULD_NOT_FETCH")
    url_new = mock_audio_server.serve_bytes("fresh.mp3", new_mp3)
    url_fail = mock_audio_server.serve_error("broken.mp3", status_code=404)

    csv_bytes = create_synthetic_csv(
        [
            {"url": url_existing, "artist": "Existing Artist", "title": "Old Track"},
            {"url": url_new, "artist": "New Artist", "title": "Fresh Song"},
            {"url": url_fail, "artist": "Missing", "title": "Broken"},
        ]
    )
    upload_resp = client.post(
        "/api/upload",
        files={"file": ("batch.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert upload_resp.status_code == 200

    # Trigger job falling back to last_upload_records + app.state.library_dir
    job_resp = client.post("/api/jobs")
    assert job_resp.status_code == 200
    created = job_resp.json()
    job_id = created["job_id"]
    assert created["total_tracks"] == 3

    # BackgroundTasks completes before TestClient.post returns
    status_resp = client.get(f"/api/jobs/{job_id}")
    assert status_resp.status_code == 200
    job = status_resp.json()
    assert job["status"] == "completed"
    assert job["total_tracks"] == 3
    assert job["downloaded"] == 1
    assert job["skipped"] == 1
    assert job["failed"] == 1
    assert len(job["tracks"]) == 3
    assert mock_audio_server.get_request_count("existing.mp3") == 0
    assert mock_audio_server.get_request_count("fresh.mp3") == 1
    assert mock_audio_server.get_request_count("broken.mp3") == 1

    # Verify list jobs
    all_jobs_resp = client.get("/api/jobs")
    assert all_jobs_resp.status_code == 200
    assert all_jobs_resp.json()["count"] == 1
    assert all_jobs_resp.json()["jobs"][0]["job_id"] == job_id

    # Verify explicit tracks + quality deduplication via job
    dup_lib = tmp_path / "dup_lib"
    u_low = mock_audio_server.serve_bytes("dup_low.mp3", low_dup)
    u_high = mock_audio_server.serve_bytes("dup_high.mp3", high_dup)
    dup_job_resp = client.post(
        "/api/jobs",
        json={
            "library_path": str(dup_lib),
            "create": True,
            "skip_existing": False,
            "tracks": [
                {"url": u_low, "artist": "Dup Band", "title": "Same Track"},
                {"url": u_high, "artist": "Dup Band", "title": "Same Track"},
            ],
        },
    )
    assert dup_job_resp.status_code == 200
    dup_job = client.get(f"/api/jobs/{dup_job_resp.json()['job_id']}").json()
    assert dup_job["status"] == "completed"
    assert dup_job["downloaded"] == 2
    assert len(dup_job["duplicates_resolved"]) >= 1
    assert len(dup_job["organized_files"]) == 1
    remaining_files = [p for p in dup_lib.iterdir() if p.is_file()]
    assert len(remaining_files) == 1
    assert remaining_files[0].name == "Dup Band - Same Track.mp3"

    # Invalid library_path override -> 403 for readonly
    ro_job_resp = client.post(
        "/api/jobs",
        json={
            "library_path": str(unwritable_library_dir),
            "tracks": [{"url": url_new}],
        },
    )
    assert ro_job_resp.status_code == 403


def test_search_sources_and_search_endpoints_mocked(
    restore_search_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)

    deezer_stub = _StubSearchProvider(
        "deezer",
        "Deezer",
        results=[
            SearchResult(
                source="deezer",
                track_id="dz-1",
                title="Around the World",
                artist="Daft Punk",
                album="Homework",
                duration_seconds=429,
                url="https://www.deezer.com/track/dz-1",
                preview_url="https://cdn.example.test/dz-1.mp3",
                isrc="FRZID9700010",
            ),
            SearchResult(
                source="deezer",
                track_id="dz-2",
                title="Da Funk",
                artist="Daft Punk",
                album="Homework",
                duration_seconds=328,
                url="https://www.deezer.com/track/dz-2",
                isrc="FRZID9600020",
            ),
        ],
    )
    itunes_stub = _StubSearchProvider(
        "itunes",
        "iTunes / Apple Music",
        results=[
            SearchResult(
                source="itunes",
                track_id="it-1",
                title="Around the World",
                artist="Daft Punk",
                album="Homework",
                duration_seconds=429,
                url="https://music.apple.com/track/it-1",
                isrc="FRZID9700010",
            ),
            SearchResult(
                source="itunes",
                track_id="it-2",
                title="Digital Love",
                artist="Daft Punk",
                album="Discovery",
                duration_seconds=301,
                url="https://music.apple.com/track/it-2",
            ),
        ],
    )
    broken_stub = _StubSearchProvider(
        "musicbrainz",
        "MusicBrainz",
        error=SearchProviderError("Upstream timeout", provider="musicbrainz", status_code=504),
    )

    register_search_provider("deezer", deezer_stub)
    register_search_provider("itunes", itunes_stub, aliases=("apple",))
    register_search_provider("musicbrainz", broken_stub)

    client = TestClient(create_app())

    # 1. GET /api/search/sources
    src_resp = client.get("/api/search/sources")
    assert src_resp.status_code == 200
    sources_list = src_resp.json()["sources"]
    source_ids = {s["id"] for s in sources_list}
    assert {"deezer", "tidal", "itunes", "musicbrainz", "qobuz", "spotify"}.issubset(source_ids)

    # 2. GET /api/search single source + type param alias
    get_resp = client.get(
        "/api/search",
        params={"q": "Daft Punk", "source": "deezer", "limit": 1, "type": "track"},
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["query"] == "Daft Punk"
    assert get_data["sources"] == ["deezer"]
    assert get_data["count"] == 1
    assert get_data["results"][0]["track_id"] == "dz-1"
    assert get_data["errors"] == {}
    assert deezer_stub.calls[-1]["search_type"] == "track"

    # 3. POST /api/search multi-source with deduplication + error isolation
    post_resp = client.post(
        "/api/search",
        json={
            "query": "Daft Punk",
            "sources": ["deezer", "itunes", "musicbrainz"],
            "limit": 5,
            "search_type": "track",
            "deduplicate": True,
        },
    )
    assert post_resp.status_code == 200
    post_data = post_resp.json()
    # dz-1 and it-1 share ISRC FRZID9700010, so deduplicated count is 3
    assert post_data["count"] == 3
    assert [r["track_id"] for r in post_data["results"]] == ["dz-1", "dz-2", "it-2"]
    assert "musicbrainz" in post_data["errors"]
    assert "timeout" in post_data["errors"]["musicbrainz"].lower()

    # 4. GET /api/search with sources=all
    all_resp = client.get("/api/search", params={"q": "Daft", "sources": "all", "deduplicate": "true"})
    assert all_resp.status_code == 200
    assert all_resp.json()["count"] == 3
    assert "musicbrainz" in all_resp.json()["errors"]

    # 5. Invalid source -> HTTP 400 on both GET and POST
    assert client.get("/api/search", params={"q": "test", "source": "unknown_xyz"}).status_code == 400
    assert client.post("/api/search", json={"query": "test", "sources": ["unknown_xyz"]}).status_code == 400
    assert client.post("/api/search", json={"query": "test", "sources": "deezer,unknown_xyz"}).status_code == 400
    assert client.get("/api/search", params={"q": "test", "source": ""}).status_code == 400

    # 6. Repeated query param ?sources=deezer&sources=itunes
    multi_get = client.get(
        "/api/search",
        params=[("q", "Daft"), ("sources", "deezer"), ("sources", "itunes"), ("deduplicate", "true")],
    )
    assert multi_get.status_code == 200
    assert multi_get.json()["sources"] == ["deezer", "itunes"]
    assert multi_get.json()["count"] == 3


def test_search_export_csv_from_results_and_query(
    restore_search_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    deezer_stub = _StubSearchProvider(
        "deezer",
        "Deezer",
        results=[
            SearchResult(
                source="deezer",
                track_id="10",
                title="Instant Crush",
                artist="Daft Punk",
                album="Random Access Memories",
                duration_seconds=337,
                url="https://www.deezer.com/track/10",
                isrc="USQX91300108",
            )
        ],
    )
    register_search_provider("deezer", deezer_stub)

    client = TestClient(create_app())

    # Export directly from submitted results
    resp_results = client.post(
        "/api/search/export",
        json={
            "results": [
                {
                    "source": "itunes",
                    "track_id": "20",
                    "title": "Get Lucky",
                    "artist": "Daft Punk",
                    "album": "RAM",
                    "duration_seconds": 248,
                    "url": "https://music.apple.com/track/20",
                    "isrc": "USQX91300105",
                }
            ]
        },
    )
    assert resp_results.status_code == 200
    assert "text/csv" in resp_results.headers["content-type"]
    parsed_1 = parse_csv(resp_results.content)
    assert len(parsed_1) == 1
    assert parsed_1[0].url == "https://music.apple.com/track/20"
    assert parsed_1[0].artist == "Daft Punk"
    assert parsed_1[0].title == "Get Lucky"

    # Export via query + sources + limit
    resp_query = client.post(
        "/api/search/export",
        json={"query": "Instant Crush", "sources": ["deezer"], "limit": 5},
    )
    assert resp_query.status_code == 200
    assert "text/csv" in resp_query.headers["content-type"]
    parsed_2 = parse_csv(resp_query.content)
    assert len(parsed_2) == 1
    assert parsed_2[0].url == "https://www.deezer.com/track/10"
    assert parsed_2[0].title == "Instant Crush"

    # Missing both results and query -> 400
    assert client.post("/api/search/export", json={}).status_code == 400


def test_search_download_with_results_query_and_preview_fallback(
    tmp_path: Path,
    clean_library_dir: Path,
    mock_audio_server,
    restore_search_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    staging = tmp_path / "search_audio_src"
    staging.mkdir()

    mp3_bytes = create_synthetic_audio(
        staging / "preview_track.mp3",
        format="mp3",
        artist="Preview Artist",
        title="Preview Song",
        bitrate_kbps=192,
    ).read_bytes()
    direct_bytes = create_synthetic_audio(
        staging / "direct_track.flac",
        format="flac",
        artist="Direct Artist",
        title="Direct Song",
    ).read_bytes()

    preview_url = mock_audio_server.serve_bytes("preview_track.mp3", mp3_bytes)
    direct_url = mock_audio_server.serve_bytes("direct_track.flac", direct_bytes)

    stub = _StubSearchProvider(
        "deezer",
        "Deezer",
        results=[
            SearchResult(
                source="deezer",
                track_id="p1",
                title="Preview Song",
                artist="Preview Artist",
                url="",
                preview_url=preview_url,
            ),
            SearchResult(
                source="deezer",
                track_id="d1",
                title="Direct Song",
                artist="Direct Artist",
                url=direct_url,
            ),
        ],
    )
    register_search_provider("deezer", stub)

    client = TestClient(create_app())
    assert client.post("/api/library", json={"path": str(clean_library_dir)}).status_code == 200

    # 1. Preview-only result with use_preview_if_no_url=False -> 400 (no downloadable URLs)
    no_url_resp = client.post(
        "/api/search/download",
        json={
            "results": [
                {
                    "source": "itunes",
                    "track_id": "p1",
                    "title": "Preview Song",
                    "artist": "Preview Artist",
                    "url": "",
                    "preview_url": preview_url,
                }
            ],
            "use_preview_if_no_url": False,
        },
    )
    assert no_url_resp.status_code == 400
    assert mock_audio_server.get_request_count("preview_track.mp3") == 0

    # 2. Preview-only result with use_preview_if_no_url=True -> downloads preview into library
    preview_resp = client.post(
        "/api/search/download",
        json={
            "results": [
                {
                    "source": "itunes",
                    "track_id": "p1",
                    "title": "Preview Song",
                    "artist": "Preview Artist",
                    "url": "",
                    "preview_url": preview_url,
                }
            ],
            "use_preview_if_no_url": True,
        },
    )
    assert preview_resp.status_code == 200
    p_data = preview_resp.json()
    assert p_data["submitted_tracks"] == 1
    p_job = client.get(f"/api/jobs/{p_data['job_id']}").json()
    assert p_job["status"] == "completed"
    assert p_job["downloaded"] == 1
    assert (clean_library_dir / "Preview Artist - Preview Song.mp3").exists()

    # 3. Query-based download with library_path override
    alt_lib = tmp_path / "alt_search_lib"
    query_dl_resp = client.post(
        "/api/search/download",
        json={
            "query": "Direct Song",
            "sources": ["deezer"],
            "limit": 5,
            "library_path": str(alt_lib),
            "create": True,
            "use_preview_if_no_url": False,
        },
    )
    assert query_dl_resp.status_code == 200
    q_data = query_dl_resp.json()
    assert q_data["submitted_tracks"] == 1
    q_job = client.get(f"/api/jobs/{q_data['job_id']}").json()
    assert q_job["status"] == "completed"
    assert q_job["downloaded"] == 1
    assert (alt_lib / "Direct Artist - Direct Song.flac").exists()

    # 4. Tracks-based download payload & missing library error check
    track_bytes = create_synthetic_audio(
        staging / "track_item.flac",
        format="flac",
        artist="Track Artist",
        title="Track Title",
    ).read_bytes()
    track_url = mock_audio_server.serve_bytes("track_item.flac", track_bytes)
    tracks_lib = tmp_path / "tracks_search_lib"
    tracks_dl_resp = client.post(
        "/api/search/download",
        json={
            "tracks": [
                {"url": track_url, "artist": "Track Artist", "title": "Track Title"}
            ],
            "library_path": str(tracks_lib),
            "create": True,
        },
    )
    assert tracks_dl_resp.status_code == 200
    assert tracks_dl_resp.json()["submitted_tracks"] == 1
    t_job = client.get(f"/api/jobs/{tracks_dl_resp.json()['job_id']}").json()
    assert t_job["status"] == "completed"
    assert (tracks_lib / "Track Artist - Track Title.flac").exists()

    # No library configured on fresh app -> 400
    fresh_client = TestClient(create_app())
    assert (
        fresh_client.post(
            "/api/search/download",
            json={"tracks": [{"url": direct_url, "artist": "A", "title": "B"}]},
        ).status_code
        == 400
    )
