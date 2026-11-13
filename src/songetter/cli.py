from __future__ import annotations
import argparse
from pathlib import Path
from .core import DEFAULT_SERVICES, configure_mornye_registry, parse_links, run_download, write_m3u8

def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-process Spotify links with Mornye providers.")
    parser.add_argument("links", nargs="*", help="Spotify track, album, playlist, or artist links")
    parser.add_argument("-i", "--input", help="Text file containing one link per line")
    parser.add_argument("-o", "--output", default="Music", help="Music library destination folder")
    parser.add_argument("--service", action="append", default=None,
                        help="Mornye extension ID; replaces the default list (repeatable)")
    parser.add_argument("--max-concurrent", type=int, default=2,
                        help="Number of tracks Mornye downloads simultaneously (default: 2)")
    parser.add_argument("--m3u", default="Songetter.m3u8", help="M3U8 playlist filename")
    args = parser.parse_args()
    if args.max_concurrent < 1:
        parser.error("--max-concurrent must be at least 1")

    raw = "\n".join(args.links)
    if args.input:
        raw += "\n" + Path(args.input).read_text(encoding="utf-8")
    links = parse_links(raw)
    output = Path(args.output).expanduser().resolve()
    services = args.service if args.service is not None else DEFAULT_SERVICES
    configure_mornye_registry()
    all_tracks, totals = [], {"downloaded": 0, "skipped": 0, "failed": 0}

    for index, url in enumerate(links, 1):
        print(f"[{index}/{len(links)}] Processing {url}")
        result = run_download(url, output, services, max_concurrent_downloads=args.max_concurrent)
        all_tracks.extend(result.tracks)
        for key in totals:
            totals[key] += getattr(result, key)
        if result.error:
            print(f"  ERROR: {result.error}")
        print(f"  downloaded={result.downloaded} existing={result.skipped} failed={result.failed}")

    playlist = output / args.m3u
    entries = write_m3u8(all_tracks, playlist, output)
    print(f"\nCompleted: {totals}")
    print(f"Providers: {', '.join(services)}")
    print(f"Concurrent track downloads: {args.max_concurrent}")
    print(f"Playlist: {playlist} ({entries} tracks)")

if __name__ == "__main__":
    main()
