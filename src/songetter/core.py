from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SPOTIFY_HOSTS = {"open.spotify.com", "spotify.link"}
AUDIO_SUFFIXES = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".wv", ".tta"}
MORNYE_REGISTRY = "https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry-unified.json"
DEFAULT_SERVICES = ["ext:tidal-web", "ext:qobuz-web", "ext:deezer", "ext:amazon", "ext:ytmusic-spotiflac", "ext:soundcloud", "ext:jiosaavn"]


@dataclass
class RunResult:
    url: str
    downloaded: int
    skipped: int
    failed: int
    tracks: list[dict]
    error: str | None = None
    elapsed_seconds: float = 0.0


def configure_mornye_registry() -> str:
    registries = [x.strip() for x in os.environ.get("MORNYE_REGISTRIES", "").split(",") if x.strip()]
    if MORNYE_REGISTRY not in registries:
        registries.append(MORNYE_REGISTRY)
    os.environ["MORNYE_REGISTRIES"] = ",".join(registries)
    return os.environ["MORNYE_REGISTRIES"]


def parse_links(raw: str) -> list[str]:
    found = []
    for candidate in re.split(r"[\s,;]+", raw.strip()):
        if not candidate:
            continue
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or host not in SPOTIFY_HOSTS:
            raise ValueError(f"Invalid Spotify link: {candidate}")
        if host == "open.spotify.com":
            canonical_path = re.sub(r"^/intl-[a-z]{2}(?:-[a-z]{2})?/", "/", parsed.path, flags=re.IGNORECASE)
            if not re.match(r"^/(track|album|playlist|artist)/[A-Za-z0-9]+/?$", canonical_path):
                raise ValueError(f"Unsupported Spotify link type: {candidate}")
            candidate = parsed._replace(path=canonical_path).geturl()
        if candidate not in found:
            found.append(candidate)
    if not found:
        raise ValueError("Provide at least one Spotify link.")
    return found


def _mornye_command() -> str:
    executable = shutil.which("mornye")
    if not executable:
        raise RuntimeError("The 'mornye' executable was not found. Install the project dependencies with: pip install -e .")
    return executable


def _safe_callback(callback: Callable | None, value) -> None:
    if callback is None:
        return
    try:
        callback(value)
    except Exception:
        # UI callbacks must never interrupt or terminate the download process.
        pass


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _extract_log_failures(stderr_text: str) -> list[dict]:
    failures: dict[str, dict] = {}
    for line in stderr_text.splitlines():
        match = re.search(r"✗\s+\[#(\d+)\]\s+(\S+)\s+·\s+(.+)$", line)
        if match:
            index, provider, detail = match.groups()
            failures[index] = {
                "index": int(index),
                "provider": provider,
                "status": "failed",
                "error": detail.strip(),
            }

    # Some Mornye providers emit a warning with a title but no track index.
    if not failures:
        for line in stderr_text.splitlines():
            match = re.search(
                r"\[([^\]]+)\]\s+Download FAILED for ['\\"](.+?)['\\"].*?:\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if match:
                provider, title, detail = match.groups()
                failures[title] = {
                    "title": title,
                    "provider": provider,
                    "status": "failed",
                    "error": detail.strip(),
                }
    return list(failures.values())


def _read_report(report_text: str, url: str, return_code: int, stderr_tail: str) -> RunResult:
    if not report_text.strip():
        detail = stderr_tail or f"Mornye exited with status {return_code} without producing a JSON report."
        return RunResult(url, 0, 0, 1, [], detail[-4000:])
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as exc:
        detail = stderr_tail or f"Mornye returned invalid JSON ({exc})."
        return RunResult(url, 0, 0, 1, [], detail[-4000:])
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return RunResult(url, 0, 0, 1, [], "Mornye returned an unrecognized JSON report.")

    tracks = report.get("tracks", [])
    summary = report.get("summary", {})
    if not isinstance(tracks, list):
        tracks = []
    if not isinstance(summary, dict):
        summary = {}

    downloaded = int(summary.get("downloaded", 0) or 0)
    skipped = int(summary.get("skipped", 0) or 0)
    reported_failed = int(summary.get("failed", 0) or 0)
    log_failures = _extract_log_failures(stderr_tail)
    json_failed_tracks = sum(1 for track in tracks if isinstance(track, dict) and track.get("status") == "failed")
    failed = max(reported_failed, json_failed_tracks, len(log_failures))

    # Keep provider diagnostics visible even when Mornye's summary under-reports failures.
    missing_log_records = max(0, len(log_failures) - json_failed_tracks)
    tracks.extend(log_failures[:missing_log_records])

    error = None
    if return_code != 0:
        error = stderr_tail[-4000:] or f"Mornye exited with status {return_code}."
    elif failed:
        error = f"{failed} track(s) failed. Provider diagnostics were collected from Mornye's output."
    return RunResult(url, downloaded, skipped, failed, tracks, error)


def run_download(
    url: str,
    output_dir: Path,
    services: list[str] | None = None,
    lyrics_providers: list[str] | None = None,
    timeout: int = 3600,
    max_concurrent_downloads: int = 2,
    progress_callback: Callable[[str], None] | None = None,
    metrics_callback: Callable[[dict], None] | None = None,
) -> RunResult:
    started = time.monotonic()
    configure_mornye_registry()
    selected = list(services or DEFAULT_SERVICES)
    if not selected:
        raise ValueError("Select at least one Mornye provider.")
    if max_concurrent_downloads < 1:
        raise ValueError("Concurrent download count must be at least 1.")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        _mornye_command(), url, str(output_dir), "--json",
        "--use-artist-subfolders", "--use-album-subfolders",
        "--lyrics-providers", *(lyrics_providers or ["apple", "lrclib"]),
        "--save-lrc", "--max-concurrent", str(max_concurrent_downloads),
        "--service", *selected,
    ]

    stderr_queue: queue.Queue[str | None] = queue.Queue()
    stderr_tail: deque[str] = deque(maxlen=80)
    report_text = ""
    process = None
    total_tracks = 0
    terminal_tracks = 0
    last_metrics_at = started
    last_bytes = _directory_bytes(output_dir)
    last_speed = 0.0

    def read_stderr(stream) -> None:
        try:
            for raw_line in stream:
                stderr_queue.put(raw_line.rstrip())
        finally:
            stderr_queue.put(None)

    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as report_file:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=report_file,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=os.environ.copy(),
                )
            except FileNotFoundError as exc:
                return RunResult(url, 0, 0, 1, [], f"Could not start Mornye: {exc}", time.monotonic() - started)
            except OSError as exc:
                return RunResult(url, 0, 0, 1, [], f"Operating system error while starting Mornye: {exc}", time.monotonic() - started)

            assert process.stderr is not None
            reader = threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True)
            reader.start()
            stream_finished = False

            while process.poll() is None or not stream_finished or not stderr_queue.empty():
                elapsed = time.monotonic() - started
                if elapsed > timeout and process.poll() is None:
                    process.kill()
                    process.wait()
                    return RunResult(
                        url, 0, 0, 1, [],
                        f"Mornye timed out after {timeout} seconds. The process was terminated.",
                        time.monotonic() - started,
                    )

                try:
                    line = stderr_queue.get(timeout=0.25)
                except queue.Empty:
                    line = ""

                if line is None:
                    stream_finished = True
                elif line:
                    stderr_tail.append(line)
                    _safe_callback(progress_callback, line)

                    run_match = re.search(r"\[RUN\]\s+(\d+)\s+track", line, re.IGNORECASE)
                    if run_match:
                        total_tracks = int(run_match.group(1))

                    if re.match(r"^\s*[✓✔⏭✗]\s", line):
                        terminal_tracks += 1

                now = time.monotonic()
                if now - last_metrics_at >= 1.0:
                    current_bytes = _directory_bytes(output_dir)
                    delta = max(0, current_bytes - last_bytes)
                    last_speed = delta / max(now - last_metrics_at, 0.001)
                    last_bytes = current_bytes
                    last_metrics_at = now
                    elapsed = now - started
                    eta = None
                    if total_tracks and terminal_tracks > 0 and terminal_tracks < total_tracks:
                        eta = elapsed / terminal_tracks * (total_tracks - terminal_tracks)
                    _safe_callback(metrics_callback, {
                        "speed_bps": last_speed,
                        "eta_seconds": eta,
                        "elapsed_seconds": elapsed,
                        "completed_tracks": min(terminal_tracks, total_tracks) if total_tracks else terminal_tracks,
                        "total_tracks": total_tracks,
                    })

            return_code = process.wait()
            report_file.seek(0)
            report_text = report_file.read()

    except Exception as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        return RunResult(
            url, 0, 0, 1, [],
            f"Unexpected download error ({type(exc).__name__}): {exc}",
            time.monotonic() - started,
        )

    result = _read_report(report_text, url, return_code, "\n".join(stderr_tail))
    result.elapsed_seconds = time.monotonic() - started
    _safe_callback(metrics_callback, {
        "speed_bps": last_speed,
        "eta_seconds": 0,
        "elapsed_seconds": result.elapsed_seconds,
        "completed_tracks": result.downloaded + result.skipped + result.failed,
        "total_tracks": total_tracks,
        "finished": True,
    })
    return result


def write_m3u8(tracks: list[dict], destination: Path, library_root: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines, count = ["#EXTM3U"], 0
    for track in tracks:
        if track.get("status") not in {"downloaded", "skipped"} or not track.get("file_path"):
            continue
        audio = Path(track["file_path"]).expanduser()
        if not audio.is_file() or audio.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        try:
            entry = os.path.relpath(audio.resolve(), destination.parent.resolve())
        except ValueError:
            entry = str(audio.resolve())
        title = str(track.get("title") or audio.stem).replace("\r", " ").replace("\n", " ")
        artist = str(track.get("artists") or "").replace("\r", " ").replace("\n", " ")
        duration = max(0, int(track.get("duration_ms", 0) or 0) // 1000)
        lines.extend([f"#EXTINF:{duration},{artist} - {title}", entry.replace(os.sep, "/")])
        count += 1
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count
