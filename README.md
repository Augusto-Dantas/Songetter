# Songetter

Songetter is a Python 3.11+ music library downloader, federated metadata search tool, and flat library organizer powered by [Mornye-Python](https://github.com/mornyeapp/Mornye-Python), `mutagen`, `httpx`, FastAPI, Textual, Streamlit, and a Hybrid Android APK built with Kotlin + Jetpack Compose + Chaquopy.

## Features

- **Unified CLI, Textual TUI, and FastAPI Web Dashboard**: Launch a terminal UI, a server-side web UI + REST API, or scriptable CLI commands from one package.
- **Federated Metadata Search**: Query `deezer`, `itunes` (`apple`), `musicbrainz`, and authenticated catalogs (`tidal`, `qobuz`, `spotify`) concurrently with ISRC/metadata deduplication and CSV export.
- **Pre-Download Skip Indexing**: Builds a dual stem + metadata `LibraryIndex` before transferring files so existing tracks are skipped with zero unnecessary network calls.
- **Atomic Staging & Quality Deduplication**: Streams direct HTTP audio to `.part` files promoted via `os.replace`, retains the highest bitrate (`bps`) then largest file size (`bytes`), and flattens nested folders into `{Artist} - {Title}.{ext}`.
- **Metadata-Only Lyrics**: Embeds lyrics strictly inside audio tags (ID3 `USLT`, Vorbis `lyrics`, MP4 `\xa9lyr`) and never creates `.lrc` sidecar files.
- **Hybrid Android APK (`android/` + `songetter.android_bridge`)**:
  - **On-Device Mode**: Pure-Python Spotify embed/oEmbed resolution (no Node.js required), optional `yt-dlp` module stream resolution, LRCLIB tag-only lyrics embedding, and local deduplication/flattening.
  - **Remote Sync Mode**: Pre-checks the local Android library, dispatches only missing tracks to a remote Songetter FastAPI server (`POST /api/jobs`), streams new tracks from `/api/library/tracks/{filename}/stream`, and organizes the local flat folder.

## Requirements

- Python `>=3.11`
- Core dependencies installed by `pip install -e .` (`mornye>=2.0.6`, `mutagen>=1.47`, `httpx>=0.27`, `fastapi>=0.115`, `uvicorn>=0.32`, `jinja2>=3.1`, `python-multipart>=0.0.12`, `textual>=0.80`, `streamlit>=1.40`)
- For full Mornye desktop/server runs: `ffmpeg` / `ffprobe` and Node.js (`node`) on `PATH`

## Installation & Quick Start

### One-Command Local Script

```bash
./scripts/install_and_run.sh tui        # Launch interactive Textual TUI (default)
./scripts/install_and_run.sh serve      # Start FastAPI web UI & REST API (port 8000)
./scripts/install_and_run.sh doctor     # Check binaries, packages, and library writability
./scripts/install_and_run.sh test       # Run pytest test suite
```

### Manual Installation & Makefile Targets

```bash
git clone https://github.com/Augusto-Dantas/Songetter.git
cd Songetter
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Available `Makefile` targets:
```bash
make install       # Create .venv and install editable package + dev deps
make tui           # Launch Textual TUI
make serve         # Start FastAPI server (HOST=0.0.0.0 PORT=8000 LIBRARY_DIR=./Music)
make streamlit     # Start legacy Streamlit UI (app.py)
make doctor        # Run environment diagnostics
make test          # Run pytest -q
make docker-build  # Build server Docker image
make android-apk   # Build Android APK locally or via Dockerfile.android
```

## Local Usage (CLI, TUI, Web)

### Unified CLI

Running `songetter` with no arguments in an interactive terminal launches the Textual TUI (equivalent to `songetter tui` or `songetter-tui`). Explicit subcommands include:

```bash
# Interactive Textual TUI
songetter tui -o ./Music
songetter-tui

# FastAPI Web UI & REST API
songetter serve --host 0.0.0.0 --port 8000 -o ./Music
songetter-web

# Batch download from Spotify/streaming links, direct HTTP audio URLs, or CSV
songetter download https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv -o ./Music
songetter download -i tracks.csv -o ./Music --max-concurrent 4

# Federated metadata search (with optional CSV export or download)
songetter search "Daft Punk One More Time" --source deezer,itunes --limit 10
songetter search "Queen" --export-csv results.csv

# Deduplicate & flatten an existing library into {Artist} - {Title}.{ext}
songetter organize -o ./Music

# Environment and dependency diagnostics
songetter doctor -o ./Music --json
```

Legacy positional/flag invocations remain supported (`songetter <spotify-link>`, `songetter -i links.txt -o ./Music --max-concurrent 4`).

### FastAPI Web Dashboard & REST API

Start the server with `songetter serve` (or `uvicorn songetter.web.app:create_app --factory --host 0.0.0.0 --port 8000`), then open `http://localhost:8000`. Key REST endpoints:
- `GET /` — Server-side Jinja2 dashboard
- `POST /api/library` / `GET /api/library` — Validate and select the server library directory
- `GET /api/library/tracks` — List flat library tracks and audio metadata
- `GET /api/library/tracks/{filename}/stream` — Stream an audio track to web or Android clients
- `POST /api/upload` — Parse & validate RFC 4180 CSV uploads
- `POST /api/jobs` / `GET /api/jobs/{job_id}` — Execute and inspect download/dedup/organization jobs
- `POST /api/organize` — Deduplicate and flatten the active library
- `GET /api/search` / `POST /api/search` — Federated multi-source search

### Legacy Streamlit UI

```bash
streamlit run app.py
```

## Hybrid Android APK (Kotlin + Jetpack Compose + Chaquopy)

The `android/` Gradle project bundles `src/songetter/` directly into an Android APK running Python 3.11 via Chaquopy (`15.0.1`), with `mutagen>=1.47`, `httpx>=0.27`, and `yt-dlp`.

### Architecture

- **`src/songetter/android_bridge.py`**:
  - Pure-Python Spotify metadata resolution (`resolve_spotify_url_pure_python`) using Spotify embed HTML (`__NEXT_DATA__`) and oEmbed JSON without Node.js.
  - LRCLIB lyrics lookup (`fetch_lyrics_lrclib`) and metadata-only tag embedding (`embed_lyrics_in_audio`, `embed_basic_tags`) — **zero `.lrc` sidecar files**.
  - `run_on_device_job`: runs `LibraryIndex` pre-check, optional `yt-dlp` stream resolution, `Downloader` atomic `.part` download, LRCLIB tag embedding, quality deduplication, and flat organization locally on the phone.
  - `run_remote_server_job`: skips tracks already present locally, submits remaining tracks to a remote Songetter FastAPI server (`POST /api/jobs`), streams new audio files from `GET /api/library/tracks/{filename}/stream` into `.part` files promoted via `os.replace`, and organizes the local folder.
  - Crash-safe JSON facades (`bridge_get_status`, `bridge_list_library`, `bridge_start_job`, `bridge_search`, `bridge_organize_library`).
- **`android/`**:
  - `MainActivity.kt` & `PythonBridge.kt`: Jetpack Compose Material 3 app with 4 tabs (`Download` [On-Device / Remote Sync + CSV picker], `Search`, `Library` [Flat list + Deduplicate & Flatten], `Settings` [Library path, Remote Server URL, Android storage permissions]).

### Building the Android APK

1. **Using Docker (`Dockerfile.android`)** (requires no local Android SDK):
   ```bash
   docker build -f Dockerfile.android -t songetter-android:latest .
   docker create --name songetter-apk-tmp songetter-android:latest
   docker cp songetter-apk-tmp:/output/songetter-debug.apk ./songetter-debug.apk
   docker rm songetter-apk-tmp
   ```
2. **Using Local Android SDK 34 + JDK 17 + Python 3.11**:
   ```bash
   cd android
   gradle wrapper --gradle-version 8.7
   ./gradlew assembleDebug --no-daemon
   ```
   The debug APK is generated at `android/app/build/outputs/apk/debug/app-debug.apk`.
3. **GitHub Actions CI**: `.github/workflows/android-apk.yml` runs the full test suite (`tests/test_android_bridge.py` included), builds the debug APK, uploads the `songetter-android-debug-apk` workflow artifact, and publishes `songetter-debug.apk` to GitHub Releases on pushes to `main`.

### Download Android APK Package

Prebuilt Android APK packages built from `main` by GitHub Actions are published on GitHub Releases:

- **Latest Release APK**: `https://github.com/Augusto-Dantas/Songetter/releases/latest/download/songetter-debug.apk`
- **Release Tag (`v0.1.0`)**: `https://github.com/Augusto-Dantas/Songetter/releases/tag/v0.1.0`

### Android Storage Paths & Permissions

- `get_default_android_library_dir()` checks `SONGETTER_LIBRARY_DIR`, `/storage/emulated/0/Music/Songetter` (when writable), `ANDROID_STORAGE`/`EXTERNAL_STORAGE`, and falls back to `~/Music/Songetter` (app-scoped storage on Android).
- To write directly to shared `/Music/Songetter` on Android 11+, grant **All Files Access** (`MANAGE_EXTERNAL_STORAGE`) from the app's **Settings** tab; otherwise use the default app-scoped Music folder.

## Integrated Providers (Mornye)

Songetter configures the unified catalog automatically when Mornye is used on desktop/server:

`https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry-unified.json`

Default providers (fallback priority): `ext:tidal-web`, `ext:qobuz-web`, `ext:deezer`, `ext:amazon`, `ext:ytmusic-spotiflac`, `ext:soundcloud`, and `ext:jiosaavn`.

Only download content you are authorized to access, and comply with the terms of each service.

## Tests

```bash
pytest -q
```

All unit, domain, web API, TUI/CLI, Android bridge, and E2E tests run offline using `MockAudioServer`, `httpx.MockTransport`, and synthetic audio/CSV fixtures.

## License

MIT. See also the licenses of the providers and extensions you use.
