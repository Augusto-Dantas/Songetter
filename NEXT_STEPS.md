# Next Steps — Continuing the Server-Side CSV Library Job

This document captures the current state of the Songetter server-side library application refactor and provides concrete next steps to complete the remaining milestones.

---

## 1. Current State (Completed So Far)

### Milestone 1 — Core Domain Modules (Implemented & Verified)
The following core modules are implemented in `src/songetter/` and covered by offline unit/adversarial tests (`185 passed, 7 skipped` awaiting M2/M3):

- **`src/songetter/config.py`**: Supported `AUDIO_SUFFIXES`, `MAX_FILENAME_BYTES` (240), forbidden character regex, CSV delimiters, and header aliases (`url`, `artist`, `title`).
- **`src/songetter/csv_processor.py`**: `parse_csv()` supporting RFC 4180 multiline fields, delimiter sniffing (`,`, `;`, `\t`, `|`), UTF-8-sig BOM stripping, latin-1 fallback, header alias mapping, headerless URL extraction, and typed exceptions (`EmptyCSVError`, `MalformedCSVError`, `NoValidUrlsError`).
- **`src/songetter/library.py`**: `validate_library_directory()` (path validation, directory check, genuine write-probe verification) and `LibraryIndex` (stem + metadata dual lookup).
- **`src/songetter/deduplicator.py`**: `extract_metadata()`, `normalize_text()`, `compare_quality()` (bitrate -> file size -> deterministic tie), and `resolve_duplicates()`. Fixed Vorbis `VComment` `ValueError` on untagged FLAC/OGG files.
- **`src/songetter/organizer.py`**: `sanitize_filename_component()`, `format_standard_filename()` (`{Artist} - {Title}.{ext}`), and `flatten_and_organize_library()` (flattens nested trees, handles filename collisions, prunes empty directories bottom-up).

### Policy, Containerization & Repo Updates Applied
- **Lyrics Policy Updated**: Removed all `.lrc` sidecar generation, moving, and deletion (`--save-lrc` removed from `src/songetter/core.py`; `.lrc` logic removed from `deduplicator.py`, `organizer.py`, and tests). Lyrics are kept strictly in audio metadata tags (`--lyrics-providers` remains active).
- **Containerization**: Added `Dockerfile` and `.dockerignore` (with `downloads/` excluded from both Git and Docker builds).
- **Agent Guidelines**: Added `AGENTS.md` documenting architecture, invariants, and test commands.

### Test Suites Currently in Repository
- `tests/test_core.py`: Mornye command construction, cooldown circuit breaker, `.m3u8` export.
- `tests/test_domain_modules.py`: Unit tests for config, CSV parser, library index, deduplicator, organizer.
- `tests/test_adversarial_m1.py` & `tests/test_empirical_adversarial.py`: Stress & boundary tests using synthetic audio fixtures.
- `tests/test_milestone1_empirical.py`: Local empirical album check (automatically skipped when `downloads/Graduation` is absent).
- `tests/e2e/` (`conftest.py`, `test_tier1_features.py`–`test_tier4_scenarios.py`, `test_e2e_suite.py`): 60 requirement-mapped E2E tests across Tiers 1–4. Tests depending on M2 (`songetter.downloader`) and M3 (`songetter.web`) are currently skipped via `has_downloader_module` / `has_web_module`.

---

## 2. Immediate Next Steps

### Step 1: Finish E2E Test Suite Remediation (Iteration 2 Fixes)
Before enabling M2/M3 integration assertions, address the 4 test-quality findings recorded during E2E Gate 1 (`.agents/teamwork/e2e_testing_orchestrator/GATE_STATUS.md`):
1. `tests/e2e/test_tier2_boundaries.py::test_boundary_pre_download_duplicate_csv_rows` — Assert application-level batch URL deduplication instead of an in-test `set()`.
2. `tests/e2e/test_tier1_features.py::test_download_atomic_staging_part_file` — Assert the final promoted audio file exists, is non-empty, and matches the served payload.
3. `tests/e2e/test_tier2_boundaries.py::test_boundary_pre_download_url_encoded_stems` — Test application URL stem decoding/matching directly instead of manual `urllib.parse.unquote` in the test.
4. `tests/e2e/test_tier4_scenarios.py` — Register routes on `mock_audio_server`, assert `mock_audio_server.get_request_count()`, and wire progressive `has_downloader_module` / `has_web_module` hooks across all 5 Tier 4 scenarios.

---

### Step 2: Milestone 2 — Download Pipeline & Pre-Check (`src/songetter/downloader.py`)
Implement `src/songetter/downloader.py` exposing `Downloader` and `download_tracks` (matching `tests/e2e/conftest.py` imports):

1. **Pre-Download Skip Check**:
   - Build `LibraryIndex.build(library_dir)` before initiating network transfers.
   - Deduplicate incoming `TrackRecord` entries by URL within the batch.
   - Check `LibraryIndex.contains_track(artist, title)` and `contains_stem(...)` before downloading; skip existing tracks without making HTTP or Mornye requests.
2. **Multi-Protocol Download Execution**:
   - Direct HTTP/HTTPS audio URLs: stream asynchronously (e.g., `httpx` / `aiohttp`) into a staging `.part` file and atomically promote (`os.replace`) upon completion; clean up `.part` on failure/timeout.
   - Spotify URLs (`open.spotify.com` / `spotify.link`): delegate to `songetter.core.run_download(...)` (metadata-only lyrics, no `--save-lrc`).
   - Fallback media URLs: invoke `yt-dlp` when direct HTTP is not a raw audio stream.
   - Enforce bounded concurrency (`max_concurrent`, default 2–4).
3. **Post-Download Pipeline**:
   - Run `resolve_duplicates(library_dir)` to keep the highest-quality copy (highest bitrate, then largest file size; deterministic tie-breaker if identical).
   - Run `flatten_and_organize_library(library_dir)` to enforce flat `{Artist} - {Title}.{ext}` naming and prune empty folders.
4. **Verification**:
   - Add `tests/test_downloader.py` covering pre-download skip (0 HTTP hits), atomic `.part` staging, error recovery, and post-download dedup + flattening.

---

### Step 3: Milestone 3 — Server-Side Web UI & REST API (`src/songetter/web/`)
Build the FastAPI + Jinja2 server application matching `create_app()` in `tests/e2e/conftest.py`:

1. **`src/songetter/web/app.py`**:
   - Export `create_app() -> FastAPI` factory with lifespan/state initialization (defaulting library path from `SONGETTER_LIBRARY_DIR` env var if set).
2. **`src/songetter/web/routes.py`**:
   - `GET /`: Server-side HTML interface (Jinja2) to select/validate a local server directory path as the music library, upload `.csv` files manually, and monitor job progress & errors.
   - Library selection endpoint (e.g. `POST /api/library`): validates directory via `validate_library_directory()` and returns structured `400`/`403` errors for nonexistent, file, or read-only paths.
   - CSV upload & job trigger endpoint (e.g. `POST /api/upload` / `POST /api/jobs`): parses CSV via `parse_csv()`, returns `400`/`422` on `EmptyCSVError` / `MalformedCSVError` / `NoValidUrlsError`, and dispatches background download + dedup + organization job.
   - Job status & library listing endpoints (e.g. `GET /api/jobs/{job_id}`, `GET /api/library/tracks`): report per-track status (`downloaded`, `skipped`, `deduplicated`, `failed`), error details, and flat library contents.
3. **CLI & Container Entrypoint Update**:
   - Add `serve` subcommand to `src/songetter/cli.py` (e.g., `uvicorn songetter.web.app:create_app --factory --host 0.0.0.0 --port 8000`).
   - Update `Dockerfile` `CMD` from Streamlit (`app.py` on port `8501`) to the FastAPI server on port `8000`.
4. **Verification**:
   - Add `tests/test_web_api.py` using `fastapi.testclient.TestClient` and `MockAudioServer`.

---

### Step 4: Milestone 4 — Full E2E & Container Verification
1. Run `pytest` and verify **0 skipped / 100% passing** across unit tests, adversarial suites, and all 60 E2E tests in `tests/e2e/`.
2. Build and run the container against a mounted library directory (ensuring `downloads/` stays excluded from the image):
   ```bash
   docker build -t songetter:latest .
   docker run --rm -p 8501:8501 -p 8000:8000 -v "$(pwd)/my_music_library:/library" songetter:latest
   ```
3. Perform manual E2E verification via browser UI: select `/library`, upload a `.csv` with existing + new + duplicate-quality tracks, confirm existing tracks are skipped, superior bitrate/size copy is retained, metadata lyrics are embedded (zero `.lrc` files), and filenames follow `{Artist} - {Title}.{ext}` in a flat folder.
