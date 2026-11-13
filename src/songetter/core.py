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
from dataclasses import dataclass, field
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
    provider_status: list[dict] = field(default_factory=list)


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
                r"""\[([^\]]+)\]\s+Download FAILED for ['"](.+?)['"].*?:\s*(.+)$""",
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




_COOLDOWN_TERMS = (
    "verification paused", "cooldown", "rate limit", "too many requests",
    "http 429", "status 429", "temporarily blocked",
)
_COOLDOWN_LOCK = threading.Lock()


def _cooldown_state_path() -> Path:
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return cache_root / "songetter" / "provider_cooldowns.json"


def _load_cooldowns() -> dict:
    path = _cooldown_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cooldowns(data: dict) -> None:
    path = _cooldown_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # Cooldown persistence is best-effort; it must not interrupt downloads.
        pass


def _active_provider_cooldown(provider: str) -> dict | None:
    with _COOLDOWN_LOCK:
        data = _load_cooldowns()
        entry = data.get(provider)
        if not isinstance(entry, dict):
            return None
        until = float(entry.get("until", 0) or 0)
        remaining = int(until - time.time())
        if remaining <= 0:
            data.pop(provider, None)
            _save_cooldowns(data)
            return None
        return {**entry, "provider": provider, "retry_after_seconds": remaining}


def _record_provider_cooldown(event: dict) -> None:
    with _COOLDOWN_LOCK:
        data = _load_cooldowns()
        data[event["provider"]] = {
            "until": float(event["until"]),
            "reason": event["reason"],
        }
        _save_cooldowns(data)


def _parse_provider_cooldown(line: str, default_provider: str) -> dict | None:
    lowered = line.lower()
    if not any(term in lowered for term in _COOLDOWN_TERMS):
        return None

    provider_match = re.search(r"\[#\d+\]\s+(\S+)", line)
    if not provider_match:
        provider_match = re.search(r"\[([^\]]+)\]", line)
    provider = provider_match.group(1) if provider_match else default_provider

    duration_match = re.search(
        r"(?:next\s+try|retry|try\s+again)\s+in\s+(\d+)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
        line,
        re.IGNORECASE,
    )
    duration = 15 * 60
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2).lower()
        multiplier = 1 if unit.startswith("s") else 3600 if unit.startswith("h") else 60
        duration = max(1, amount * multiplier)

    return {
        "provider": provider,
        "retry_after_seconds": duration,
        "until": time.time() + duration,
        "reason": line.strip()[-1000:],
        "status": "cooldown",
    }


def _track_identity(track: dict) -> str:
    isrc = track.get("isrc") or track.get("ISRC")
    if isrc:
        return "isrc:" + str(isrc).strip().lower()
    title = str(track.get("title") or "").strip().lower()
    artists = track.get("artists") or track.get("artist") or ""
    if isinstance(artists, list):
        artists = ", ".join(str(item) for item in artists)
    album = str(track.get("album") or "").strip().lower()
    if title:
        return "track:" + "|".join((title, str(artists).strip().lower(), album))
    index = track.get("index") or track.get("position") or track.get("track_number")
    if index is not None:
        return "index:" + str(index)
    return "unknown:" + str(id(track))


def _merge_provider_tracks(existing: list[dict], incoming: list[dict]) -> list[dict]:
    priority = {"failed": 0, "error": 0, "skipped": 1, "existing": 1, "downloaded": 2, "success": 2}
    merged = {_track_identity(track): dict(track) for track in existing}
    for track in incoming:
        key = _track_identity(track)
        previous = merged.get(key)
        if previous is None or priority.get(str(track.get("status", "")).lower(), 0) >= priority.get(
            str(previous.get("status", "")).lower(), 0
        ):
            merged[key] = dict(track)
    return list(merged.values())

def _run_download_once(
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
    cooldown_event = None
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

                    detected_cooldown = _parse_provider_cooldown(line, selected[0])
                    if detected_cooldown and cooldown_event is None:
                        cooldown_event = detected_cooldown
                    if detected_cooldown and process.poll() is None:
                        _safe_callback(
                            progress_callback,
                            f"[CIRCUIT BREAKER] {selected[0]} paused; switching to the next provider.",
                        )
                        process.kill()

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

    if cooldown_event:
        result = RunResult(
            url, 0, 0, 0, [],
            f"{cooldown_event['provider']} entered cooldown; remaining tracks will be offered to the next provider.",
            time.monotonic() - started,
            [cooldown_event],
        )
        return result

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
    """Run providers in priority order, opening a circuit when one reports cooldown.

    Mornye is invoked once per provider. It indexes the destination and skips audio
    already downloaded by an earlier provider, so subsequent providers only need
    to fill the remaining gaps.
    """
    started = time.monotonic()
    selected = list(services or DEFAULT_SERVICES)
    if not selected:
        raise ValueError("Select at least one Mornye provider.")
    if max_concurrent_downloads < 1:
        raise ValueError("Concurrent download count must be at least 1.")

    merged_tracks: list[dict] = []
    provider_status: list[dict] = []
    last_error = None
    downloaded_fallback = skipped_fallback = failed_fallback = 0
    ran_provider = False

    for provider in selected:
        active = _active_provider_cooldown(provider)
        if active:
            provider_status.append({
                "provider": provider,
                "status": "cooldown",
                "retry_after_seconds": active["retry_after_seconds"],
                "reason": active.get("reason", "Provider is in cooldown from an earlier run."),
            })
            _safe_callback(
                progress_callback,
                f"[CIRCUIT BREAKER] Skipping {provider}; cooldown has {active['retry_after_seconds']} seconds remaining.",
            )
            continue

        _safe_callback(progress_callback, f"[FALLBACK] Trying provider {provider}.")
        result = _run_download_once(
            url,
            output_dir,
            [provider],
            lyrics_providers,
            timeout,
            max_concurrent_downloads,
            progress_callback,
            metrics_callback,
        )
        ran_provider = True

        if result.provider_status and result.provider_status[0].get("status") == "cooldown":
            event = result.provider_status[0]
            _record_provider_cooldown(event)
            provider_status.append(event)
            last_error = result.error
            continue

        provider_status.append({
            "provider": provider,
            "status": "completed",
            "downloaded": result.downloaded,
            "skipped": result.skipped,
            "failed": result.failed,
        })
        merged_tracks = _merge_provider_tracks(merged_tracks, result.tracks)
        downloaded_fallback = max(downloaded_fallback, result.downloaded)
        skipped_fallback = max(skipped_fallback, result.skipped)
        failed_fallback = max(failed_fallback, result.failed)
        if result.error:
            last_error = result.error

        # A complete report with no failed tracks means no later provider is needed.
        if result.failed == 0 and result.tracks:
            break

    if not ran_provider:
        last_error = "All configured providers are currently in cooldown. Wait for their retry windows or choose other providers."

    if merged_tracks:
        downloaded = sum(1 for track in merged_tracks if track.get("status") in {"downloaded", "success"})
        skipped = sum(1 for track in merged_tracks if track.get("status") in {"skipped", "existing"})
        failed = sum(1 for track in merged_tracks if track.get("status") in {"failed", "error"})
        if failed:
            last_error = f"{failed} track(s) remain unavailable after trying the configured providers."
        elif downloaded or skipped:
            last_error = None
    else:
        downloaded = downloaded_fallback
        skipped = skipped_fallback
        failed = failed_fallback
        if failed and not last_error:
            last_error = f"{failed} track(s) failed across the configured providers."

    elapsed = time.monotonic() - started
    _safe_callback(metrics_callback, {
        "speed_bps": 0,
        "eta_seconds": 0,
        "elapsed_seconds": elapsed,
        "completed_tracks": downloaded + skipped + failed,
        "total_tracks": downloaded + skipped + failed,
        "finished": True,
    })
    return RunResult(
        url, downloaded, skipped, failed, merged_tracks, last_error, elapsed, provider_status
    )

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
