from __future__ import annotations
import argparse
from pathlib import Path
from .core import DEFAULT_SERVICES, configure_mornye_registry, parse_links, run_downloads, write_m3u8

def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-process Spotify links with Mornye providers.")
    parser.add_argument("links", nargs="*", help="Spotify track, album, playlist, or artist links")
    parser.add_argument("-i", "--input", help="Text file containing one link per line")
    parser.add_argument("-o", "--output", default="Music", help="Music library destination folder")
    parser.add_argument("--service", action="append", default=None,
                        help="Mornye extension ID; replaces the default list (repeatable)")
    parser.add_argument("--workers", type=int, default=3,
                        help="Number of Spotify links to process concurrently (default: 3)")
    parser.add_argument("--m3u", default="Songetter.m3u8", help="M3U8 playlist filename")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    raw = "\n".join(args.links)
    if args.input:
        raw += "\n" + Path(args.input).read_text(encoding="utf-8")
    links = parse_links(raw)
    output = Path(args.output).expanduser().resolve()
    services = args.service if args.service is not None else DEFAULT_SERVICES
    configure_mornye_registry()

    def report_progress(completed: int, total: int, result) -> None:
        print(f"[{completed}/{total}] Finished {result.url}")
        if result.error:
            print(f"  ERROR: {result.error}")
        print(f"  downloaded={result.downloaded} existing={result.skipped} failed={result.failed}")

    results = run_downloads(links, output, services, max_workers=args.workers,
                            on_complete=report_progress)
    all_tracks = [track for result in results for track in result.tracks]
    totals = {
        "downloaded": sum(result.downloaded for result in results),
        "skipped": sum(result.skipped for result in results),
        "failed": sum(result.failed for result in results),
    }
    playlist = output / args.m3u
    entries = write_m3u8(all_tracks, playlist, output)
    print(f"\nCompleted: {totals}")
    print(f"Providers: {', '.join(services)}")
    print(f"Workers: {args.workers}")
    print(f"Playlist: {playlist} ({entries} tracks)")

if __name__ == "__main__":
    main()
