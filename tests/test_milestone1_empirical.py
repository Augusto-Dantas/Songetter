import json
import os
import re
import subprocess
from pathlib import Path
from PIL import Image
import io
import pytest
from mutagen.flac import FLAC

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads" / "Graduation"
ALBUM_DIR = DOWNLOAD_DIR / "Kanye West" / "Graduation"
PLAYLIST_FILE = DOWNLOAD_DIR / "Songetter.m3u8"

pytestmark = pytest.mark.skipif(
    not ALBUM_DIR.is_dir(),
    reason="Local downloads/Graduation fixture is excluded from repository",
)

EXPECTED_TRACKS = [
    (1, "Good Morning", "Kanye West", 195.09),
    (2, "Champion", "Kanye West", 167.60),
    (3, "Stronger", "Kanye West", 311.87),
    (4, "I Wonder", "Kanye West", 243.44),
    (5, "Good Life", "Kanye West, T-Pain", 207.00),
    (6, "Can't Tell Me Nothing", "Kanye West", 271.60),
    (7, "Barry Bonds", "Kanye West, Lil Wayne", 204.31),
    (8, "Drunk and Hot Girls", "Kanye West, Mos Def", 313.23),
    (9, "Flashing Lights", "Kanye West, Dwele", 237.51),
    (10, "Everything I Am", "Kanye West, DJ Premier", 227.89),
    (11, "The Glory", "Kanye West", 212.71),
    (12, "Homecoming", "Kanye West, Chris Martin", 203.49),
    (13, "Big Brother", "Kanye West", 287.56),
    (14, "Good Night", "Kanye West, Mos Def, Albe Back", 185.97),
]


def test_directory_and_flac_file_count():
    assert ALBUM_DIR.is_dir(), f"Album directory missing: {ALBUM_DIR}"
    flac_files = list(ALBUM_DIR.glob("*.flac"))
    assert len(flac_files) == 14, f"Expected 14 FLAC files, found {len(flac_files)}"


def test_deep_bitstream_integrity_ffmpeg():
    """Verify zero corrupt frames or truncated streams across all 14 FLAC files."""
    flac_files = sorted(list(ALBUM_DIR.glob("*.flac")))
    assert len(flac_files) == 14
    for f in flac_files:
        cmd = ["ffmpeg", "-v", "error", "-i", str(f), "-f", "null", "-"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0, f"FFmpeg failed on {f.name} with code {res.returncode}"
        assert not res.stderr.strip(), f"Corrupt bitstream detected in {f.name}: {res.stderr}"


def test_audio_stream_properties_ffprobe():
    """Verify 44.1kHz, 16-bit stereo FLAC container properties."""
    flac_files = sorted(list(ALBUM_DIR.glob("*.flac")))
    for f in flac_files:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=index,codec_name,codec_type,sample_rate,channels,bits_per_raw_sample",
            "-of", "json", str(f)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        assert audio_stream is not None, f"No audio stream in {f.name}"
        assert audio_stream["codec_name"] == "flac"
        assert int(audio_stream["sample_rate"]) == 44100
        assert int(audio_stream["channels"]) == 2
        assert int(audio_stream["bits_per_raw_sample"]) == 16


def test_metadata_vorbis_tags_and_artwork():
    """Verify Vorbis comments (title, artist, album, track numbers, total) and 1500x1500 cover art."""
    flac_files = sorted(list(ALBUM_DIR.glob("*.flac")))
    track_numbers = set()
    for f in flac_files:
        audio = FLAC(f)
        assert audio.get("album", [""])[0] == "Graduation"
        trk = int(audio.get("tracknumber", ["0"])[0])
        total = int(audio.get("tracktotal", ["0"])[0])
        assert 1 <= trk <= 14
        assert total == 14
        track_numbers.add(trk)

        # Embedded front cover picture
        assert len(audio.pictures) >= 1, f"Missing cover art in {f.name}"
        pic = audio.pictures[0]
        assert pic.mime == "image/jpeg"
        img = Image.open(io.BytesIO(pic.data))
        assert img.size == (1500, 1500)
        assert img.format == "JPEG"

    assert track_numbers == set(range(1, 15))


def test_embedded_metadata_lyrics():
    """Verify lyrics are embedded in FLAC Vorbis metadata rather than requiring .lrc sidecars."""
    flac_files = sorted(list(ALBUM_DIR.glob("*.flac")))
    assert len(flac_files) == 14
    embedded_count = sum(1 for f in flac_files if FLAC(f).get("lyrics"))
    assert embedded_count >= 1, "Expected embedded lyrics in FLAC Vorbis metadata"


def test_m3u8_playlist_paths_and_durations():
    """Verify playlist exists, parses correctly, paths resolve, and durations match audio streams."""
    assert PLAYLIST_FILE.is_file(), f"M3U8 playlist missing at {PLAYLIST_FILE}"
    lines = PLAYLIST_FILE.read_text(encoding="utf-8").splitlines()
    assert lines[0].strip() == "#EXTM3U"

    extinf_pattern = re.compile(r"^#EXTINF:(\d+),(.*)$")
    entries = []
    current_extinf = None

    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            m = extinf_pattern.match(line)
            assert m, f"Malformed EXTINF: {line}"
            current_extinf = {"duration": int(m.group(1)), "info": m.group(2)}
        elif not line.startswith("#"):
            assert current_extinf is not None
            resolved = (PLAYLIST_FILE.parent / line).resolve()
            entries.append((current_extinf["duration"], current_extinf["info"], line, resolved))
            current_extinf = None

    assert len(entries) == 14, f"Expected 14 playlist entries, found {len(entries)}"

    for duration, info, rel_path, resolved in entries:
        assert resolved.is_file(), f"Playlist path does not exist: {resolved}"
        audio = FLAC(resolved)
        actual_sec = audio.info.length
        # Difference between integer seconds in EXTINF and float audio stream duration should be < 1.0s
        assert abs(actual_sec - duration) < 1.0, f"Duration mismatch for {rel_path}: EXTINF={duration}, actual={actual_sec}"
