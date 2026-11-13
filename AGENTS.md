# AGENTS.md

Guidelines for AI agents and contributors working on **Songetter**.

## Project Overview

Songetter is a Python 3.11+ music library downloader and organizer built around [Mornye-Python](https://github.com/mornyeapp/Mornye-Python), `mutagen`, `ffmpeg`, Streamlit (`app.py`), and an in-progress server-side FastAPI ingestion pipeline (`src/songetter/`).

See `NEXT_STEPS.md` for the active implementation roadmap and current milestone status.

## Environment & Tooling

- **Python**: `>=3.11` (local dev environment uses `.venv/` with Python 3.14).
- **Install editable with dev/test dependencies**:
  ```bash
  pip install -e ".[dev]"
  ```
- **System binaries required on `PATH`**:
  - `ffmpeg` / `ffprobe` (audio validation and synthetic test audio generation)
  - `node` (Node.js runtime for Mornye JavaScript provider extensions)
- **Run tests**:
  ```bash
  pytest -q
  ```
  Do not require live network calls in unit or E2E tests; use `tests/e2e/conftest.py` (`MockAudioServer`, `create_synthetic_audio`, `create_synthetic_csv`) for offline deterministic testing.

## Architecture & Module Map

- `src/songetter/config.py` — Shared constants (`AUDIO_SUFFIXES`, `MAX_FILENAME_BYTES`, `PROBE_PREFIX`, CSV column aliases, delimiters).
- `src/songetter/csv_processor.py` — RFC 4180 CSV/text parsing, delimiter sniffing (`,`, `;`, `\t`, `|`), BOM/encoding handling, header alias mapping, headerless URL fallback, and `TrackRecord` creation.
- `src/songetter/library.py` — Server library path validation (`validate_library_directory` with write-probe verification) and `LibraryIndex` (dual stem + metadata index).
- `src/songetter/deduplicator.py` — Mutagen metadata extraction (`extract_metadata`), Unicode normalization (`normalize_text`), quality comparison (`compare_quality`: bitrate -> file size -> deterministic tie-breaker), and `resolve_duplicates`.
- `src/songetter/organizer.py` — Flat directory flattening (`flatten_and_organize_library`), `{Artist} - {Title}.{ext}` standardization (`format_standard_filename`), forbidden-character sanitization, UTF-8 byte-length bounding (<=240 bytes), collision suffixing, and empty folder pruning.
- `src/songetter/core.py` — Mornye subprocess execution, registry configuration, provider circuit-breaker cooldowns, and `.m3u8` playlist generation.
- `src/songetter/cli.py` — CLI entry point (`songetter`).
- `app.py` — Current Streamlit web UI.
- *(Planned M2)* `src/songetter/downloader.py` — Multi-protocol downloader with pre-download `LibraryIndex` skip check and atomic `.part` staging.
- *(Planned M3)* `src/songetter/web/` — FastAPI + Jinja2 server-side web UI and REST API (`create_app()`).

## Non-Negotiable Engineering Rules

1. **Metadata-Only Lyrics (No `.lrc` Sidecars)**:
   - Lyrics must be stored **only** inside audio file metadata tags (e.g., Vorbis `lyrics` comment, ID3 `USLT`, MP4 `\xa9lyr`).
   - Do **not** pass `--save-lrc` to `mornye`.
   - Do **not** generate, move, rename, or delete `.lrc` sidecar files in `organizer.py`, `deduplicator.py`, or `downloader.py`.
2. **Flat Library Organization**:
   - The target music library is organized as a **flat folder** at the selected library root using `{Artist} - {Title}.{ext}`.
   - Nested directories created by downloaders must be flattened and empty directories pruned bottom-up.
3. **Deduplication & Quality Retention**:
   - Match duplicates by normalized `(artist, title)` metadata.
   - Retain the copy with higher audio bitrate (`bps`), breaking ties by larger file size (`bytes`). If both metrics are equal, retain one copy and delete the other deterministically.
4. **Mutagen Tag Safety**:
   - Always guard raw `tags.get(key)` calls against `ValueError`, `KeyError`, and `TypeError` (e.g., Mutagen `VComment` raises `ValueError` for non-ASCII keys like `"\xa9ART"`).
5. **Repository & Container Hygiene**:
   - Never commit or bake `downloads/`, `Music/`, `.venv/`, `.pytest_cache/`, or `.agents/` into Git or Docker images (enforced via `.gitignore` and `.dockerignore`).
   - Do not hardcode machine-specific absolute paths like `/home/<user>/...` in tests or source code; resolve relative to `Path(__file__)` or configuration/environment variables (`SONGETTER_LIBRARY_DIR`).
