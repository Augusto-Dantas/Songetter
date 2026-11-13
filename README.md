# Songetter

Songetter is a Python application for batch-processing Spotify links with [Mornye-Python](https://github.com/mornyeapp/Mornye-Python) and providers from the [Mornye Extension Store](https://github.com/mornyeapp/Mornye-Extension).

## Integrated providers

Songetter automatically adds the unified catalog without removing registries you have already configured:

`https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry-unified.json`

Default providers (fallback priority): `ext:tidal-web`, `ext:qobuz-web`, `ext:deezer`, `ext:amazon`, `ext:ytmusic-spotiflac`, `ext:soundcloud`, and `ext:jiosaavn`.

The catalog also includes `utilities`, a Python package providing metadata, session, and browser-resolution utilities. The unified catalog distinguishes Python and JavaScript runtimes; Mornye selects compatible packages and installs the required providers from the registry.

`beatport` and `jellyfin` are not included in the default list because they may require dedicated infrastructure, an account, or service-specific configuration. They can be selected manually once configured. Extensions are third-party code: review manifests, permissions, requirements, licenses, and service terms before installing them.

## Requirements

- Python 3.11+
- `mornye>=2.0.6` (installed automatically by `pip install -e .`)
- Node.js for JavaScript-based providers

The first run accesses the registry. Installing extensions may require an internet connection and provider-specific setup.

## Installation

```bash
git clone https://github.com/Augusto-Dantas/Songetter.git
cd Songetter
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

## Web application

```bash
streamlit run app.py
```

Open `http://localhost:8501`, paste your Spotify links, adjust the provider list or priority order, and choose the maximum number of tracks downloaded simultaneously (1–8). The web app streams Mornye's live stderr progress, measures the growth of audio files in the destination folder to show current throughput, and estimates remaining time from observed track completion rate. The ETA is marked as calculating until enough track completions are available. It also surfaces per-track provider failures, process startup errors, timeouts, invalid/missing JSON reports, and non-zero process exits. Providers are tried in the configured priority order, one provider per Mornye run. When a provider reports a verification pause, rate limit, HTTP 429, or explicit cooldown, Songetter stops that provider process, records its retry window in `~/.cache/songetter/provider_cooldowns.json` (or `$XDG_CACHE_HOME/songetter/provider_cooldowns.json`), and moves to the next provider. Future runs skip providers until their recorded cooldown expires. Mornye indexes the destination and skips tracks already downloaded by an earlier provider, allowing later providers to fill the remaining gaps. Other failures such as `not_found` do not open the circuit; the next provider is tried for remaining tracks.

## Command-line interface

```bash
songetter <spotify-link>
songetter --input links.txt --output ./Music
songetter --input links.txt --max-concurrent 4
songetter --input links.txt --service ext:tidal-web --service ext:qobuz-web
```

The `--max-concurrent` option controls how many tracks Mornye downloads at the same time (default: 2). Use `--max-concurrent 1` for sequential track downloads. The provider-selection example replaces the default provider list. Additional registries can be configured through `MORNYE_REGISTRIES` (comma-separated); existing entries are preserved.

Supported Spotify links include tracks, albums, playlists, and artist pages. Localized URLs such as `https://open.spotify.com/intl-en/artist/...` are normalized automatically.

## Output

Mornye handles tags and artwork, embeds lyrics directly into track metadata when available (no `.lrc` sidecar files are created), and Songetter exports an overall `.m3u8` playlist using relative paths. Tracks within each Spotify link are downloaded concurrently by Mornye; links are processed sequentially, and the output playlist is assembled after all links finish.

For server-side library organization, `songetter.organizer` and `songetter.deduplicator` flatten libraries to `{Artist} - {Title}.{ext}` and retain the highest-quality copy per track. See `NEXT_STEPS.md` for the ongoing server-side CSV ingestion and FastAPI roadmap, and `Dockerfile` for containerized execution.

Only download content you are authorized to access, and comply with the terms of each service.

## Tests

```bash
pip install pytest
pytest
```

## License

MIT. See also the licenses of the providers and extensions you use.
