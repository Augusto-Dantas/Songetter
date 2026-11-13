from __future__ import annotations
from pathlib import Path
import streamlit as st
from songetter.core import DEFAULT_SERVICES, MORNYE_REGISTRY, configure_mornye_registry, parse_links, run_download, write_m3u8

st.set_page_config(page_title="Songetter", page_icon="♫", layout="wide")
st.title("Songetter")
st.caption("Gerenciador de downloads em lote para sua biblioteca musical — Python + Mornye.")
with st.sidebar:
    st.subheader("Biblioteca")
    output_value = st.text_input("Pasta de destino", value="./Music")
    services_value = st.text_area("Provedores Mornye (ordem de prioridade)", value=", ".join(DEFAULT_SERVICES),
        help="O Mornye instala os provedores necessários a partir do catálogo integrado. Remova ou reordene os IDs se quiser.")
    lyrics_value = st.text_input("Provedores de letras", value="apple,lrclib")
    st.caption("Catálogo: " + MORNYE_REGISTRY)
    st.info("O Songetter preserva registries existentes e adiciona o catálogo Mornye. Extensões são código de terceiros; revise requisitos e permissões.")
st.subheader("Links do Spotify")
raw_links = st.text_area("Cole links de faixas, álbuns, playlists ou artistas (um por linha)", height=220,
    placeholder="https://open.spotify.com/playlist/...\nhttps://open.spotify.com/artist/...")
start = st.button("Iniciar processamento", type="primary", use_container_width=True)
if start:
    try:
        links = parse_links(raw_links)
        configure_mornye_registry()
        output = Path(output_value).expanduser().resolve()
        services = [x.strip() for x in services_value.replace("\n", ",").split(",") if x.strip()]
        lyrics = [x.strip() for x in lyrics_value.split(",") if x.strip()]
        if not services: raise ValueError("Informe ao menos um provedor Mornye.")
        all_tracks, totals = [], {"downloaded": 0, "skipped": 0, "failed": 0}
        progress, status = st.progress(0), st.empty()
        for index, url in enumerate(links, 1):
            status.write(f"Processando link {index} de {len(links)}: {url}")
            result = run_download(url, output, services, lyrics)
            all_tracks.extend(result.tracks)
            for key in totals: totals[key] += getattr(result, key)
            if result.error: st.error(f"{url}: {result.error}")
            progress.progress(index / len(links))
        playlist = output / "Songetter.m3u8"
        count = write_m3u8(all_tracks, playlist, output)
        status.success("Processamento finalizado.")
        col1, col2, col3 = st.columns(3)
        col1.metric("Baixadas", totals["downloaded"])
        col2.metric("Já existentes", totals["skipped"])
        col3.metric("Falhas", totals["failed"])
        st.write(f"Playlist criada: {playlist} — {count} faixas.")
        if all_tracks:
            st.dataframe([{"Faixa": t.get("title"), "Artista": t.get("artists"), "Álbum": t.get("album"),
                "Status": t.get("status"), "Arquivo": t.get("file_path"), "Erro": t.get("error")} for t in all_tracks],
                use_container_width=True)
    except Exception as exc: st.error(str(exc))
st.divider()
st.caption("Use somente conteúdo que você tenha autorização para baixar e respeite os termos dos serviços.")
