from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SPOTIFY_HOSTS = {"open.spotify.com", "spotify.link"}
AUDIO_SUFFIXES = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".wv", ".tta"}


@dataclass
class RunResult:
    url: str
    downloaded: int
    skipped: int
    failed: int
    tracks: list[dict]
    error: str | None = None


def parse_links(raw: str) -> list[str]:
    """Extract and validate Spotify URLs from whitespace/newline-separated input."""
    found: list[str] = []
    for candidate in re.split(r"[\s,;]+", raw.strip()):
        if not candidate:
            continue
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or host not in SPOTIFY_HOSTS:
            raise ValueError(f"Link Spotify inválido: {candidate}")
        if host == "open.spotify.com" and not re.match(
            r"^/(track|album|playlist|artist)/[A-Za-z0-9]+", parsed.path
        ):
            raise ValueError(f"Tipo de link Spotify não suportado: {candidate}")
        if candidate not in found:
            found.append(candidate)
    if not found:
        raise ValueError("Informe pelo menos um link do Spotify.")
    return found


def _mornye_command() -> str:
    executable = shutil.which("mornye")
    if not executable:
        raise RuntimeError("Executável 'mornye' não encontrado. Instale as dependências com: pip install -e .")
    return executable


def run_download(
    url: str,
    output_dir: Path,
    services: list[str],
    lyrics_providers: list[str] | None = None,
    timeout: int = 3600,
) -> RunResult:
    """Run Mornye for one Spotify URL and parse its machine-readable report."""
    if not services:
        raise ValueError("Configure pelo menos um serviço Mornye instalado (ex.: ext:seu-provedor).")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        _mornye_command(), url, str(output_dir),
        "--json",
        "--use-artist-subfolders",
        "--use-album-subfolders",
        "--lyrics-providers", *(lyrics_providers or ["apple", "lrclib"]),
        "--save-lrc",
    ]
    if services:
        command.extend(["--service", *services])

    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout,
        env=os.environ.copy(), check=False
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        details = (completed.stderr or completed.stdout or "Mornye não retornou JSON válido").strip()
        return RunResult(url, 0, 0, 0, [], details[-4000:])

    if report.get("schema_version") != 1:
        return RunResult(url, 0, 0, 0, [], "Versão de relatório Mornye não reconhecida.")

    tracks = report.get("tracks", [])
    summary = report.get("summary", {})
    return RunResult(
        url=url,
        downloaded=int(summary.get("downloaded", 0)),
        skipped=int(summary.get("skipped", 0)),
        failed=int(summary.get("failed", 0)),
        tracks=tracks,
        error=None if completed.returncode == 0 else (completed.stderr.strip()[-4000:] or "Mornye terminou com erro."),
    )


def write_m3u8(tracks: list[dict], destination: Path, library_root: Path) -> int:
    """Create a portable M3U8 containing only existing successful audio files."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    count = 0
    for track in tracks:
        if track.get("status") not in {"downloaded", "skipped"}:
            continue
        raw_path = track.get("file_path")
        if not raw_path:
            continue
        audio = Path(raw_path).expanduser()
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
