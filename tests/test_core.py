from pathlib import Path

import pytest

from songetter.core import parse_links, write_m3u8


def test_parse_links_deduplicates_and_accepts_track_and_playlist():
    links = parse_links(
        "https://open.spotify.com/track/abc123\nhttps://open.spotify.com/playlist/def456"
    )
    assert len(links) == 2


def test_parse_links_rejects_non_spotify():
    with pytest.raises(ValueError):
        parse_links("https://example.com/song")


def test_write_m3u8_only_includes_existing_audio(tmp_path: Path):
    audio = tmp_path / "Artist" / "Song.flac"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    tracks = [
        {"status": "downloaded", "file_path": str(audio), "title": "Song",
         "artists": "Artist", "duration_ms": 123000},
        {"status": "failed", "file_path": str(audio), "title": "Nope"},
    ]
    playlist = tmp_path / "library.m3u8"
    assert write_m3u8(tracks, playlist, tmp_path) == 1
    content = playlist.read_text(encoding="utf-8")
    assert "#EXTM3U" in content
    assert "Artist - Song" in content
    assert "Song.flac" in content
