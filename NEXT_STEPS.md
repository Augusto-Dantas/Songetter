# Next Steps & Milestone Status

This document tracks the completed milestones for Songetter across the core domain pipeline, server-side FastAPI web application, unified CLI & Textual TUI, and Hybrid Chaquopy Android APK.

---

## Completed Milestones

### Milestone 1 — Core Domain Modules (`src/songetter/`)
- **`src/songetter/config.py`**: Supported `AUDIO_SUFFIXES`, `MAX_FILENAME_BYTES` (240), forbidden character regex, CSV delimiters, header aliases, `SUPPORTED_SEARCH_SOURCES`, and `SUPPORTED_SOURCE_HOSTS`.
- **`src/songetter/csv_processor.py`**: `parse_csv()` supporting RFC 4180 multiline fields, delimiter sniffing (`,`, `;`, `\t`, `|`), UTF-8-sig BOM stripping, latin-1 fallback, header alias mapping, headerless URL extraction, and typed exceptions (`EmptyCSVError`, `MalformedCSVError`, `NoValidUrlsError`).
- **`src/songetter/library.py`**: `validate_library_directory()` (path validation, directory check, write-probe verification) and `LibraryIndex` (dual stem + metadata lookup, percent-decoded URL stem matching).
- **`src/songetter/deduplicator.py`**: `extract_metadata()`, `normalize_text()`, `compare_quality()` (bitrate -> file size -> deterministic tie-breaker), and `resolve_duplicates()`.
- **`src/songetter/organizer.py`**: `sanitize_filename_component()`, `format_standard_filename()` (`{Artist} - {Title}.{ext}`), and `flatten_and_organize_library()` (flattens nested trees, handles collisions, prunes empty directories bottom-up).

### Milestone 2 — Multi-Protocol Download Pipeline & Federated Search
- **`src/songetter/downloader.py`**: `Downloader` and `download_tracks` providing batch URL deduplication (`deduplicate_records`), pre-download `LibraryIndex` skip checks, Mornye delegation, direct HTTP `.part` atomic staging with `Content-Length` checks, optional `yt-dlp` fallback, and post-download dedup + flattening.
- **`src/songetter/search.py`**: Federated search providers (`DeezerSearchProvider`, `TidalSearchProvider`, `ItunesSearchProvider`, `MusicBrainzSearchProvider`, `QobuzSearchProvider`, `SpotifySearchProvider`), `search_tracks_with_errors`, `search_tracks`, and `search_results_to_csv`.

### Milestone 3 — FastAPI Web UI & REST API (`src/songetter/web/`)
- **`src/songetter/web/app.py` & `templates/index.html`**: Server-side Jinja2 dashboard and REST endpoints (`/api/library`, `/api/library/tracks`, `/api/library/tracks/{filename}/stream`, `/api/upload`, `/api/jobs`, `/api/organize`, `/api/search*`).
- **E2E Suite (`tests/e2e/`)**: 60 requirement-mapped E2E tests across Tiers 1–4.

### Milestone 4 — Unified CLI & Textual TUI (`src/songetter/cli.py`, `src/songetter/tui/`)
- **`src/songetter/tui/app.py`**: 4-tab `SongetterTUI` (`Download & Ingest`, `Search`, `Library & Organizer`, `Providers & Doctor`) with keyboard shortcuts and live progress/metrics.
- **`src/songetter/cli.py`**: Unified subcommands (`tui`, `serve`/`web`, `download`, `organize`/`dedup`, `search`, `doctor`), entrypoints (`songetter`, `songetter-tui`, `songetter-web`), and backward-compatible legacy flag handling.
- **Local Developer Runner**: `scripts/install_and_run.sh` and `Makefile`.

### Milestone 5 — Hybrid Android APK (`android/` + `src/songetter/android_bridge.py`)
- **`src/songetter/android_bridge.py`**:
  - `get_default_android_library_dir()` resolving `SONGETTER_LIBRARY_DIR`, `/storage/emulated/0/Music/Songetter`, `ANDROID_STORAGE`/`EXTERNAL_STORAGE`, or home fallback.
  - `resolve_spotify_url_pure_python()` parsing Spotify embed HTML / oEmbed JSON without Node.js.
  - `fetch_lyrics_lrclib()`, `embed_lyrics_in_audio()`, and `embed_basic_tags()` storing lyrics strictly inside Mutagen audio tags (`ID3 USLT`, `Vorbis lyrics`, `MP4 \xa9lyr`) with zero `.lrc` sidecar files.
  - `run_on_device_job()` and `run_remote_server_job()` supporting both standalone Android execution and remote FastAPI library syncing.
  - Crash-safe Chaquopy JSON facades (`bridge_get_status`, `bridge_list_library`, `bridge_start_job`, `bridge_search`, `bridge_organize_library`).
- **`android/`**: Native Gradle + Kotlin + Jetpack Compose Material 3 + Chaquopy 15.0.1 (Python 3.11) project.
- **Automation**: `Dockerfile.android` (OpenJDK 17 + Android SDK 34 + NDK + Python 3.11) and `.github/workflows/android-apk.yml`.
- **Tests**: `tests/test_android_bridge.py`.

---

## Ongoing Maintenance & Operational Checks

1. Keep the metadata-only lyrics invariant intact (`--save-lrc` and `.lrc` sidecars remain prohibited across all modules).
2. Run the full offline test suite before releases:
   ```bash
   pytest -q
   ```
3. Build and validate the Android APK via CI or `Dockerfile.android`:
   ```bash
   docker build -f Dockerfile.android -t songetter-android:latest .
   ```
