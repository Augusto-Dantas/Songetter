from __future__ import annotations

from pathlib import Path
import re
import streamlit as st

from songetter.core import (
    DEFAULT_SERVICES,
    MORNYE_REGISTRY,
    configure_mornye_registry,
    parse_links,
    run_download,
    write_m3u8,
)


def format_bytes_per_second(value: float) -> str:
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    amount = max(0.0, float(value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GB/s"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Calculating..."
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


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
    concurrency_value = st.slider(
        "Concurrent track downloads", min_value=1, max_value=8, value=2,
        help="Maximum number of tracks Mornye downloads at the same time.",
    )
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

        all_tracks = []
        totals = {"downloaded": 0, "skipped": 0, "failed": 0}
        progress = st.progress(0, text="Starting Mornye...")
        status = st.empty()
        log_panel = st.empty()
        speed_metric, eta_metric, elapsed_metric = st.columns(3)
        speed_value = speed_metric.empty()
        eta_value = eta_metric.empty()
        elapsed_value = elapsed_metric.empty()
        progress_lines = []
        def show_progress(message: str) -> None:
            progress_lines.append(message)
            del progress_lines[:-12]
            log_panel.code("\n".join(progress_lines), language="text")
            match = re.search(r"Track\s+\[(\d+)/(\d+)\]", message)
            if match:
                current_track, current_total = map(int, match.groups())
                if current_total:
                    progress.progress(
                        min(current_track / current_total, 1.0),
                        text=f"Processing track {current_track} of {current_total}",
                    )
            elif message.startswith("[RUN]"):
                progress_match = re.search(r"\[RUN\]\s+(\d+)\s+track", message)
                if progress_match:
                    current_total = int(progress_match.group(1))
                progress.progress(0, text="Mornye is resolving the Spotify link...")
            if "verification paused" in message.lower() or "next try in" in message.lower():
                status.warning("A provider cooldown was detected. Songetter is stopping that provider and moving to the next one.")

        def show_metrics(metrics: dict) -> None:
            speed_value.metric("Download speed", format_bytes_per_second(metrics.get("speed_bps", 0)))
            eta_value.metric("Estimated time remaining", format_duration(metrics.get("eta_seconds")))
            elapsed_value.metric("Elapsed", format_duration(metrics.get("elapsed_seconds", 0)))

        for index, url in enumerate(links, 1):
            status.info(f"Processing link {index} of {len(links)}: {url}")
            try:
                result = run_download(
                    url,
                    output,
                    services,
                    lyrics,
                    max_concurrent_downloads=concurrency_value,
                    progress_callback=show_progress,
                    metrics_callback=show_metrics,
                )
            except Exception as exc:
                status.error(f"Could not process {url}: {type(exc).__name__}: {exc}")
                totals["failed"] += 1
                continue

            all_tracks.extend(result.tracks)
            for key in totals:
                totals[key] += getattr(result, key)

            for provider_result in result.provider_status:
                provider_name = provider_result.get("provider", "Unknown provider")
                if provider_result.get("status") == "cooldown":
                    remaining = int(provider_result.get("retry_after_seconds", 0) or 0)
                    minutes, seconds = divmod(max(0, remaining), 60)
                    st.warning(
                        f"{provider_name} is temporarily paused ({minutes}m {seconds}s remaining). "
                        "It has been skipped for this run and its cooldown is saved for future runs."
                    )
                elif provider_result.get("status") == "completed" and provider_result.get("failed", 0):
                    st.caption(
                        f"{provider_name}: {provider_result.get('failed')} track(s) still need another provider."
                    )

            if result.error:
                st.error(f"{url}: {result.error}")
            for track in result.tracks:
                if track.get("status") == "failed" or track.get("error"):
                    title = track.get("title") or "Unknown track"
                    detail = track.get("error") or track.get("message") or "The provider did not return a failure reason."
                    st.warning(f"{title}: {detail}")

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
                        "Error": track.get("error") or track.get("message"),
                    }
                    for track in all_tracks
                ],
                use_container_width=True,
            )
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")

st.divider()
st.caption("Only download content you are authorized to access, and comply with each service's terms.")
