from __future__ import annotations

import argparse
from pathlib import Path

from .core import parse_links, run_download, write_m3u8


def main() -> None:
    parser = argparse.ArgumentParser(description="Processa listas de links Spotify com Mornye.")
    parser.add_argument("links", nargs="*", help="Links Spotify (faixa, álbum ou playlist)")
    parser.add_argument("-i", "--input", help="Arquivo de texto com um link por linha")
    parser.add_argument("-o", "--output", default="Music", help="Pasta da biblioteca")
    parser.add_argument("--service", action="append", default=[], help="ID de extensão Mornye instalada (repetível)")
    parser.add_argument("--m3u", default="Songetter.m3u8", help="Nome do arquivo M3U8")
    args = parser.parse_args()

    raw_links = "\n".join(args.links)
    if args.input:
        raw_links += "\n" + Path(args.input).read_text(encoding="utf-8")
    links = parse_links(raw_links)
    output = Path(args.output).expanduser().resolve()
    all_tracks = []
    totals = {"downloaded": 0, "skipped": 0, "failed": 0}

    for index, url in enumerate(links, start=1):
        print(f"[{index}/{len(links)}] Processando {url}")
        result = run_download(url, output, args.service)
        all_tracks.extend(result.tracks)
        totals["downloaded"] += result.downloaded
        totals["skipped"] += result.skipped
        totals["failed"] += result.failed
        if result.error:
            print(f"  ERRO: {result.error}")
        print(f"  baixadas={result.downloaded} existentes={result.skipped} falhas={result.failed}")

    playlist = output / args.m3u
    entries = write_m3u8(all_tracks, playlist, output)
    print(f"\nConcluído: {totals}")
    print(f"Playlist: {playlist} ({entries} faixas)")


if __name__ == "__main__":
    main()
