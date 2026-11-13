from __future__ import annotations
import json, os, re, shutil, subprocess
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

def configure_mornye_registry() -> str:
    registries = [x.strip() for x in os.environ.get("MORNYE_REGISTRIES", "").split(",") if x.strip()]
    if MORNYE_REGISTRY not in registries: registries.append(MORNYE_REGISTRY)
    os.environ["MORNYE_REGISTRIES"] = ",".join(registries)
    return os.environ["MORNYE_REGISTRIES"]

def parse_links(raw: str) -> list[str]:
    found = []
    for candidate in re.split(r"[\s,;]+", raw.strip()):
        if not candidate: continue
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or host not in SPOTIFY_HOSTS:
            raise ValueError(f"Link Spotify inválido: {candidate}")
        if host == "open.spotify.com":
            # Spotify pode incluir um prefixo de idioma, por exemplo /intl-pt/artist/...
            canonical_path = re.sub(r"^/intl-[a-z]{2}(?:-[a-z]{2})?/", "/", parsed.path, flags=re.IGNORECASE)
            if not re.match(r"^/(track|album|playlist|artist)/[A-Za-z0-9]+/?$", canonical_path):
                raise ValueError(f"Tipo de link Spotify não suportado: {candidate}")
            # Entrega ao Mornye a URL canônica, sem o prefixo de localização.
            candidate = parsed._replace(path=canonical_path).geturl()
        if candidate not in found: found.append(candidate)
    if not found: raise ValueError("Informe pelo menos um link do Spotify.")
    return found

def _mornye_command() -> str:
    executable = shutil.which("mornye")
    if not executable: raise RuntimeError("Executável 'mornye' não encontrado. Instale as dependências com: pip install -e .")
    return executable

def run_download(url: str, output_dir: Path, services: list[str] | None = None,
                 lyrics_providers: list[str] | None = None, timeout: int = 3600) -> RunResult:
    configure_mornye_registry()
    selected = list(services or DEFAULT_SERVICES)
    if not selected: raise ValueError("Selecione ao menos um provedor Mornye.")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [_mornye_command(), url, str(output_dir), "--json", "--use-artist-subfolders",
               "--use-album-subfolders", "--lyrics-providers", *(lyrics_providers or ["apple", "lrclib"]),
               "--save-lrc", "--service", *selected]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                               env=os.environ.copy(), check=False)
    try: report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        details = (completed.stderr or completed.stdout or "Mornye não retornou JSON válido").strip()
        return RunResult(url, 0, 0, 0, [], details[-4000:])
    if report.get("schema_version") != 1: return RunResult(url, 0, 0, 0, [], "Versão de relatório Mornye não reconhecida.")
    tracks, summary = report.get("tracks", []), report.get("summary", {})
    return RunResult(url, int(summary.get("downloaded", 0)), int(summary.get("skipped", 0)),
                     int(summary.get("failed", 0)), tracks,
                     None if completed.returncode == 0 else (completed.stderr.strip()[-4000:] or "Mornye terminou com erro."))

def write_m3u8(tracks: list[dict], destination: Path, library_root: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines, count = ["#EXTM3U"], 0
    for track in tracks:
        if track.get("status") not in {"downloaded", "skipped"} or not track.get("file_path"): continue
        audio = Path(track["file_path"]).expanduser()
        if not audio.is_file() or audio.suffix.lower() not in AUDIO_SUFFIXES: continue
        try: entry = os.path.relpath(audio.resolve(), destination.parent.resolve())
        except ValueError: entry = str(audio.resolve())
        title = str(track.get("title") or audio.stem).replace("\r", " ").replace("\n", " ")
        artist = str(track.get("artists") or "").replace("\r", " ").replace("\n", " ")
        duration = max(0, int(track.get("duration_ms", 0) or 0) // 1000)
        lines.extend([f"#EXTINF:{duration},{artist} - {title}", entry.replace(os.sep, "/")])
        count += 1
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count
