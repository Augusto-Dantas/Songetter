from __future__ import annotations
import argparse
from pathlib import Path
from .core import DEFAULT_SERVICES, configure_mornye_registry, parse_links, run_download, write_m3u8

def main() -> None:
    parser = argparse.ArgumentParser(description="Processa listas de links Spotify em lote com os provedores Mornye.")
    parser.add_argument("links", nargs="*", help="Links Spotify (faixa, álbum, playlist ou artista)")
    parser.add_argument("-i", "--input", help="Arquivo de texto com um link por linha")
    parser.add_argument("-o", "--output", default="Music", help="Pasta da biblioteca")
    parser.add_argument("--service", action="append", default=None, help="ID de extensão Mornye; substitui a lista padrão (repetível)")
    parser.add_argument("--m3u", default="Songetter.m3u8", help="Nome do arquivo M3U8")
    args = parser.parse_args()
    raw = "\n".join(args.links)
    if args.input: raw += "\n" + Path(args.input).read_text(encoding="utf-8")
    links = parse_links(raw)
    output = Path(args.output).expanduser().resolve()
    services = args.service if args.service is not None else DEFAULT_SERVICES
    configure_mornye_registry()
    all_tracks, totals = [], {"downloaded": 0, "skipped": 0, "failed": 0}
    for index, url in enumerate(links, 1):
        print(f"[{index}/{len(links)}] Processando {url}")
        result = run_download(url, output, services)
        all_tracks.extend(result.tracks)
        for key in totals: totals[key] += getattr(result, key)
        if result.error: print(f"  ERRO: {result.error}")
        print(f"  baixadas={result.downloaded} existentes={result.skipped} falhas={result.failed}")
    playlist = output / args.m3u
    entries = write_m3u8(all_tracks, playlist, output)
    print(f"\nConcluído: {totals}")
    print(f"Provedores: {', '.join(services)}")
    print(f"Playlist: {playlist} ({entries} faixas)")

if __name__ == "__main__": main()
