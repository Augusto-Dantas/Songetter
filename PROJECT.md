# Project: Songetter Functional Verification & Speed Diagnosis

## Architecture
Songetter is a Python orchestration CLI and GUI wrapper built on top of the `mornye` music downloading engine.
- **CLI/Core (`src/songetter/cli.py`, `src/songetter/core.py`)**: Parses Spotify links, canonicalizes internationalized URLs, configures Mornye registry, and invokes Mornye subprocesses.
- **Execution & Provider Fallback**: Invokes `mornye` with Spotify URL and configured audio providers (`ext:tidal-web`, `ext:qobuz-web`, `ext:deezer`, `ext:amazon`, `ext:ytmusic-spotiflac`, `ext:soundcloud`, `ext:jiosaavn`). Monitors stderr for rate limits/cooldowns (HTTP 429, verification paused), triggering an internal circuit breaker to persist cooldowns in `~/.cache/songetter/provider_cooldowns.json` and fallback to the next provider.
- **Speed & Progress Calculation**: Measures directory byte deltas across completed audio files (`AUDIO_SUFFIXES`). Does not track `.part` files, leading to native speed metrics reporting 0 B/s during active transfers.
- **Playlist Generation**: Generates `Songetter.m3u8` with relative paths to downloaded tracks upon completion.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Repository & Dependency Setup | Clone repo, set up Python venv, install dependencies (`mornye`, `streamlit`), verify system tools (`ffmpeg`, `node`, `git`). | M1 | survey (explorer 1) |
| 2 | Spotify URL Parsing & Normalization | Validate and canonicalize internationalized album links (`/intl-pt/` -> `/album/`). | M1 | survey (explorer 2) |
| 3 | Functional Download Execution | Execute Songetter against album `https://open.spotify.com/intl-pt/album/4SZko61aMnmgvNhfhgTuD3?si=G2_DECW-RfyqGKLXSdKULg` (Kanye West – Graduation, 14 tracks). | M1 | survey (explorer 2) |
| 4 | Real-Time Bandwidth & Progress Monitoring | Concurrently sample `/proc/net/dev` network RX throughput and filesystem directory growth (including `.part` files) at 1s intervals. | M1 | survey (explorer 3) |
| 5 | Output Artifact Verification | Verify 14 audio tracks downloaded with valid metadata tags, artwork, LRC lyrics, and valid `Songetter.m3u8` playlist. | M1 | survey (explorer 2) |
| 6 | Performance Evaluation | Compare observed throughput against host network baseline (~29 MB/s) and expected album completion time (45s–120s, >= 3.0 MB/s). | M1 | survey (explorer 3) |
| 7 | Bottleneck Diagnosis | If slow, systematically diagnose root cause (HTTP 429 cooldowns, CDN packet pacing, Zarz HMAC latency, CLI silence, or CPU/transcoding). | M1 | survey (explorer 3) |
| 8 | Comprehensive Reporting | Generate functional verification report and performance diagnosis report. | M1 | survey (explorer 1, 2, 3) |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Functional Verification & Speed Diagnosis | Environment setup, test execution against album link, real-time speed monitoring, output verification, and bottleneck diagnosis report. | None | DONE |

## Key Outputs
- **Functional Verification Report**: `/home/augusto_dantas/teamwork_projects/songetter/verification_report.md`
- **Speed & Bottleneck Diagnostic Report**: `/home/augusto_dantas/teamwork_projects/songetter/diagnostic_report.md`
- **Downloaded Audio Directory**: `/home/augusto_dantas/teamwork_projects/songetter/downloads/Graduation/` (14 lossless 16-bit FLAC tracks, 14 LRC lyrics files, 1 M3U8 playlist, 375.2 MB)
- **Time-Series Telemetry**: `/home/augusto_dantas/teamwork_projects/songetter/.agents/teamwork/teamwork_worker_1/metrics_timeseries.csv` (51 samples)
- **Subprocess Logs & JSON**: `/home/augusto_dantas/teamwork_projects/songetter/.agents/teamwork/teamwork_worker_1/execution.log`, `execution_summary.json`
- **Gate Status**: PASS (Clean forensic audit, 2 Approve reviewer verdicts, 2 Approve challenger verdicts)

## Code Layout
- `src/songetter/`: Application source code (`cli.py`, `core.py`, `__init__.py`).
- `tests/`: Unit tests (`test_core.py`, `test_milestone1_empirical.py`).
- `downloads/`: Target download directory for test execution.
- `.agents/teamwork/`: Metadata, plans, monitoring logs, and reports.
  - `orchestrator_1/`: Orchestrator state and project summaries.
  - `teamwork_explorer_survey_1/`: Codebase survey report.
  - `teamwork_explorer_survey_2/`: Workflow & target survey report.
  - `teamwork_explorer_survey_3/`: Performance & diagnosis survey report.
  - `teamwork_worker_1/`: Worker state, execution logs, and reports.
  - `teamwork_reviewer_1/`: Functional review report.
  - `teamwork_reviewer_2/`: Performance review report.
  - `teamwork_challenger_1/`: Audio bitstream & artifact challenge report.
  - `teamwork_challenger_2/`: Performance & telemetry challenge report.
  - `teamwork_auditor_1/`: Forensic integrity audit report.

## Baseline Metrics & Benchmarks
- Host line capacity: ~29 MB/s (~232 Mbps).
- Target album: Kanye West – Graduation (14 tracks, ~350–450 MB FLAC).
- Normal execution: 45s–120s ($\ge 3.0\text{ MB/s}$).
- Observed execution: 50.06s, average 7.84 MB/s (peak 25.90 MB/s).
