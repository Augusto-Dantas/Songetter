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

Open `http://localhost:8501`, paste your Spotify links, adjust the provider list or priority order, and choose the number of concurrent downloads (1–8). The progress bar updates whenever a link finishes.

## Command-line interface

```bash
songetter <spotify-link>
songetter --input links.txt --output ./Music
songetter --input links.txt --workers 4
songetter --input links.txt --service ext:tidal-web --service ext:qobuz-web
```

The `--workers` option controls how many Spotify links are processed concurrently (default: 3). Use `--workers 1` to disable concurrency. The provider-selection example replaces the default provider list. Additional registries can be configured through `MORNYE_REGISTRIES` (comma-separated); existing entries are preserved.

Supported Spotify links include tracks, albums, playlists, and artist pages. Localized URLs such as `https://open.spotify.com/intl-en/artist/...` are normalized automatically.

## Output

The library is organized into artist and album subfolders. Mornye handles tags and artwork, downloads lyrics when available, and Songetter exports an overall `.m3u8` playlist using relative paths. Multiple input links are processed concurrently; the output playlist is assembled after all workers finish.

Only download content you are authorized to access, and comply with the terms of each service.

## Tests

```bash
pip install pytest
pytest
```

## License

MIT. See also the licenses of the providers and extensions you use.
