"""E2E Test Configuration and Fixtures for Songetter.

Provides 100% offline, deterministic test infrastructure:
- In-process MockAudioServer for HTTP streaming media simulation.
- Synthetic audio generators for MP3, WAV, and FLAC with ID3 and Vorbis tags.
- RFC 4180 synthetic CSV generator with delimiter sniffing and BOM support.
- Filesystem fixtures for clean, unwritable, and nested music libraries.
- FastAPI TestClient fixture for REST API and web UI route validation.
"""

from __future__ import annotations

import csv
import http.server
import io
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

import mutagen
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from mutagen.id3 import COMM, ID3, TIT2, TPE1
from mutagen.wave import WAVE

# ---------------------------------------------------------------------------
# Module Availability Checks for Progressive Milestones
# ---------------------------------------------------------------------------

try:
    from songetter.web.app import create_app
    has_web_module = True
except (ImportError, ModuleNotFoundError):
    try:
        from songetter.web import app as fastapi_app
        create_app = lambda: fastapi_app  # type: ignore[assignment]
        has_web_module = True
    except (ImportError, ModuleNotFoundError):
        has_web_module = False
        create_app = None  # type: ignore[assignment]

try:
    from songetter.downloader import Downloader, download_tracks  # type: ignore[import-not-found]
    has_downloader_module = True
except (ImportError, ModuleNotFoundError):
    has_downloader_module = False
    Downloader = None  # type: ignore[assignment]
    download_tracks = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Mock HTTP Streaming Server Component
# ---------------------------------------------------------------------------

class MockAudioServer:
    """Zero-dependency background HTTP server for offline media streaming tests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.routes: Dict[str, Callable[[http.server.BaseHTTPRequestHandler], None]] = {}
        self.request_log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        server_instance = self

        class StreamingHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                # Silence console logging during test runs
                pass

            def do_GET(self) -> None:
                with server_instance._lock:
                    server_instance.request_log.append({
                        "path": self.path,
                        "headers": dict(self.headers),
                    })
                clean_path = self.path.split("?")[0]
                if clean_path in server_instance.routes:
                    server_instance.routes[clean_path](self)
                else:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"404 Not Found")

        self.server = http.server.ThreadingHTTPServer((host, port), StreamingHandler)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url_for(self, route: str) -> str:
        return f"{self.base_url}/{route.lstrip('/')}"

    def reset(self) -> None:
        with self._lock:
            self.routes.clear()
            self.request_log.clear()

    def get_request_count(self, path_substring: Optional[str] = None) -> int:
        with self._lock:
            if path_substring is None:
                return len(self.request_log)
            return sum(1 for r in self.request_log if path_substring in r["path"])

    def serve_bytes(
        self,
        route: str,
        data: bytes,
        content_type: str = "audio/mpeg",
        status_code: int = 200,
    ) -> str:
        route_key = "/" + route.lstrip("/")

        def handler(req: http.server.BaseHTTPRequestHandler) -> None:
            req.send_response(status_code)
            req.send_header("Content-Type", content_type)
            req.send_header("Content-Length", str(len(data)))
            req.end_headers()
            req.wfile.write(data)

        self.routes[route_key] = handler
        return self.url_for(route_key)

    def serve_error(
        self,
        route: str,
        status_code: int = 404,
        retry_after: Optional[int] = None,
    ) -> str:
        route_key = "/" + route.lstrip("/")

        def handler(req: http.server.BaseHTTPRequestHandler) -> None:
            req.send_response(status_code)
            if retry_after is not None:
                req.send_header("Retry-After", str(retry_after))
            req.send_header("Content-Type", "text/plain")
            req.end_headers()
            req.wfile.write(f"HTTP {status_code} Error".encode("utf-8"))

        self.routes[route_key] = handler
        return self.url_for(route_key)

    def serve_truncated(
        self,
        route: str,
        full_size: int = 100000,
        cut_at_byte: int = 500,
    ) -> str:
        route_key = "/" + route.lstrip("/")

        def handler(req: http.server.BaseHTTPRequestHandler) -> None:
            req.send_response(200)
            req.send_header("Content-Type", "audio/mpeg")
            req.send_header("Content-Length", str(full_size))
            req.end_headers()
            chunk = b"MPEG_TRUNCATED_STREAM" * (cut_at_byte // 21 + 1)
            req.wfile.write(chunk[:cut_at_byte])
            req.wfile.flush()
            # Force abrupt TCP socket closure
            req.connection.close()

        self.routes[route_key] = handler
        return self.url_for(route_key)

    def serve_slow(
        self,
        route: str,
        data: bytes,
        chunk_size: int = 500,
        delay_sec: float = 0.02,
    ) -> str:
        route_key = "/" + route.lstrip("/")

        def handler(req: http.server.BaseHTTPRequestHandler) -> None:
            req.send_response(200)
            req.send_header("Content-Type", "audio/mpeg")
            req.send_header("Content-Length", str(len(data)))
            req.end_headers()
            for i in range(0, len(data), chunk_size):
                req.wfile.write(data[i : i + chunk_size])
                req.wfile.flush()
                time.sleep(delay_sec)

        self.routes[route_key] = handler
        return self.url_for(route_key)

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Synthetic Audio Generator
# ---------------------------------------------------------------------------

def generate_minimal_mp3_frames(
    bitrate_kbps: int = 128,
    sample_rate: int = 44100,
    duration_sec: float = 1.0,
) -> bytes:
    """Generates valid MPEG-1 Layer 3 audio frames without external codecs."""
    bitrate_map = {
        32: 1, 40: 2, 48: 3, 56: 4, 64: 5, 80: 6, 96: 7, 112: 8,
        128: 9, 160: 10, 192: 11, 224: 12, 256: 13, 320: 14,
    }
    sr_map = {44100: 0, 48000: 1, 32000: 2}
    br_idx = bitrate_map.get(bitrate_kbps, 9)
    sr_idx = sr_map.get(sample_rate, 0)

    # 4-byte MPEG sync header
    header = bytes([0xFF, 0xFB, (br_idx << 4) | (sr_idx << 2), 0x00])
    frame_size = int(144 * (bitrate_kbps * 1000) / sample_rate)
    frame_data = header + b"\x00" * max(0, frame_size - 4)

    num_frames = int(duration_sec * (sample_rate / 1152.0))
    return frame_data * max(10, num_frames)


def create_synthetic_audio(
    path: Union[str, Path],
    format: str = "mp3",
    artist: str = "Test Artist",
    title: str = "Test Title",
    bitrate_kbps: int = 128,
    sample_rate: int = 44100,
    channels: int = 2,
    duration_sec: float = 1.0,
    pad_bytes: int = 0,
) -> Path:
    """Creates a tagged synthetic audio file (MP3, FLAC, or WAV) with exact quality metrics."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = format.lower().lstrip(".")

    if fmt == "mp3":
        raw_audio = generate_minimal_mp3_frames(
            bitrate_kbps=bitrate_kbps,
            sample_rate=sample_rate,
            duration_sec=duration_sec,
        )
        out_path.write_bytes(raw_audio)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=[title]))
        tags.add(TPE1(encoding=3, text=[artist]))
        if pad_bytes > 0:
            tags.add(COMM(encoding=3, lang="eng", desc="padding", text=["X" * pad_bytes]))
        tags.save(str(out_path))

    elif fmt == "wav":
        t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
        audio_data = 0.5 * np.sin(2 * np.pi * 440 * t)
        if channels == 2:
            audio_data = np.column_stack([audio_data, audio_data])
        subtype = "PCM_24" if bitrate_kbps > 2000 else "PCM_16"
        sf.write(str(out_path), audio_data, sample_rate, format="WAV", subtype=subtype)
        wav = WAVE(str(out_path))
        wav.add_tags()
        wav.tags.add(TIT2(encoding=3, text=[title]))
        wav.tags.add(TPE1(encoding=3, text=[artist]))
        if pad_bytes > 0:
            wav.tags.add(COMM(encoding=3, lang="eng", desc="padding", text=["X" * pad_bytes]))
        wav.save()

    elif fmt == "flac":
        t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
        audio_data = 0.5 * np.sin(2 * np.pi * 440 * t)
        if channels == 2:
            audio_data = np.column_stack([audio_data, audio_data])
        subtype = "PCM_24" if bitrate_kbps > 300 else "PCM_16"
        sf.write(str(out_path), audio_data, sample_rate, format="FLAC", subtype=subtype)
        flac = FLAC(str(out_path))
        flac["artist"] = [artist]
        flac["title"] = [title]
        if pad_bytes > 0:
            flac.save(padding=lambda info: pad_bytes)
        else:
            flac.save()
    else:
        raise ValueError(f"Unsupported synthetic format: {format}")

    return out_path


# ---------------------------------------------------------------------------
# Synthetic CSV Generator
# ---------------------------------------------------------------------------

def create_synthetic_csv(
    tracks: Sequence[Union[Dict[str, str], str]],
    delimiter: str = ",",
    header: Optional[Sequence[str]] = ("url", "artist", "title"),
    bom: bool = False,
    headerless: bool = False,
    file_path: Optional[Union[str, Path]] = None,
) -> bytes:
    """Generates RFC 4180 CSV bytes with configurable delimiters, aliases, and BOM."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)

    if not headerless and header:
        writer.writerow(header)

    for item in tracks:
        if headerless:
            url_val = item.get("url", "") if isinstance(item, dict) else str(item)
            writer.writerow([url_val])
        else:
            row = []
            for col in (header or ()):
                k = col.lower()
                if isinstance(item, dict):
                    if k in ("url", "link", "download_url", "track_uri", "source_url"):
                        row.append(item.get("url", item.get(col, "")))
                    elif k in ("artist", "performer", "author", "artist_name"):
                        row.append(item.get("artist", item.get(col, "")))
                    elif k in ("title", "song", "track", "name", "song_title"):
                        row.append(item.get("title", item.get(col, "")))
                    else:
                        row.append(item.get(col, ""))
                else:
                    row.append(str(item))
            writer.writerow(row)

    content_str = buf.getvalue()
    raw_bytes = (b"\xef\xbb\xbf" if bom else b"") + content_str.encode("utf-8")

    if file_path is not None:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw_bytes)

    return raw_bytes


# ---------------------------------------------------------------------------
# Pytest Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_audio_server():
    """Provides a running background MockAudioServer and guarantees clean shutdown."""
    server = MockAudioServer()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture
def clean_library_dir(tmp_path: Path) -> Path:
    """Yields a clean, empty, and writable music library directory."""
    lib = tmp_path / "clean_library"
    lib.mkdir(parents=True, exist_ok=True)
    return lib


@pytest.fixture
def unwritable_library_dir(tmp_path: Path):
    """Yields an unwritable directory (0o555) and safely restores permissions on teardown."""
    ro_dir = tmp_path / "unwritable_library"
    ro_dir.mkdir(parents=True, exist_ok=True)
    ro_dir.chmod(0o555)
    try:
        yield ro_dir
    finally:
        ro_dir.chmod(0o755)


@pytest.fixture
def nested_library_dir(tmp_path: Path) -> Path:
    """Pre-populates a nested directory tree with subfolders and empty folders for R4 tests."""
    lib = tmp_path / "nested_library"
    lib.mkdir(parents=True, exist_ok=True)

    # Nested folder levels
    (lib / "Artist 1" / "Album A").mkdir(parents=True, exist_ok=True)
    (lib / "Artist 2" / "Empty Album").mkdir(parents=True, exist_ok=True)
    (lib / "Downloads" / "tmp_sub").mkdir(parents=True, exist_ok=True)

    create_synthetic_audio(
        lib / "Artist 1" / "Album A" / "track1.mp3",
        artist="Artist 1",
        title="Song 1",
        bitrate_kbps=128,
    )
    create_synthetic_audio(
        lib / "Downloads" / "tmp_sub" / "track2.flac",
        format="flac",
        artist="Artist 2",
        title="Song 2",
    )
    return lib


@pytest.fixture
def api_client():
    """Provides a FastAPI TestClient for web endpoints."""
    if not has_web_module or create_app is None:
        pytest.skip("songetter.web is not yet implemented (Milestone 3)")
    app = create_app()
    with TestClient(app) as client:
        yield client
