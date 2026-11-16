"""Offline unit tests for songetter.search and search config constants."""

from __future__ import annotations

import csv
import io
import json
import threading
from typing import Any, Dict, List

import httpx
import pytest

from songetter import search as search_mod
from songetter.config import SUPPORTED_SEARCH_SOURCES, SUPPORTED_SOURCE_HOSTS
from songetter.csv_processor import parse_csv
from songetter.search import (
    DeezerSearchProvider,
    ItunesSearchProvider,
    MusicBrainzSearchProvider,
    QobuzSearchProvider,
    SearchProvider,
    SearchProviderError,
    SearchResult,
    SpotifySearchProvider,
    TidalSearchProvider,
    UnsupportedSearchSourceError,
    get_search_provider,
    list_search_sources,
    register_search_provider,
    search_results_to_csv,
    search_tracks,
    search_tracks_with_errors,
)


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "SONGETTER_TIDAL_TOKEN",
        "TIDAL_CLIENT_ID",
        "TIDAL_CLIENT_SECRET",
        "SONGETTER_TIDAL_COUNTRY",
        "SONGETTER_TIDAL_API_URL",
        "SONGETTER_QOBUZ_APP_ID",
        "SONGETTER_QOBUZ_TOKEN",
        "SONGETTER_SPOTIFY_TOKEN",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


def test_config_search_and_host_constants():
    assert SUPPORTED_SEARCH_SOURCES == (
        "deezer",
        "tidal",
        "itunes",
        "musicbrainz",
        "qobuz",
        "spotify",
    )
    expected_hosts = {
        "open.spotify.com",
        "spotify.link",
        "deezer.com",
        "www.deezer.com",
        "deezer.page.link",
        "tidal.com",
        "listen.tidal.com",
        "qobuz.com",
        "open.qobuz.com",
        "play.qobuz.com",
        "music.apple.com",
        "music.youtube.com",
        "soundcloud.com",
        "on.soundcloud.com",
        "jiosaavn.com",
        "www.jiosaavn.com",
    }
    assert expected_hosts.issubset(SUPPORTED_SOURCE_HOSTS)


def test_search_result_to_dict_and_to_track_record():
    res = SearchResult(
        source="deezer",
        track_id="123",
        title="One More Time",
        artist="Daft Punk",
        album="Discovery",
        duration_seconds=320,
        url="https://www.deezer.com/track/123",
        preview_url="https://cdns-preview.deezer.com/123.mp3",
        cover_url="https://e-cdns-images.dzcdn.net/cover.jpg",
        isrc="GBDUW0000053",
        quality_hint="FLAC",
        extra={"rank": 999},
    )
    data = res.to_dict()
    assert data["source"] == "deezer"
    assert data["isrc"] == "GBDUW0000053"
    assert data["extra"] == {"rank": 999}
    data["extra"]["rank"] = 1
    assert res.extra["rank"] == 999

    record = res.to_track_record()
    assert record.url == "https://www.deezer.com/track/123"
    assert record.artist == "Daft Punk"
    assert record.title == "One More Time"
    assert record.raw_row["isrc"] == "GBDUW0000053"

    no_url = SearchResult(
        source="itunes",
        track_id="456",
        title="Harder, Better, Faster, Stronger",
        artist="Daft Punk",
        url="",
        preview_url="https://audio-ssl.itunes.apple.com/preview.m4a",
    )
    assert no_url.to_track_record(use_preview_if_no_url=False).url == ""
    assert (
        no_url.to_track_record(use_preview_if_no_url=True).url
        == "https://audio-ssl.itunes.apple.com/preview.m4a"
    )


def test_search_results_to_csv_is_rfc4180_and_parseable():
    results = [
        SearchResult(
            source="deezer",
            track_id="1",
            title='Song, "Special"',
            artist="Artist A",
            album="Album One",
            duration_seconds=210,
            url="https://www.deezer.com/track/1",
            isrc="USABC1234567",
        ),
        SearchResult(
            source="itunes",
            track_id="2",
            title="Plain Song",
            artist="Artist B",
            url="https://music.apple.com/us/album/2",
        ),
    ]
    csv_text = search_results_to_csv(results)
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0] == [
        "url",
        "artist",
        "title",
        "album",
        "source",
        "isrc",
        "duration_seconds",
    ]
    assert rows[1] == [
        "https://www.deezer.com/track/1",
        "Artist A",
        'Song, "Special"',
        "Album One",
        "deezer",
        "USABC1234567",
        "210",
    ]
    assert rows[2][6] == ""

    parsed = parse_csv(csv_text)
    assert len(parsed) == 2
    assert parsed[0].title == 'Song, "Special"'
    assert parsed[1].url == "https://music.apple.com/us/album/2"


def test_deezer_provider_track_album_artist_and_errors():
    seen_paths: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/search/track":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 3135556,
                            "title": "Harder, Better, Faster, Stronger",
                            "duration": 224,
                            "link": "https://www.deezer.com/track/3135556",
                            "preview": "https://cdns.dzcdn.net/preview.mp3",
                            "isrc": "GBDUW0000059",
                            "rank": 950000,
                            "artist": {"name": "Daft Punk"},
                            "album": {
                                "title": "Discovery",
                                "cover_xl": "https://e-cdns.dzcdn.net/cover_xl.jpg",
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/search/album":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 302127,
                            "title": "Discovery",
                            "link": "https://www.deezer.com/album/302127",
                            "cover_big": "https://e-cdns.dzcdn.net/album_big.jpg",
                            "artist": {"name": "Daft Punk"},
                        }
                    ]
                },
            )
        if request.url.path == "/search/artist":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 27,
                            "name": "Daft Punk",
                            "link": "https://www.deezer.com/artist/27",
                            "picture_medium": "https://e-cdns.dzcdn.net/artist.jpg",
                        }
                    ]
                },
            )
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={"error": {"type": "QuotaException", "message": "Quota limit", "code": 4}},
            )
        return httpx.Response(404)

    provider = DeezerSearchProvider()
    assert provider.is_configured() is True
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert provider.search("   ", client=client) == []
        assert provider.search("daft punk", limit=0, client=client) == []

        tracks = provider.search("daft punk", limit=5, search_type="track", client=client)
        assert len(tracks) == 1
        assert tracks[0].title == "Harder, Better, Faster, Stronger"
        assert tracks[0].artist == "Daft Punk"
        assert tracks[0].album == "Discovery"
        assert tracks[0].duration_seconds == 224
        assert tracks[0].isrc == "GBDUW0000059"
        assert tracks[0].cover_url == "https://e-cdns.dzcdn.net/cover_xl.jpg"

        albums = provider.search("discovery", search_type="album", client=client)
        assert albums[0].album == "Discovery"
        assert albums[0].url == "https://www.deezer.com/album/302127"

        artists = provider.search("daft punk", search_type="artist", client=client)
        assert artists[0].artist == "Daft Punk"

        with pytest.raises(SearchProviderError, match="Quota limit"):
            provider.search("daft punk", search_type="all", client=client)

    assert seen_paths == ["/search/track", "/search/album", "/search/artist", "/search"]


def test_itunes_provider_and_apple_alias():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "itunes.apple.com"
        assert request.url.params["media"] == "music"
        entity = request.url.params["entity"]
        if entity == "song":
            return httpx.Response(
                200,
                json={
                    "resultCount": 1,
                    "results": [
                        {
                            "trackId": 697195787,
                            "trackName": "Get Lucky",
                            "artistName": "Daft Punk",
                            "collectionName": "Random Access Memories",
                            "trackTimeMillis": 369626,
                            "trackViewUrl": "https://music.apple.com/us/album/get-lucky/697195787",
                            "previewUrl": "https://audio-ssl.itunes.apple.com/get-lucky.m4a",
                            "artworkUrl100": "https://is1-ssl.mzstatic.com/100x100bb.jpg",
                            "primaryGenreName": "Electronic",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"resultCount": 0, "results": []})

    provider = get_search_provider("apple")
    assert isinstance(provider, ItunesSearchProvider)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = provider.search("get lucky", limit=3, client=client)
    assert len(results) == 1
    assert results[0].source == "itunes"
    assert results[0].duration_seconds == 369
    assert results[0].preview_url == "https://audio-ssl.itunes.apple.com/get-lucky.m4a"
    assert results[0].cover_url == "https://is1-ssl.mzstatic.com/100x100bb.jpg"


def test_musicbrainz_provider_user_agent_and_artist_credit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "Songetter/0.1.0"
        assert request.url.params["fmt"] == "json"
        if request.url.path == "/ws/2/recording":
            return httpx.Response(
                200,
                json={
                    "recordings": [
                        {
                            "id": "mbid-rec-1",
                            "title": "Digital Love",
                            "length": 301000,
                            "score": 100,
                            "artist-credit": [
                                {"name": "Daft Punk", "joinphrase": " feat. "},
                                {"artist": {"name": "Romanthony"}, "joinphrase": ""},
                            ],
                            "releases": [{"title": "Discovery"}],
                            "isrcs": ["GBDUW0000060"],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"releases": []})

    provider = MusicBrainzSearchProvider()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = provider.search("digital love", client=client)
    assert len(results) == 1
    r = results[0]
    assert r.track_id == "mbid-rec-1"
    assert r.artist == "Daft Punk feat. Romanthony"
    assert r.album == "Discovery"
    assert r.duration_seconds == 301
    assert r.isrc == "GBDUW0000060"
    assert r.url == "https://musicbrainz.org/recording/mbid-rec-1"


def test_tidal_provider_token_oauth2_v2_and_v1_base_url(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    provider = TidalSearchProvider()
    assert provider.is_configured() is False
    with pytest.raises(SearchProviderError, match="not configured"):
        provider.search("around the world")

    # 1. OAuth2 client credentials + OpenAPI v2 JSON:API + custom countryCode
    monkeypatch.setenv("TIDAL_CLIENT_ID", "tidal-cid")
    monkeypatch.setenv("TIDAL_CLIENT_SECRET", "tidal-csec")
    monkeypatch.setenv("SONGETTER_TIDAL_COUNTRY", "BR")
    assert provider.is_configured() is True

    token_calls = 0

    def v2_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if str(request.url) == "https://auth.tidal.com/v1/oauth2/token":
            token_calls += 1
            assert request.headers["Authorization"].startswith("Basic ")
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(
                200, json={"access_token": "tidal-oauth-tok", "expires_in": 3600}
            )
        assert request.url.host == "openapi.tidal.com"
        assert request.url.path == "/v2/searchResults/around the world"
        assert "/v2/searchResults/around%20the%20world" in str(request.url)
        assert request.url.params["countryCode"] == "BR"
        assert request.headers["Authorization"] == "Bearer tidal-oauth-tok"
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "around the world",
                    "type": "searchResults",
                    "relationships": {
                        "tracks": {"data": [{"id": "77610", "type": "tracks"}]}
                    },
                },
                "included": [
                    {
                        "id": "77610",
                        "type": "tracks",
                        "attributes": {
                            "title": "Around the World",
                            "isrc": "GBDUW9700001",
                            "duration": "PT7M9S",
                            "mediaTags": ["LOSSLESS"],
                            "externalLinks": [{"href": "https://tidal.com/browse/track/77610"}],
                        },
                        "relationships": {
                            "artists": {"data": [{"id": "146", "type": "artists"}]},
                            "albums": {"data": [{"id": "77600", "type": "albums"}]},
                        },
                    },
                    {
                        "id": "146",
                        "type": "artists",
                        "attributes": {"name": "Daft Punk"},
                    },
                    {
                        "id": "77600",
                        "type": "albums",
                        "attributes": {
                            "title": "Homework",
                            "imageLinks": [{"href": "https://resources.tidal.com/hw.jpg"}],
                        },
                    },
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(v2_handler)) as client:
        first = provider.search("around the world", client=client)
        second = provider.search("around the world", client=client)

    assert token_calls == 1  # OAuth2 token cached across searches on same client
    assert len(first) == 1 and len(second) == 1
    assert first[0].title == "Around the World"
    assert first[0].artist == "Daft Punk"
    assert first[0].album == "Homework"
    assert first[0].duration_seconds == 429
    assert first[0].isrc == "GBDUW9700001"
    assert first[0].quality_hint == "LOSSLESS"
    assert first[0].cover_url == "https://resources.tidal.com/hw.jpg"
    assert first[0].url == "https://tidal.com/browse/track/77610"

    # 2. Direct token + v1 API URL
    monkeypatch.setenv("SONGETTER_TIDAL_TOKEN", "direct-tidal-tok")
    monkeypatch.setenv("SONGETTER_TIDAL_API_URL", "https://api.tidal.com")

    def v1_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tidal.com"
        assert request.url.path == "/v1/search/tracks"
        assert request.url.params["countryCode"] == "BR"
        assert request.headers["Authorization"] == "Bearer direct-tidal-tok"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 9911,
                        "title": "Da Funk",
                        "duration": 328,
                        "isrc": "GBDUW9600001",
                        "audioQuality": "HI_RES_LOSSLESS",
                        "url": "https://tidal.com/browse/track/9911",
                        "artists": [{"name": "Daft Punk"}],
                        "album": {"title": "Homework", "cover": "aa-bb-cc"},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(v1_handler)) as client:
        v1_results = provider.search("da funk", client=client)

    assert len(v1_results) == 1
    assert v1_results[0].duration_seconds == 328
    assert v1_results[0].quality_hint == "HI_RES_LOSSLESS"
    assert v1_results[0].cover_url == "https://resources.tidal.com/images/aa/bb/cc/640x640.jpg"


def test_qobuz_provider_auth_and_hires_metadata(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    provider = QobuzSearchProvider()
    assert provider.is_configured() is False
    with pytest.raises(SearchProviderError, match="not configured"):
        provider.search("veridis quo")

    monkeypatch.setenv("SONGETTER_QOBUZ_APP_ID", "app-42")
    monkeypatch.setenv("SONGETTER_QOBUZ_TOKEN", "qobuz-secret")
    assert provider.is_configured() is True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api.json/0.2/track/search"
        assert request.url.params["app_id"] == "app-42"
        assert request.headers["X-App-Id"] == "app-42"
        assert request.headers["X-User-Auth-Token"] == "qobuz-secret"
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": 55501,
                            "title": "Veridis Quo",
                            "duration": 345,
                            "isrc": "GBDUW0000064",
                            "maximum_bit_depth": 24,
                            "maximum_sampling_rate": 96,
                            "hires": True,
                            "performer": {"name": "Daft Punk"},
                            "album": {
                                "title": "Discovery",
                                "image": {"large": "https://static.qobuz.com/cover.jpg"},
                            },
                        }
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = provider.search("veridis quo", client=client)
    assert len(results) == 1
    assert results[0].quality_hint == "24-bit/96kHz"
    assert results[0].url == "https://open.qobuz.com/track/55501"
    assert results[0].isrc == "GBDUW0000064"


def test_spotify_provider_oauth2_and_direct_token(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    provider = SpotifySearchProvider()
    assert provider.is_configured() is False
    with pytest.raises(SearchProviderError, match="not configured"):
        provider.search("instant crush")

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "sp-cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "sp-csec")
    assert provider.is_configured() is True

    token_requests = 0

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if str(request.url) == "https://accounts.spotify.com/api/token":
            token_requests += 1
            return httpx.Response(
                200, json={"access_token": "sp-access-1", "expires_in": 3600}
            )
        assert str(request.url).startswith("https://api.spotify.com/v1/search")
        assert request.headers["Authorization"] == "Bearer sp-access-1"
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "sp123",
                            "name": "Instant Crush",
                            "duration_ms": 337000,
                            "preview_url": "https://p.scdn.co/mp3-preview/sp123",
                            "external_urls": {"spotify": "https://open.spotify.com/track/sp123"},
                            "external_ids": {"isrc": "USQX91300108"},
                            "artists": [{"name": "Daft Punk"}, {"name": "Julian Casablancas"}],
                            "album": {
                                "name": "Random Access Memories",
                                "images": [{"url": "https://i.scdn.co/image/ram.jpg"}],
                            },
                        }
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(oauth_handler)) as client:
        res1 = provider.search("instant crush", client=client)
        res2 = provider.search("instant crush", client=client)

    assert token_requests == 1
    assert res1[0].artist == "Daft Punk, Julian Casablancas"
    assert res1[0].duration_seconds == 337
    assert res1[0].isrc == "USQX91300108"
    assert res2[0].url == "https://open.spotify.com/track/sp123"

    # Direct token bypasses OAuth2 endpoint
    monkeypatch.setenv("SONGETTER_SPOTIFY_TOKEN", "sp-direct")

    def direct_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.spotify.com"
        assert request.headers["Authorization"] == "Bearer sp-direct"
        return httpx.Response(200, json={"tracks": {"items": []}})

    with httpx.Client(transport=httpx.MockTransport(direct_handler)) as client:
        assert provider.search("nothing", client=client) == []


def test_list_search_sources_and_unsupported_source(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    sources = {entry["id"]: entry for entry in list_search_sources()}
    assert list(sources.keys())[:6] == list(SUPPORTED_SEARCH_SOURCES)
    assert sources["deezer"]["configured"] is True
    assert sources["deezer"]["requires_auth"] is False
    assert sources["itunes"]["aliases"] == ["apple"]
    assert sources["spotify"]["requires_auth"] is True
    assert sources["spotify"]["configured"] is False

    monkeypatch.setenv("SONGETTER_SPOTIFY_TOKEN", "tok")
    updated = {entry["id"]: entry for entry in list_search_sources()}
    assert updated["spotify"]["configured"] is True

    with pytest.raises(UnsupportedSearchSourceError):
        get_search_provider("unknown-source")


def test_federated_search_all_deduplication_and_error_isolation(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)

    def multi_handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.deezer.com":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 1,
                            "title": "One More Time",
                            "duration": 320,
                            "link": "https://www.deezer.com/track/1",
                            "isrc": "GBDUW0000053",
                            "artist": {"name": "Daft Punk"},
                            "album": {"title": "Discovery"},
                        },
                        {
                            "id": 2,
                            "title": "Aerodynamic",
                            "duration": 212,
                            "link": "https://www.deezer.com/track/2",
                            "artist": {"name": "Daft Punk"},
                            "album": {"title": "Discovery"},
                        },
                    ]
                },
            )
        if host == "itunes.apple.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "trackId": 100,
                            "trackName": "One More Time!",
                            "artistName": "Daft Punk",
                            "collectionName": "Discovery",
                            "trackTimeMillis": 320000,
                            "trackViewUrl": "https://music.apple.com/us/album/100",
                        },
                        {
                            "trackId": 101,
                            "trackName": "Voyager",
                            "artistName": "Daft Punk",
                            "collectionName": "Discovery",
                            "trackTimeMillis": 227000,
                            "trackViewUrl": "https://music.apple.com/us/album/101",
                        },
                    ]
                },
            )
        if host == "musicbrainz.org":
            # Simulate failure in one provider during federated search
            return httpx.Response(503)
        raise AssertionError(f"Unexpected unconfigured host called: {host}")

    with httpx.Client(transport=httpx.MockTransport(multi_handler)) as client:
        # sources="all" only calls configured providers (deezer, itunes, musicbrainz)
        raw_results, errors = search_tracks_with_errors(
            "daft punk",
            sources="all",
            deduplicate=False,
            http_client=client,
            raise_on_error=False,
        )
        assert len(raw_results) == 4
        assert "musicbrainz" in errors
        assert "503" in errors["musicbrainz"]

        deduped = search_tracks(
            "daft punk",
            sources="all",
            deduplicate=True,
            http_client=client,
            raise_on_error=False,
        )
        # "One More Time!" from iTunes deduplicated against Deezer's "One More Time"
        assert [r.title for r in deduped] == ["One More Time", "Aerodynamic", "Voyager"]

        with pytest.raises(SearchProviderError):
            search_tracks(
                "daft punk",
                sources="all",
                http_client=client,
                raise_on_error=True,
            )


def test_deduplication_by_isrc_even_with_different_title_suffixes(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("SONGETTER_SPOTIFY_TOKEN", "sp-tok")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.deezer.com":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 10,
                            "title": "Around the World",
                            "isrc": "GB-DUW-97-00001",
                            "link": "https://www.deezer.com/track/10",
                            "artist": {"name": "Daft Punk"},
                        }
                    ]
                },
            )
        if request.url.host == "api.spotify.com":
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "id": "sp10",
                                "name": "Around the World (2022 Remaster)",
                                "external_ids": {"isrc": "GBDUW9700001"},
                                "external_urls": {"spotify": "https://open.spotify.com/track/sp10"},
                                "artists": [{"name": "Daft Punk"}],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        deduped = search_tracks(
            "around the world",
            sources=["deezer", "spotify"],
            deduplicate=True,
            http_client=client,
        )
    assert len(deduped) == 1
    assert deduped[0].source == "deezer"


def test_custom_provider_registration_and_concurrent_all_execution(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    original_providers = dict(search_mod._PROVIDERS)
    original_aliases = dict(search_mod._ALIASES)
    thread_ids: List[int] = []

    class CustomSyncProvider(SearchProvider):
        name = "custom_one"
        display_name = "Custom One"
        requires_auth = False
        description = "Test custom provider"

        def search(self, query: str, limit: int = 10, search_type: str = "track", client=None):
            thread_ids.append(threading.get_ident())
            return [
                SearchResult(
                    source=self.name,
                    track_id="c1",
                    title=f"{query} Track",
                    artist="Custom Artist",
                    url="https://example.test/c1",
                )
            ]

    class MinimalDuckProvider:
        name = "custom_two"
        display_name = "Custom Two"
        requires_auth = False
        description = "Minimal provider without client arg"

        def is_configured(self) -> bool:
            return True

        def search(self, query: str, limit: int = 10, search_type: str = "track"):
            thread_ids.append(threading.get_ident())
            return [
                SearchResult(
                    source=self.name,
                    track_id="c2",
                    title=f"{query} Track 2",
                    artist="Custom Artist 2",
                    url="https://example.test/c2",
                )
            ]

    try:
        register_search_provider("custom_one", CustomSyncProvider(), aliases=("c1",))
        register_search_provider("custom_two", MinimalDuckProvider())

        assert isinstance(get_search_provider("c1"), CustomSyncProvider)
        results = search_tracks(
            "demo",
            sources=["c1", "custom_two"],
            http_client=None,
        )
        assert [r.track_id for r in results] == ["c1", "c2"]
        assert len(thread_ids) == 2

        # Request error wrapping check
        def failing_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        with httpx.Client(transport=httpx.MockTransport(failing_transport)) as failing_client:
            res, errs = search_tracks_with_errors(
                "demo",
                sources=["deezer"],
                http_client=failing_client,
                raise_on_error=False,
            )
            assert res == []
            assert "deezer" in errs
    finally:
        with search_mod._REGISTRY_LOCK:
            search_mod._PROVIDERS.clear()
            search_mod._PROVIDERS.update(original_providers)
            search_mod._ALIASES.clear()
            search_mod._ALIASES.update(original_aliases)


def test_error_handling_and_validation_edge_cases(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    deezer = DeezerSearchProvider()

    def bad_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    with httpx.Client(transport=httpx.MockTransport(bad_json_handler)) as client:
        with pytest.raises(SearchProviderError, match="invalid JSON"):
            deezer.search("test", client=client)

    def server_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with httpx.Client(transport=httpx.MockTransport(server_error_handler)) as client:
        with pytest.raises(SearchProviderError, match="HTTP 500"):
            deezer.search("test", client=client)

    with pytest.raises(SearchProviderError, match="Unsupported search_type"):
        deezer.search("test", search_type="podcast")

    with pytest.raises(ValueError, match="cannot be empty"):
        register_search_provider("", deezer)

    with pytest.raises(TypeError, match="callable search"):
        register_search_provider("invalid", object())  # type: ignore[arg-type]

    with pytest.raises(UnsupportedSearchSourceError):
        search_tracks("test", sources="")

    assert search_tracks("test", sources=[]) == []


def test_album_and_artist_modes_across_musicbrainz_qobuz_spotify(monkeypatch: pytest.MonkeyPatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("SONGETTER_QOBUZ_APP_ID", "q-app")
    monkeypatch.setenv("SONGETTER_SPOTIFY_TOKEN", "sp-tok")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "musicbrainz.org":
            if request.url.path == "/ws/2/release":
                return httpx.Response(
                    200,
                    json={
                        "releases": [
                            {
                                "id": "rel-1",
                                "title": "Discovery",
                                "artist-credit": [{"name": "Daft Punk"}],
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"artists": [{"id": "art-1", "name": "Daft Punk"}]})
        if request.url.host == "www.qobuz.com":
            return httpx.Response(
                200,
                json={
                    "albums": {
                        "items": [
                            {
                                "id": "q-alb-1",
                                "title": "Discovery",
                                "hires_streamable": True,
                                "artist": {"name": "Daft Punk"},
                            }
                        ]
                    }
                },
            )
        if request.url.host == "api.spotify.com":
            return httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [
                            {
                                "id": "sp-art-1",
                                "name": "Daft Punk",
                                "images": [{"url": "https://i.scdn.co/daft.jpg"}],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mb = MusicBrainzSearchProvider()
        assert mb.search("Discovery", search_type="album", client=client)[0].album == "Discovery"
        assert mb.search("Daft Punk", search_type="artist", client=client)[0].artist == "Daft Punk"

        qb = QobuzSearchProvider()
        qb_album = qb.search("Discovery", search_type="album", client=client)[0]
        assert qb_album.quality_hint == "Hi-Res"
        assert qb_album.url == "https://open.qobuz.com/album/q-alb-1"

        sp = SpotifySearchProvider()
        sp_artist = sp.search("Daft Punk", search_type="artist", client=client)[0]
        assert sp_artist.cover_url == "https://i.scdn.co/daft.jpg"
        assert sp_artist.url == "https://open.spotify.com/artist/sp-art-1"

