from __future__ import annotations
from pathlib import Path
import streamlit as st
from songetter.core import DEFAULT_SERVICES, MORNYE_REGISTRY, configure_mornye_registry, parse_links, run_download, write_m3u8

st.set_page_config(page_title="Songetter", page_icon="♫", layout="wide")
st.title("Songetter")
st.caption("Batch music library downloader powered by Python and Mornye.")

with st.sidebar:
    st.subheader("Library")
    output_value = st.text_input("Destination folder", value="./Music")
    services_value = st.text_area(
        "Mornye providers (priority order)",
        value=", ".join(DEFAULT_SERVICES),
        help="Mornye installs the required providers from the integrated catalog. Remove or reorder IDs as needed.",
    )
    lyrics_value = st.text_input("Lyrics providers", value="apple,lrclib")
    concurrency_value = st.slider("Concurrent track downloads", min_value=1, max_value=8, value=2, help="Maximum number of tracks Mornye downloads at the same time.")
    st.caption("Registry catalog: " + MORNYE_REGISTRY)
    st.info(
        "Songetter preserves existing registries and adds the Mornye catalog. "
        "Extensions are third-party code; review their requirements and permissions."
    )

st.subheader("Spotify links")
raw_links = st.text_area(
    "Paste Spotify track, album, playlist, or artist links (one per line)",
    height=220,
    placeholder="https://open.spotify.com/playlist/...\nhttps://open.spotify.com/artist/...",
)
start = st.button("Start processing", type="primary", use_container_width=True)

if start:
    try:
        links = parse_links(raw_links)
        configure_mornye_registry()
        output = Path(output_value).expanduser().resolve()
        services = [item.strip() for item in services_value.replace("\n", ",").split(",") if item.strip()]
        lyrics = [item.strip() for item in lyrics_value.split(",") if item.strip()]
        if not services:
            raise ValueError("Select at least one Mornye provider.")

        all_tracks, totals = [], {"downloaded": 0, "skipped": 0, "failed": 0}
        progress = st.progress(0, text="Starting Mornye...")
        status = st.empty()
        live_log = st.empty()
        progress_lines = []

        def show_progress(message: str) -> None:
            progress_lines.append(message)
            del progress_lines[:-8]
            live_log.code("\\n".join(progress_lines), language="text")
            track_match = __import__("re").search(r"Track \\[(\\d+)/(\\d+)\\]", message)
            if track_match:
                current, total = map(int, track_match.groups())
                if total:
                    progress.progress(min(current / total, 1.0), text=f"Processing track {current} of {total}")
            elif message.startswith("[RUN]"):
                progress.progress(0, text="Mornye is resolving the Spotify link...")
        for index, url in enumerate(links, 1):
            status.write(f"Processing link {index} of {len(links)}: {url}")
            result = run_download(
                url,
                output,
                services,
                lyrics,
                max_concurrent_downloads=concurrency_value,
                progress_callback=show_progress,
            )
            all_tracks.extend(result.tracks)
            for key in totals:
                totals[key] += getattr(result, key)
            if result.error:
                st.error(f"{url}: {result.error}")
            if index == len(links):
                progress.progress(1.0, text=f"Completed {index} of {len(links)} links")

        playlist = output / "Songetter.m3u8"
        count = write_m3u8(all_tracks, playlist, output)
        status.success(
            f"Processing complete. Processed {len(links)} links with up to "
            f"{concurrency_value} tracks downloading simultaneously."
        )
        col1, col2, col3 = st.columns(3)
        col1.metric("Downloaded", totals["downloaded"])
        col2.metric("Already existing", totals["skipped"])
        col3.metric("Failed", totals["failed"])
        st.write(f"Playlist created: {playlist} — {count} tracks.")
        if all_tracks:
            st.dataframe(
                [
                    {
                        "Track": track.get("title"),
                        "Artist": track.get("artists"),
                        "Album": track.get("album"),
                        "Status": track.get("status"),
                        "File": track.get("file_path"),
                        "Error": track.get("error"),
                    }
                    for track in all_tracks
                ],
                use_container_width=True,
            )
    except Exception as exc:
        st.error(str(exc))

st.divider()
st.caption("Only download content you are authorized to access, and comply with each service's terms.")
