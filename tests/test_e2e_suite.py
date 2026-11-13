"""Top-Level Meta-Suite & Aggregator for Songetter E2E Tests.

Validates the full 60-test E2E architecture across Tiers 1-4:
- Tier 1: Feature Coverage (25 tests)
- Tier 2: Boundary & Corner Cases (25 tests)
- Tier 3: Cross-Feature Interactions (5 tests)
- Tier 4: Real-World Application Scenarios (5 tests)
- Total: 60 requirement-mapped tests
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import tests.e2e.test_tier1_features as tier1
import tests.e2e.test_tier2_boundaries as tier2
import tests.e2e.test_tier3_interactions as tier3
import tests.e2e.test_tier4_scenarios as tier4
from tests.e2e.conftest import (
    MockAudioServer,
    create_synthetic_audio,
    create_synthetic_csv,
)


def _get_test_functions(module) -> list[str]:
    """Inspects module and extracts names of all test functions."""
    return [
        name
        for name, obj in inspect.getmembers(module)
        if inspect.isfunction(obj) and name.startswith("test_")
    ]


def test_e2e_suite_tier_counts_and_integrity() -> None:
    """Verifies that all 60 required tests across Tiers 1-4 are defined and discoverable."""
    t1_tests = _get_test_functions(tier1)
    t2_tests = _get_test_functions(tier2)
    t3_tests = _get_test_functions(tier3)
    t4_tests = _get_test_functions(tier4)

    assert len(t1_tests) == 25, f"Expected 25 Tier 1 tests, found {len(t1_tests)}: {t1_tests}"
    assert len(t2_tests) == 25, f"Expected 25 Tier 2 tests, found {len(t2_tests)}: {t2_tests}"
    assert len(t3_tests) == 5, f"Expected 5 Tier 3 tests, found {len(t3_tests)}: {t3_tests}"
    assert len(t4_tests) == 5, f"Expected 5 Tier 4 tests, found {len(t4_tests)}: {t4_tests}"

    total_tests = len(t1_tests) + len(t2_tests) + len(t3_tests) + len(t4_tests)
    assert total_tests == 60, f"Expected exactly 60 total E2E tests, found {total_tests}"


def test_e2e_synthetic_audio_generator_capabilities(tmp_path: Path) -> None:
    """Verifies synthetic audio generator supports MP3, WAV, and FLAC containers."""
    mp3_path = create_synthetic_audio(tmp_path / "test.mp3", format="mp3", artist="A", title="T", bitrate_kbps=128)
    wav_path = create_synthetic_audio(tmp_path / "test.wav", format="wav", artist="A", title="T")
    flac_path = create_synthetic_audio(tmp_path / "test.flac", format="flac", artist="A", title="T")

    assert mp3_path.exists() and mp3_path.stat().st_size > 0
    assert wav_path.exists() and wav_path.stat().st_size > 0
    assert flac_path.exists() and flac_path.stat().st_size > 0


def test_e2e_mock_http_server_lifecycle() -> None:
    """Verifies MockAudioServer spins up on ephemeral localhost port and shuts down cleanly."""
    server = MockAudioServer()
    try:
        url_200 = server.serve_bytes("stream.mp3", b"PAYLOAD_200", "audio/mpeg")
        url_404 = server.serve_error("bad.mp3", 404)

        assert url_200.startswith("http://127.0.0.1:")
        assert url_404.startswith("http://127.0.0.1:")

        import urllib.request
        with urllib.request.urlopen(url_200) as resp:
            assert resp.status == 200
            assert resp.read() == b"PAYLOAD_200"

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url_404)
        assert exc_info.value.code == 404

        assert server.get_request_count("stream.mp3") == 1
        assert server.get_request_count("bad.mp3") == 1
    finally:
        server.shutdown()
