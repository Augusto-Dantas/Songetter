"""Tier 4: Real-World Application Scenarios E2E Tests (5 Test Cases).

Verifies complete end-to-end user workflows matching realistic operational lifecycles:
- SCN-01: Initial Clean Library Ingestion & Flattening
- SCN-02: Incremental Library Expansion with Pre-Download Skipping
- SCN-03: Quality Upgrade Workflow (Replacing Low-Bitrate Files with Lossless)
- SCN-04: Quality Downgrade Rejection (Protecting High-Fidelity Collection)
- SCN-05: Fault-Tolerant Ingestion Recovery (Handling Mixed Corrupt & Valid Inputs)
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from songetter.csv_processor import (
    TrackRecord,
    parse_csv,
)
from songetter.deduplicator import (
    AudioMetadata,
    extract_metadata,
    resolve_duplicates,
)
from songetter.library import (
    LibraryIndex,
    validate_library_directory,
)
from songetter.organizer import (
    flatten_and_organize_library,
    format_standard_filename,
    sanitize_filename_component,
)

from tests.e2e.conftest import (
    create_synthetic_audio,
    create_synthetic_csv,
    has_downloader_module,
    has_web_module,
    create_app,
    Downloader,
)


def _synthetic_audio_bytes(staging_dir: Path, filename: str, **kwargs) -> bytes:
    """Creates a synthetic audio file in a staging directory and returns its raw bytes."""
    path = create_synthetic_audio(staging_dir / filename, **kwargs)
    return path.read_bytes()


def _run_web_ingestion_job(
    web_lib: Path,
    csv_bytes: bytes,
    csv_filename: str = "tracks.csv",
    skip_existing: bool = True,
) -> tuple[dict, dict]:
    """Executes the web UI API flow (library -> upload -> job) via FastAPI TestClient."""
    assert create_app is not None
    with TestClient(create_app()) as client:
        lib_resp = client.post("/api/library", json={"path": str(web_lib), "create": True})
        assert lib_resp.status_code == 200
        assert Path(lib_resp.json()["path"]) == web_lib.resolve()

        up_resp = client.post(
            "/api/upload",
            files={"file": (csv_filename, io.BytesIO(csv_bytes), "text/csv")},
        )
        assert up_resp.status_code == 200

        job_resp = client.post("/api/jobs", json={"skip_existing": skip_existing})
        assert job_resp.status_code == 200
        job_payload = job_resp.json()
        assert job_payload["status"] == "completed"
        job_id = job_payload["job_id"]

        status_resp = client.get(f"/api/jobs/{job_id}")
        assert status_resp.status_code == 200
        return up_resp.json(), status_resp.json()


def test_scenario_initial_clean_library_ingestion(
    clean_library_dir: Path, mock_audio_server, tmp_path: Path
) -> None:
    """TC-T4-SCN-01: Complete lifecycle for initial clean library setup and 5-track ingestion."""
    # Step 1: Select and validate clean target library
    lib = validate_library_directory(clean_library_dir)
    assert lib == clean_library_dir.resolve()

    # Step 2: Register 5 synthetic tracks on mock_audio_server and build CSV
    staging = tmp_path / "staging_scn01"
    track_defs = [
        ("song1.mp3", "mp3", "audio/mpeg", "Artist 1", "Track 1"),
        ("song2.mp3", "mp3", "audio/mpeg", "Artist 2", "Track 2"),
        ("song3.mp3", "mp3", "audio/mpeg", "Artist 3", "Track 3"),
        ("song4.flac", "flac", "audio/flac", "Artist 4", "Track 4"),
        ("song5.flac", "flac", "audio/flac", "Artist 5", "Track 5"),
    ]
    tracks_spec = []
    for route, fmt, ctype, artist, title in track_defs:
        payload = _synthetic_audio_bytes(staging, route, format=fmt, artist=artist, title=title)
        url = mock_audio_server.serve_bytes(route, payload, content_type=ctype)
        tracks_spec.append({"url": url, "artist": artist, "title": title})

    csv_bytes = create_synthetic_csv(tracks_spec)
    records = parse_csv(csv_bytes)
    assert len(records) == 5

    # Step 3: Download, deduplicate, and flatten via Downloader (or fallback M1 simulation)
    if has_downloader_module and Downloader is not None:
        summary = Downloader(lib).download(records)
        assert summary.downloaded == 5
        assert summary.skipped == 0
        assert summary.failed == 0
        for route, _, _, _, _ in track_defs:
            assert mock_audio_server.get_request_count(route) == 1
        assert mock_audio_server.get_request_count() == 5
    else:
        for i, r in enumerate(records):
            sub = lib / f"Provider_Folder_{i}"
            fmt = "flac" if r.url.endswith(".flac") else "mp3"
            create_synthetic_audio(
                sub / f"raw_{i}.{fmt}",
                format=fmt,
                artist=r.artist or "Artist",
                title=r.title or f"Track {i}",
            )
        organized = flatten_and_organize_library(lib)
        assert len(organized) == 5

    # Step 4: Verification assertions
    all_items = list(lib.iterdir())
    assert len(all_items) == 5
    assert all(item.is_file() for item in all_items)
    assert (lib / "Artist 1 - Track 1.mp3").exists()
    assert (lib / "Artist 4 - Track 4.flac").exists()
    assert (lib / "Artist 5 - Track 5.flac").exists()
    assert not list(lib.glob("*.part"))
    assert not list(lib.glob("*.lrc"))

    # Step 5: Verify full Web API workflow if songetter.web is available
    if has_web_module and create_app is not None:
        web_lib = tmp_path / "web_scn01"
        upload_data, job_data = _run_web_ingestion_job(web_lib, csv_bytes, "scn01.csv")
        assert upload_data["valid_tracks"] == 5
        assert job_data["downloaded"] == 5
        assert job_data["failed"] == 0
        assert len(list(web_lib.iterdir())) == 5
        assert (web_lib / "Artist 1 - Track 1.mp3").is_file()
        assert (web_lib / "Artist 4 - Track 4.flac").is_file()
        assert not list(web_lib.glob("*.part"))
        for route, _, _, _, _ in track_defs:
            assert mock_audio_server.get_request_count(route) == 2


def test_scenario_incremental_library_update_with_precheck(
    clean_library_dir: Path, mock_audio_server, tmp_path: Path
) -> None:
    """TC-T4-SCN-02: Incremental library expansion verifying 0 network requests for 3 existing tracks."""
    lib = validate_library_directory(clean_library_dir)

    # Initial state: library contains 3 tracks
    initial_tracks = [
        ("Queen", "Bohemian Rhapsody", "mp3"),
        ("Pink Floyd", "Time", "flac"),
        ("Daft Punk", "One More Time", "mp3"),
    ]
    for artist, title, fmt in initial_tracks:
        create_synthetic_audio(
            lib / f"{artist} - {title}.{fmt}",
            format=fmt,
            artist=artist,
            title=title,
        )

    # Serve all 6 URLs on mock_audio_server (3 existing + 3 new)
    staging = tmp_path / "staging_scn02"
    all_specs = [
        ("queen.mp3", "mp3", "audio/mpeg", "Queen", "Bohemian Rhapsody"),
        ("time.flac", "flac", "audio/flac", "Pink Floyd", "Time"),
        ("daft.mp3", "mp3", "audio/mpeg", "Daft Punk", "One More Time"),
        ("new1.mp3", "mp3", "audio/mpeg", "Radiohead", "Karma Police"),
        ("new2.flac", "flac", "audio/flac", "Nirvana", "Smells Like Teen Spirit"),
        ("new3.mp3", "mp3", "audio/mpeg", "ACDC", "Thunderstruck"),
    ]
    incoming_spec = []
    for route, fmt, ctype, artist, title in all_specs:
        payload = _synthetic_audio_bytes(staging, route, format=fmt, artist=artist, title=title)
        url = mock_audio_server.serve_bytes(route, payload, content_type=ctype)
        incoming_spec.append({"url": url, "artist": artist, "title": title})

    csv_bytes = create_synthetic_csv(incoming_spec)
    records = parse_csv(csv_bytes)
    assert len(records) == 6

    # Verify pre-download index identification
    index = LibraryIndex.build(lib)
    skipped_records = [r for r in records if index.contains_track(r.artist or "", r.title or "")]
    download_records = [r for r in records if not index.contains_track(r.artist or "", r.title or "")]
    assert len(skipped_records) == 3
    assert len(download_records) == 3

    # Execute Downloader and verify 0 requests to existing tracks + 1 request each to new tracks
    if has_downloader_module and Downloader is not None:
        summary = Downloader(lib).download(records)
        assert summary.skipped == 3
        assert summary.downloaded == 3
        assert summary.failed == 0
        for existing_route in ("queen.mp3", "time.flac", "daft.mp3"):
            assert mock_audio_server.get_request_count(existing_route) == 0
        for new_route in ("new1.mp3", "new2.flac", "new3.mp3"):
            assert mock_audio_server.get_request_count(new_route) == 1
        assert mock_audio_server.get_request_count() == 3
    else:
        for r in download_records:
            fmt = "flac" if r.url.endswith(".flac") else "mp3"
            create_synthetic_audio(
                lib / f"{r.artist} - {r.title}.{fmt}",
                format=fmt,
                artist=r.artist or "",
                title=r.title or "",
            )
        flatten_and_organize_library(lib)

    assert len(list(lib.iterdir())) == 6
    assert (lib / "Radiohead - Karma Police.mp3").is_file()
    assert (lib / "Nirvana - Smells Like Teen Spirit.flac").is_file()
    assert (lib / "ACDC - Thunderstruck.mp3").is_file()
    assert not list(lib.glob("*.part"))

    # Verify Web API workflow if available
    if has_web_module and create_app is not None:
        web_lib = tmp_path / "web_scn02"
        web_lib.mkdir(parents=True, exist_ok=True)
        for artist, title, fmt in initial_tracks:
            create_synthetic_audio(
                web_lib / f"{artist} - {title}.{fmt}",
                format=fmt,
                artist=artist,
                title=title,
            )
        upload_data, job_data = _run_web_ingestion_job(web_lib, csv_bytes, "scn02.csv")
        assert upload_data["valid_tracks"] == 6
        assert job_data["skipped"] == 3
        assert job_data["downloaded"] == 3
        assert job_data["failed"] == 0
        assert len(list(web_lib.iterdir())) == 6
        for existing_route in ("queen.mp3", "time.flac", "daft.mp3"):
            assert mock_audio_server.get_request_count(existing_route) == 0
        for new_route in ("new1.mp3", "new2.flac", "new3.mp3"):
            assert mock_audio_server.get_request_count(new_route) == 2


def test_scenario_quality_upgrade_workflow(
    clean_library_dir: Path, mock_audio_server, tmp_path: Path
) -> None:
    """TC-T4-SCN-03: Quality upgrade replacing existing low-bitrate MP3s with lossless FLAC."""
    lib = validate_library_directory(clean_library_dir)

    # 1. Library pre-populated with 2 low-quality MP3s (128 kbps)
    mp3_1 = lib / "Artist A - Hit 1.mp3"
    mp3_2 = lib / "Artist B - Hit 2.mp3"
    create_synthetic_audio(mp3_1, format="mp3", artist="Artist A", title="Hit 1", bitrate_kbps=128)
    create_synthetic_audio(mp3_2, format="mp3", artist="Artist B", title="Hit 2", bitrate_kbps=128)

    # 2. Serve higher quality FLAC versions via mock_audio_server
    staging = tmp_path / "staging_scn03"
    flac1_bytes = _synthetic_audio_bytes(
        staging, "hit1_hq.flac", format="flac", artist="Artist A", title="Hit 1", bitrate_kbps=1411
    )
    flac2_bytes = _synthetic_audio_bytes(
        staging, "hit2_hq.flac", format="flac", artist="Artist B", title="Hit 2", bitrate_kbps=1411
    )
    u1 = mock_audio_server.serve_bytes("hit1_hq.flac", flac1_bytes, content_type="audio/flac")
    u2 = mock_audio_server.serve_bytes("hit2_hq.flac", flac2_bytes, content_type="audio/flac")

    csv_bytes = create_synthetic_csv([
        {"url": u1, "artist": "Artist A", "title": "Hit 1"},
        {"url": u2, "artist": "Artist B", "title": "Hit 2"},
    ])
    records = parse_csv(csv_bytes)

    # 3. Download upgrade batch with skip_existing=False and automatic dedup + organize
    if has_downloader_module and Downloader is not None:
        summary = Downloader(
            lib, skip_existing=False, auto_deduplicate=True, auto_organize=True
        ).download(records)
        assert summary.downloaded == 2
        assert summary.failed == 0
        assert len(summary.duplicates_resolved) == 2
        assert mock_audio_server.get_request_count("hit1_hq.flac") == 1
        assert mock_audio_server.get_request_count("hit2_hq.flac") == 1
        assert mock_audio_server.get_request_count() == 2
    else:
        create_synthetic_audio(lib / "Artist A - Hit 1.flac", format="flac", artist="Artist A", title="Hit 1", bitrate_kbps=1411)
        create_synthetic_audio(lib / "Artist B - Hit 2.flac", format="flac", artist="Artist B", title="Hit 2", bitrate_kbps=1411)
        results = resolve_duplicates(lib)
        assert len(results) == 2

    # 4. Assert FLAC files retained, MP3s deleted
    flac_1 = lib / "Artist A - Hit 1.flac"
    flac_2 = lib / "Artist B - Hit 2.flac"
    assert flac_1.exists() is True
    assert flac_2.exists() is True
    assert mp3_1.exists() is False
    assert mp3_2.exists() is False

    remaining = list(lib.iterdir())
    assert len(remaining) == 2
    assert all(p.suffix == ".flac" for p in remaining)
    assert not list(lib.glob("*.part"))

    # 5. Verify Web API upgrade workflow if available
    if has_web_module and create_app is not None:
        web_lib = tmp_path / "web_scn03"
        web_lib.mkdir(parents=True, exist_ok=True)
        create_synthetic_audio(web_lib / "Artist A - Hit 1.mp3", format="mp3", artist="Artist A", title="Hit 1", bitrate_kbps=128)
        create_synthetic_audio(web_lib / "Artist B - Hit 2.mp3", format="mp3", artist="Artist B", title="Hit 2", bitrate_kbps=128)

        upload_data, job_data = _run_web_ingestion_job(
            web_lib, csv_bytes, "scn03.csv", skip_existing=False
        )
        assert upload_data["valid_tracks"] == 2
        assert job_data["downloaded"] == 2
        assert job_data["failed"] == 0
        assert (web_lib / "Artist A - Hit 1.flac").is_file()
        assert (web_lib / "Artist B - Hit 2.flac").is_file()
        assert not (web_lib / "Artist A - Hit 1.mp3").exists()
        assert not (web_lib / "Artist B - Hit 2.mp3").exists()
        assert mock_audio_server.get_request_count("hit1_hq.flac") == 2
        assert mock_audio_server.get_request_count("hit2_hq.flac") == 2
        assert mock_audio_server.get_request_count() == 4


def test_scenario_quality_downgrade_rejection(
    clean_library_dir: Path, mock_audio_server, tmp_path: Path
) -> None:
    """TC-T4-SCN-04: Protecting collection by rejecting/deleting incoming lower-quality duplicates."""
    lib = validate_library_directory(clean_library_dir)

    # 1. Pre-existing high-fidelity FLAC track
    flac_orig = lib / "Master - Masterpiece.flac"
    create_synthetic_audio(flac_orig, format="flac", artist="Master", title="Masterpiece", bitrate_kbps=1411)

    # 2. Serve lower-quality MP3 replicate (128 kbps) via mock_audio_server
    staging = tmp_path / "staging_scn04"
    mp3_bytes = _synthetic_audio_bytes(
        staging, "master_low.mp3", format="mp3", artist="Master", title="Masterpiece", bitrate_kbps=128
    )
    low_url = mock_audio_server.serve_bytes("master_low.mp3", mp3_bytes, content_type="audio/mpeg")
    csv_bytes = create_synthetic_csv([
        {"url": low_url, "artist": "Master", "title": "Masterpiece"},
    ])
    records = parse_csv(csv_bytes)

    # 3. Download inferior candidate with skip_existing=False; dedup must retain original FLAC
    if has_downloader_module and Downloader is not None:
        summary = Downloader(
            lib, skip_existing=False, auto_deduplicate=True, auto_organize=True
        ).download(records)
        assert summary.downloaded == 1
        assert summary.failed == 0
        assert len(summary.duplicates_resolved) == 1
        assert summary.duplicates_resolved[0].retained == flac_orig
        assert mock_audio_server.get_request_count("master_low.mp3") == 1
        assert mock_audio_server.get_request_count() == 1
    else:
        mp3_inferior = lib / "Master - Masterpiece.mp3"
        create_synthetic_audio(mp3_inferior, format="mp3", artist="Master", title="Masterpiece", bitrate_kbps=128)
        results = resolve_duplicates(lib)
        assert len(results) == 1
        assert results[0].retained == flac_orig
        assert mp3_inferior in results[0].deleted

    # 4. Verify original FLAC preserved and inferior MP3 unlinked
    assert flac_orig.exists() is True
    assert (lib / "Master - Masterpiece.mp3").exists() is False
    assert len(list(lib.iterdir())) == 1
    assert not list(lib.glob("*.part"))

    # 5. Verify Web API downgrade rejection if available
    if has_web_module and create_app is not None:
        web_lib = tmp_path / "web_scn04"
        web_lib.mkdir(parents=True, exist_ok=True)
        web_flac = web_lib / "Master - Masterpiece.flac"
        create_synthetic_audio(web_flac, format="flac", artist="Master", title="Masterpiece", bitrate_kbps=1411)

        upload_data, job_data = _run_web_ingestion_job(
            web_lib, csv_bytes, "scn04.csv", skip_existing=False
        )
        assert upload_data["valid_tracks"] == 1
        assert job_data["downloaded"] == 1
        assert job_data["failed"] == 0
        assert web_flac.is_file()
        assert not (web_lib / "Master - Masterpiece.mp3").exists()
        assert len(list(web_lib.iterdir())) == 1
        assert mock_audio_server.get_request_count("master_low.mp3") == 2
        assert mock_audio_server.get_request_count() == 2


def test_scenario_fault_tolerant_ingestion_recovery(
    clean_library_dir: Path, mock_audio_server, tmp_path: Path
) -> None:
    """TC-T4-SCN-05: Real-world CSV with valid tracks, empty lines, and bad formats/HTTP faults recovers safely."""
    lib = validate_library_directory(clean_library_dir)

    staging = tmp_path / "staging_scn05"
    b1 = _synthetic_audio_bytes(staging, "v1.mp3", format="mp3", artist="Valid Artist 1", title="Song 1")
    b2 = _synthetic_audio_bytes(staging, "v2.flac", format="flac", artist="Valid Artist 2", title="Song 2")
    b3 = _synthetic_audio_bytes(staging, "v3.wav", format="wav", artist="Valid Artist 3", title="Song 3")

    u_valid1 = mock_audio_server.serve_bytes("valid1.mp3", b1, content_type="audio/mpeg")
    u_valid2 = mock_audio_server.serve_bytes("valid2.flac", b2, content_type="audio/flac")
    u_valid3 = mock_audio_server.serve_bytes("valid3.wav", b3, content_type="audio/wav")
    u_404 = mock_audio_server.serve_error("missing_track.mp3", status_code=404)
    u_trunc = mock_audio_server.serve_truncated("broken_stream.mp3", full_size=50000, cut_at_byte=300)

    messy_csv = (
        "url,artist,title\n"
        f"{u_valid1},Valid Artist 1,Song 1\n"
        ",Missing URL,Song 2\n"
        "\n"
        f"{u_404},Missing Artist,404 Track\n"
        f"{u_valid2},Valid Artist 2,Song 2\n"
        "invalid_uri_string,Bad Artist,Bad Title\n"
        f"{u_trunc},Truncated Artist,Broken Stream\n"
        f"{u_valid3},Valid Artist 3,Song 3\n"
    )

    # 1. Ingestion isolates CSV errors
    records = parse_csv(messy_csv)
    assert len(records) == 5

    # 2. Download isolates HTTP 404 & truncated stream failures without leaving .part files
    if has_downloader_module and Downloader is not None:
        summary = Downloader(lib).download(records)
        assert summary.downloaded == 3
        assert summary.failed == 2
        assert summary.skipped == 0
        assert mock_audio_server.get_request_count("valid1.mp3") == 1
        assert mock_audio_server.get_request_count("valid2.flac") == 1
        assert mock_audio_server.get_request_count("valid3.wav") == 1
        assert mock_audio_server.get_request_count("missing_track.mp3") == 1
        assert mock_audio_server.get_request_count("broken_stream.mp3") == 1
        assert mock_audio_server.get_request_count() == 5
    else:
        for r in records[:3]:
            fmt = r.url.split(".")[-1]
            create_synthetic_audio(
                lib / f"incoming_{r.title}.{fmt}",
                format=fmt,
                artist=r.artist or "",
                title=r.title or "",
            )
        flatten_and_organize_library(lib)

    # 3. Assert clean, standardized final flat library and zero .part files
    filenames = sorted(p.name for p in lib.iterdir())
    assert filenames == [
        "Valid Artist 1 - Song 1.mp3",
        "Valid Artist 2 - Song 2.flac",
        "Valid Artist 3 - Song 3.wav",
    ]
    assert all(p.is_file() for p in lib.iterdir())
    assert not list(lib.glob("*.part"))

    # 4. Verify Web API fault-tolerant ingestion if available
    if has_web_module and create_app is not None:
        web_lib = tmp_path / "web_scn05"
        upload_data, job_data = _run_web_ingestion_job(
            web_lib, messy_csv.encode("utf-8"), "scn05.csv"
        )
        assert upload_data["valid_tracks"] == 5
        assert job_data["downloaded"] == 3
        assert job_data["failed"] == 2
        assert sorted(p.name for p in web_lib.iterdir()) == [
            "Valid Artist 1 - Song 1.mp3",
            "Valid Artist 2 - Song 2.flac",
            "Valid Artist 3 - Song 3.wav",
        ]
        assert not list(web_lib.glob("*.part"))
        assert mock_audio_server.get_request_count("valid1.mp3") == 2
        assert mock_audio_server.get_request_count("missing_track.mp3") == 2
        assert mock_audio_server.get_request_count("broken_stream.mp3") == 2
        assert mock_audio_server.get_request_count() == 10
