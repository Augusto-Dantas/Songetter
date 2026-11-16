"""Comprehensive offline API and UI tests for songetter.web (Milestone 3)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC

from songetter import search as search_mod
from songetter.csv_processor import parse_csv
from songetter.search import (
    SearchProvider,
    SearchProviderError,
    SearchResult,
    register_search_provider,
)
from songetter.web import app as package_app
from songetter.web import create_app
from songetter.web.app import app as module_app
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


def test_web_ui_renders_all_panels_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    assert package_app is module_app

    client = TestClient(create_app(default_library_dir=tmp_path / "ui_lib"))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    html = resp.text
    lower = html.lower()

    assert "library" in lower
    assert "directory" in lower or "folder" in lower
    assert 'type="file"' in lower
    assert 'accept=".csv"' in html
    assert "spotify" in lower
    assert "urls" in lower
    assert "search" in lower
    for src in ("deezer", "tidal", "itunes", "musicbrainz", "qobuz", "spotify", "all"):
        assert src in lower
    assert "/api/organize" in html
    assert "/api/search/export" in html
    assert "/api/search/download" in html
    assert "/api/jobs" in html
    assert "/api/library/tracks" in html


def test_startup_default_and_env_library_dir_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Explicit default_library_dir takes precedence over SONGETTER_LIBRARY_DIR
    explicit_lib = tmp_path / "explicit_lib"
    explicit_lib.mkdir()
    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(tmp_path / "ignored_env_lib"))
    app_explicit = create_app(default_library_dir=explicit_lib)
    assert app_explicit.state.library_dir == explicit_lib.resolve()
    assert app_explicit.state.jobs == {}
    assert app_explicit.state.last_upload_records == []

    # 2. SONGETTER_LIBRARY_DIR used when default_library_dir is None
    env_lib = tmp_path / "env_music_lib"
    monkeypatch.setenv("SONGETTER_LIBRARY_DIR", str(env_lib))
    app_env = create_app()
    assert app_env.state.library_dir == env_lib.resolve()

    client_env = TestClient(app_env)
    r_missing = client_env.get("/api/library")
    assert r_missing.status_code == 200
    assert r_missing.json() == {"path": str(env_lib.resolve()), "exists": False}

    env_lib.mkdir()
    r_exists = client_env.get("/api/library")
    assert r_exists.status_code == 200
    assert r_exists.json() == {"path": str(env_lib.resolve()), "exists": True}

    # 3. Fallback to Path('./Music').resolve() when neither is set
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    app_default = create_app()
    assert app_default.state.library_dir == (tmp_path / "Music").resolve()
    assert not (tmp_path / "Music").exists()


def test_library_selection_and_validation_errors(
    tmp_path: Path,
    clean_library_dir: Path,
    unwritable_library_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    client = TestClient(create_app(default_library_dir=tmp_path / "init_lib"))

    # Empty initially
    init_resp = client.get("/api/library/tracks")
    assert init_resp.status_code == 200
    assert init_resp.json()["path"] == str((tmp_path / "init_lib").resolve())
    assert init_resp.json()["tracks"] == []

    # Valid existing directory
    ok_resp = client.post("/api/library", json={"path": str(clean_library_dir)})
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"path": str(clean_library_dir.resolve()), "status": "ok"}
    assert client.get("/api/library").json() == {
        "path": str(clean_library_dir.resolve()),
        "exists": True,
    }

    # Auto-create nonexistent directory when create=True
    new_dir = tmp_path / "nested" / "created_lib"
    create_resp = client.post("/api/library", json={"path": str(new_dir), "create": True})
    assert create_resp.status_code == 200
    assert new_dir.is_dir()
    assert Path(create_resp.json()["path"]) == new_dir.resolve()

    # Nonexistent without create -> 400
    assert client.post("/api/library", json={"path": str(tmp_path / "missing_lib")}).status_code == 400

    # Empty path -> 400
    assert client.post("/api/library", json={"path": "   "}).status_code == 400

    # Regular file -> 400
    regular_file = tmp_path / "regular.txt"
    regular_file.write_text("hello", encoding="utf-8")
    assert client.post("/api/library", json={"path": str(regular_file)}).status_code == 400

    # Restricted root -> 400
    assert client.post("/api/library", json={"path": "/root"}).status_code == 400

    # Read-only directory -> 403
    assert client.post("/api/library", json={"path": str(unwritable_library_dir)}).status_code == 403


def test_library_tracks_listing_and_streaming_security(
    clean_library_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    mp3_path = create_synthetic_audio(
        clean_library_dir / "Daft Punk - Discovery.mp3",
        format="mp3",
        artist="Daft Punk",
        title="Discovery",
        bitrate_kbps=320,
    )
    flac_path = create_synthetic_audio(
        clean_library_dir / "Queen - Time.flac",
        format="flac",
        artist="Queen",
        title="Time",
    )
    # Non-audio file and .part file must be ignored
    (clean_library_dir / "notes.txt").write_text("ignore", encoding="utf-8")
    (clean_library_dir / "pending.mp3.part").write_bytes(b"partial")

    client = TestClient(create_app(default_library_dir=clean_library_dir))

    resp = client.get("/api/library/tracks")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["path"] == str(clean_library_dir.resolve())
    tracks = payload["tracks"]
    assert len(tracks) == 2
    assert {t["filename"] for t in tracks} == {
        "Daft Punk - Discovery.mp3",
        "Queen - Time.flac",
    }

    mp3_track = next(t for t in tracks if t["filename"] == "Daft Punk - Discovery.mp3")
    assert mp3_track["artist"] == "Daft Punk"
    assert mp3_track["title"] == "Discovery"
    assert mp3_track["bitrate"] == 320000
    assert mp3_track["file_size"] == mp3_path.stat().st_size
    assert mp3_track["duration"] > 0
    assert mp3_track["format"] == "mp3"

    flac_track = next(t for t in tracks if t["filename"] == "Queen - Time.flac")
    assert flac_track["format"] == "flac"

    # Stream valid MP3 and FLAC files
    s_mp3 = client.get("/api/library/tracks/Daft%20Punk%20-%20Discovery.mp3/stream")
    assert s_mp3.status_code == 200
    assert "audio/mpeg" in s_mp3.headers.get("content-type", "")
    assert s_mp3.content == mp3_path.read_bytes()

    s_flac = client.get("/api/library/tracks/Queen%20-%20Time.flac/stream")
    assert s_flac.status_code == 200
    assert "audio/flac" in s_flac.headers.get("content-type", "")
    assert s_flac.content == flac_path.read_bytes()

    # Missing track or non-audio file -> 404
    assert client.get("/api/library/tracks/Missing%20-%20Song.mp3/stream").status_code == 404
    assert client.get("/api/library/tracks/notes.txt/stream").status_code == 404

    # Path traversal variants (/, \, ..) -> 400
    for bad_path in (
        "/api/library/tracks/sub/Daft%20Punk%20-%20Discovery.mp3/stream",
        "/api/library/tracks/..%2Fsecret.mp3/stream",
        "/api/library/tracks/sub%5Ctrack.mp3/stream",
        "/api/library/tracks/..%5Csecret.mp3/stream",
        "/api/library/tracks/bad..name.mp3/stream",
    ):
        assert client.get(bad_path).status_code == 400, f"Expected 400 for {bad_path}"


def test_csv_upload_dedup_auto_download_and_error_cases(
    tmp_path: Path,
    clean_library_dir: Path,
    mock_audio_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    app = create_app(default_library_dir=clean_library_dir)
    client = TestClient(app)

    staging = tmp_path / "upload_staging"
    t1_bytes = create_synthetic_audio(
        staging / "t1.mp3", format="mp3", artist="Artist 1", title="Song 1"
    ).read_bytes()
    t2_bytes = create_synthetic_audio(
        staging / "t2.flac", format="flac", artist="Artist 2", title="Song 2"
    ).read_bytes()
    u1 = mock_audio_server.serve_bytes("t1.mp3", t1_bytes)
    u2 = mock_audio_server.serve_bytes("t2.flac", t2_bytes, content_type="audio/flac")

    # Includes a duplicate URL row to verify deduplicate_records()
    csv_bytes = create_synthetic_csv(
        [
            {"url": u1, "artist": "Artist 1", "title": "Song 1"},
            {"url": u2, "artist": "Artist 2", "title": "Song 2"},
            {"url": u1, "artist": "Artist 1", "title": "Song 1"},
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
    assert data["filename"] == "playlist.csv"
    assert data["total_tracks"] == 3
    assert data["valid_tracks"] == 2
    assert data["job_id"] is None
    assert data["preview"] == [
        {"url": u1, "artist": "Artist 1", "title": "Song 1"},
        {"url": u2, "artist": "Artist 2", "title": "Song 2"},
    ]
    assert len(app.state.last_upload_records) == 2

    # Upload with auto_download=true executes Downloader immediately
    auto_resp = client.post(
        "/api/upload?auto_download=true",
        files={"file": ("auto.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert auto_resp.status_code == 200
    auto_data = auto_resp.json()
    assert auto_data["job_id"] is not None
    job_data = client.get(f"/api/jobs/{auto_data['job_id']}").json()
    assert job_data["status"] == "completed"
    assert job_data["downloaded"] == 2
    assert (clean_library_dir / "Artist 1 - Song 1.mp3").is_file()
    assert (clean_library_dir / "Artist 2 - Song 2.flac").is_file()
    assert not list(clean_library_dir.rglob("*.lrc"))

    # Empty CSV -> 400
    assert (
        client.post(
            "/api/upload",
            files={"file": ("empty.csv", io.BytesIO(b"   \n"), "text/csv")},
        ).status_code
        == 400
    )

    # Binary data -> 400
    assert (
        client.post(
            "/api/upload",
            files={"file": ("bad.csv", io.BytesIO(b"\x00\xff\x00binary"), "text/csv")},
        ).status_code
        == 400
    )

    # Header-only / no valid URLs -> 400
    assert (
        client.post(
            "/api/upload",
            files={"file": ("nourl.csv", io.BytesIO(b"url,artist,title\nnot_a_url,A,B\n"), "text/csv")},
        ).status_code
        == 400
    )

    # Metadata headers without URL column -> 400
    assert (
        client.post(
            "/api/upload",
            files={"file": ("missing_col.csv", io.BytesIO(b"artist,title\nA,B\n"), "text/csv")},
        ).status_code
        == 400
    )


def test_jobs_lifecycle_urls_records_csv_text_and_errors(
    tmp_path: Path,
    clean_library_dir: Path,
    unwritable_library_dir: Path,
    mock_audio_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    client = TestClient(create_app(default_library_dir=clean_library_dir))

    # Unknown job -> 404
    assert client.get("/api/jobs/nonexistent").status_code == 404

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
    assert (
        client.post(
            "/api/upload",
            files={"file": ("batch.csv", io.BytesIO(csv_bytes), "text/csv")},
        ).status_code
        == 200
    )

    # 1. Trigger job using last_upload_records
    job_resp = client.post("/api/jobs")
    assert job_resp.status_code == 200
    job = job_resp.json()
    job_id = job["job_id"]
    assert job["status"] == "completed"
    assert job["downloaded"] == 1
    assert job["skipped"] == 1
    assert job["failed"] == 1
    assert len(job["tracks"]) == 3
    assert isinstance(job["duplicates_resolved"], list)
    assert isinstance(job["organized_files"], list)
    assert mock_audio_server.get_request_count("existing.mp3") == 0
    assert mock_audio_server.get_request_count("fresh.mp3") == 1
    assert mock_audio_server.get_request_count("broken.mp3") == 1

    # Retrieve job via GET /api/jobs/{job_id} and GET /api/jobs
    assert client.get(f"/api/jobs/{job_id}").json()["downloaded"] == 1
    all_jobs_resp = client.get("/api/jobs")
    assert all_jobs_resp.status_code == 200
    assert all_jobs_resp.json()["count"] == 1

    # 2. Trigger job via urls list into a dedicated target directory
    urls_lib = tmp_path / "urls_lib"
    urls_lib.mkdir()
    u_direct = mock_audio_server.serve_bytes("direct_url.mp3", new_mp3)
    urls_job = client.post(
        "/api/jobs",
        json={"urls": [u_direct], "library_path": str(urls_lib)},
    )
    assert urls_job.status_code == 200
    assert urls_job.json()["downloaded"] == 1
    assert (urls_lib / "New Artist - Fresh Song.mp3").is_file()

    # 3. Trigger job via records with quality deduplication
    dup_lib = tmp_path / "dup_lib"
    dup_lib.mkdir()
    u_low = mock_audio_server.serve_bytes("dup_low.mp3", low_dup)
    u_high = mock_audio_server.serve_bytes("dup_high.mp3", high_dup)
    dup_job_resp = client.post(
        "/api/jobs",
        json={
            "library_path": str(dup_lib),
            "skip_existing": False,
            "records": [
                {"url": u_low, "artist": "Dup Band", "title": "Same Track"},
                {"url": u_high, "artist": "Dup Band", "title": "Same Track"},
            ],
        },
    )
    assert dup_job_resp.status_code == 200
    dup_job = dup_job_resp.json()
    assert dup_job["status"] == "completed"
    assert dup_job["downloaded"] == 2
    assert len(dup_job["duplicates_resolved"]) >= 1
    assert len(dup_job["organized_files"]) == 1
    remaining_files = [p for p in dup_lib.iterdir() if p.is_file()]
    assert [p.name for p in remaining_files] == ["Dup Band - Same Track.mp3"]

    # 4. Trigger job via csv_text
    csv_lib = tmp_path / "csv_text_lib"
    csv_lib.mkdir()
    u_csv = mock_audio_server.serve_bytes("csv_song.mp3", new_mp3)
    csv_job = client.post(
        "/api/jobs",
        json={
            "library_path": str(csv_lib),
            "csv_text": f"url,artist,title\n{u_csv},New Artist,Fresh Song\n",
        },
    )
    assert csv_job.status_code == 200
    assert csv_job.json()["downloaded"] == 1
    assert (csv_lib / "New Artist - Fresh Song.mp3").is_file()

    # 5. Error paths: invalid URLs, nonexistent library_path without create, file, readonly
    assert client.post("/api/jobs", json={"urls": ["not_a_url"]}).status_code == 400
    assert client.post("/api/jobs", json={"urls": []}).status_code == 400
    assert (
        client.post(
            "/api/jobs",
            json={"urls": [url_new], "library_path": str(tmp_path / "missing_target")},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/jobs",
            json={"urls": [url_new], "library_path": str(unwritable_library_dir)},
        ).status_code
        == 403
    )


def test_organize_endpoint_deduplicates_flattens_and_preserves_embedded_lyrics(
    tmp_path: Path,
    clean_library_dir: Path,
    unwritable_library_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONGETTER_LIBRARY_DIR", raising=False)
    sub_low = clean_library_dir / "Artist" / "Low"
    sub_high = clean_library_dir / "Artist" / "High"
    empty_sub = clean_library_dir / "Empty" / "Nested"
    sub_low.mkdir(parents=True)
    sub_high.mkdir(parents=True)
    empty_sub.mkdir(parents=True)

    create_synthetic_audio(
        sub_low / "low.mp3",
        format="mp3",
        artist="Queen",
        title="Show Must Go On",
        bitrate_kbps=128,
    )
    high_flac = create_synthetic_audio(
        sub_high / "high.flac",
        format="flac",
        artist="Queen",
        title="Show Must Go On",
        bitrate_kbps=1411,
    )
    flac_tags = FLAC(high_flac)
    flac_tags["lyrics"] = "[00:01.00] Empty spaces"
    flac_tags.save()

    client = TestClient(create_app(default_library_dir=clean_library_dir))
    resp = client.post("/api/organize")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["duplicates_resolved_count"] == 1
    assert data["deleted_count"] == 1
    assert data["organized_count"] == 1
    assert data["filenames"] == ["Queen - Show Must Go On.flac"]

    final_file = clean_library_dir / "Queen - Show Must Go On.flac"
    assert final_file.is_file()
    assert FLAC(final_file).get("lyrics") == ["[00:01.00] Empty spaces"]
    assert not (clean_library_dir / "Artist").exists()
    assert not (clean_library_dir / "Empty").exists()
    assert not list(clean_library_dir.rglob("*.lrc"))

    # Read-only or missing library errors
    assert (
        client.post("/api/organize", json={"library_path": str(unwritable_library_dir)}).status_code
        == 403
    )
    assert (
        client.post("/api/organize", json={"library_path": str(tmp_path / "missing")}).status_code
        == 400
    )


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
    source_ids = {s["id"] for s in src_resp.json()["sources"]}
    assert {"deezer", "tidal", "itunes", "musicbrainz", "qobuz", "spotify"}.issubset(source_ids)

    # 2. GET /api/search with single source + type param alias
    get_resp = client.get(
        "/api/search",
        params={"q": "Daft Punk", "source": "deezer", "limit": 1, "type": "track"},
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["results"][0]["track_id"] == "dz-1"
    assert get_data["errors"] == {}
    assert deezer_stub.calls[-1]["search_type"] == "track"

    # 3. GET /api/search omitting sources defaults to federated "all"
    fed_resp = client.get("/api/search", params={"q": "Daft", "limit": 5})
    assert fed_resp.status_code == 200
    fed_data = fed_resp.json()
    assert len(fed_data["results"]) == 4
    assert "musicbrainz" in fed_data["errors"]

    # 4. GET /api/search with comma-separated sources
    comma_resp = client.get(
        "/api/search",
        params={"q": "Daft", "sources": "deezer,itunes", "deduplicate": "true"},
    )
    assert comma_resp.status_code == 200
    assert [r["track_id"] for r in comma_resp.json()["results"]] == ["dz-1", "dz-2", "it-2"]

    # 5. POST /api/search multi-source with deduplication + error isolation
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
    assert post_data["count"] == 3
    assert [r["track_id"] for r in post_data["results"]] == ["dz-1", "dz-2", "it-2"]
    assert "musicbrainz" in post_data["errors"]

    # 6. Invalid source -> HTTP 400 on both GET and POST
    assert client.get("/api/search", params={"q": "test", "source": "unknown_xyz"}).status_code == 400
    assert client.post("/api/search", json={"query": "test", "sources": ["unknown_xyz"]}).status_code == 400
    assert client.post("/api/search", json={"query": "test", "sources": "deezer,unknown_xyz"}).status_code == 400
    assert client.get("/api/search", params={"q": "test", "source": ""}).status_code == 400


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

    resp_query = client.post(
        "/api/search/export",
        json={"query": "Instant Crush", "sources": ["deezer"], "limit": 5},
    )
    assert resp_query.status_code == 200
    parsed_2 = parse_csv(resp_query.content)
    assert len(parsed_2) == 1
    assert parsed_2[0].url == "https://www.deezer.com/track/10"

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

    client = TestClient(create_app(default_library_dir=clean_library_dir))

    # 1. Preview-only result with use_preview_if_no_url=False -> 400
    assert (
        client.post(
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
        ).status_code
        == 400
    )
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
    p_job = client.get(f"/api/jobs/{preview_resp.json()['job_id']}").json()
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
    q_job = client.get(f"/api/jobs/{query_dl_resp.json()['job_id']}").json()
    assert q_job["status"] == "completed"
    assert q_job["downloaded"] == 1
    assert (alt_lib / "Direct Artist - Direct Song.flac").exists()
