# AGENTS.md

Guidelines for AI agents and contributors working on **Songetter**.

## Project Overview

Songetter is a Python 3.11+ music library downloader, search orchestrator, and organizer built around [Mornye-Python](https://github.com/mornyeapp/Mornye-Python), `mutagen`, `httpx`, FastAPI (`src/songetter/web/`), Textual (`src/songetter/tui/`), Streamlit (`app.py`), and a Hybrid Chaquopy Android application (`android/` + `src/songetter/android_bridge.py`).

See `NEXT_STEPS.md` for the milestone history and verification status.

## Environment & Tooling

- **Python**: `>=3.11` (local dev environment uses `.venv/`).
- **Install editable with dev/test dependencies**:
  ```bash
  pip install -e ".[dev]"
  ```
  Or run `./scripts/install_and_run.sh` / `make install`.
- **System binaries required on `PATH` for full desktop/server Mornye media runs**:
  - `ffmpeg` / `ffprobe` (audio transcoding, probing, and validation)
  - `node` (Node.js runtime for Mornye JavaScript provider extensions; **not** required on Android via `android_bridge.py`)
- **Run tests**:
  ```bash
  pytest -q
  ```
  Do not require live network calls in unit or E2E tests; use `tests/e2e/conftest.py` (`MockAudioServer`, `create_synthetic_audio`, `create_synthetic_csv`) and `httpx.MockTransport` for offline deterministic testing.

## Architecture & Module Map

- `src/songetter/config.py` — Shared constants (`AUDIO_SUFFIXES`, `MAX_FILENAME_BYTES`, `PROBE_PREFIX`, CSV column aliases, delimiters, `SUPPORTED_SEARCH_SOURCES`, `SUPPORTED_SOURCE_HOSTS`).
- `src/songetter/csv_processor.py` — RFC 4180 CSV/text parsing, delimiter sniffing (`,`, `;`, `\t`, `|`), BOM/encoding handling, header alias mapping, headerless URL fallback, and `TrackRecord` creation.
- `src/songetter/library.py` — Server library path validation (`validate_library_directory` with write-probe verification) and `LibraryIndex` (dual stem + metadata index).
- `src/songetter/deduplicator.py` — Mutagen metadata extraction (`extract_metadata`), Unicode normalization (`normalize_text`), quality comparison (`compare_quality`: bitrate -> file size -> deterministic tie-breaker), and `resolve_duplicates`.
- `src/songetter/organizer.py` — Flat directory flattening (`flatten_and_organize_library`), `{Artist} - {Title}.{ext}` standardization (`format_standard_filename`), forbidden-character sanitization, UTF-8 byte-length bounding (<=240 bytes), collision suffixing, and empty folder pruning.
- `src/songetter/core.py` — Mornye subprocess execution, registry configuration, provider circuit-breaker cooldowns, link normalization (`parse_links`, `parse_source_links`), and `.m3u8` playlist generation.
- `src/songetter/downloader.py` — Multi-protocol `Downloader` and `download_tracks` with batch URL deduplication, pre-download `LibraryIndex` skip check, atomic `.part` staging, `yt-dlp` fallback, and post-download dedup + flat organization.
- `src/songetter/search.py` — Federated multi-source search providers (`Deezer`, `TIDAL`, `iTunes`, `MusicBrainz`, `Qobuz`, `Spotify`), `search_tracks_with_errors`, `search_tracks`, and `search_results_to_csv`.
- `src/songetter/web/` — FastAPI + Jinja2 server-side web UI and REST API (`create_app()`, `/api/library`, `/api/library/tracks/{filename}/stream`, `/api/upload`, `/api/jobs`, `/api/organize`, `/api/search`).
- `src/songetter/tui/` — Interactive Textual terminal UI (`SongetterTUI`, `run_tui()`).
- `src/songetter/cli.py` — Unified CLI entry point (`songetter`, `songetter-tui`, `songetter-web`) with subcommands (`tui`, `serve`/`web`, `download`, `organize`/`dedup`, `search`, `doctor`) and legacy flag compatibility.
- `src/songetter/android_bridge.py` — Pure-Python Chaquopy bridge (`resolve_spotify_url_pure_python`, `fetch_lyrics_lrclib`, `embed_lyrics_in_audio`, `embed_basic_tags`, `run_on_device_job`, `run_remote_server_job`, and crash-safe JSON facades `bridge_*`).
- `android/` — Native Gradle + Kotlin + Jetpack Compose Material 3 + Chaquopy Python 3.11 Android project bundling `src/songetter/`.
- `Dockerfile` & `Dockerfile.android` — Container builds for the server runtime and the Android debug APK (`./gradlew assembleDebug`).
- `app.py` — Legacy Streamlit web UI.

## Non-Negotiable Engineering Rules

1. **Metadata-Only Lyrics (No `.lrc` Sidecars)**:
   - Lyrics must be stored **only** inside audio file metadata tags (e.g., Vorbis `lyrics` comment for FLAC/OGG/OPUS, ID3 `USLT` for MP3/WAV, MP4 `\xa9lyr` for M4A).
   - Do **not** pass `--save-lrc` to `mornye`.
   - Do **not** generate, move, rename, or delete `.lrc` sidecar files in `organizer.py`, `deduplicator.py`, `downloader.py`, or `android_bridge.py`.
2. **Flat Library Organization**:
   - The target music library is organized as a **flat folder** at the selected library root using `{Artist} - {Title}.{ext}`.
   - Nested directories created by downloaders must be flattened and empty directories pruned bottom-up.
3. **Deduplication & Quality Retention**:
   - Match duplicates by normalized `(artist, title)` metadata.
   - Retain the copy with higher audio bitrate (`bps`), breaking ties by larger file size (`bytes`). If both metrics are equal, retain one copy and delete the other deterministically.
4. **Mutagen Tag Safety**:
   - Always guard raw `tags.get(key)` and tag deletion/assignment calls against `ValueError`, `KeyError`, and `TypeError` (e.g., Mutagen `VComment` raises `ValueError` for non-ASCII keys like `"\xa9ART"`).
5. **Android Pure-Python Compatibility**:
   - `src/songetter/android_bridge.py` and its imported `src/songetter/` domain modules must run inside Chaquopy Python 3.11 without requiring `mornye`, `node`, `ffmpeg`, `fastapi`, `streamlit`, or `textual` at import time.
6. **Repository & Container Hygiene**:
   - Never commit or bake `downloads/`, `Music/`, `.venv/`, `.pytest_cache/`, or `.agents/` into Git or Docker images (enforced via `.gitignore` and `.dockerignore`).
   - Do not hardcode machine-specific absolute paths like `/home/<user>/...` in tests or source code; resolve relative to `Path(__file__)` or configuration/environment variables (`SONGETTER_LIBRARY_DIR`).
