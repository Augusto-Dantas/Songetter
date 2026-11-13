# Songetter

Aplicação Python para processar vários links do Spotify em lote usando [Mornye-Python](https://github.com/mornyeapp/Mornye-Python).

## Recursos

- Links de faixas, álbuns e playlists em lote.
- Interface web local com Streamlit e CLI.
- Organização por artista e álbum.
- Tags e capa incorporadas ao áudio pelo Mornye.
- Letras sincronizadas incorporadas às tags e exportadas em arquivos `.lrc`.
- Playlist geral `.m3u8` com caminhos relativos.
- Relatório de faixas baixadas, existentes e com falha.

## Requisitos

Python 3.11+ e um provedor de áudio compatível com Mornye instalado e configurado.

**Importante:** Mornye não inclui provedores de áudio nem um registry padrão. Songetter não instala extensões automaticamente. Escolha, revise e instale explicitamente uma extensão de um registry em que confie. Use apenas conteúdo que você tenha autorização para obter e respeite os termos dos serviços.

## Instalação

```bash
git clone https://github.com/Augusto-Dantas/Songetter.git
cd Songetter
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

## Configurar Mornye

Siga a [documentação oficial](https://github.com/mornyeapp/Mornye-Python) para configurar um registry e instalar uma extensão de provedor. Songetter recebe os IDs das extensões já instaladas, por exemplo `ext:tidal-web`; esse exemplo não instala nem recomenda o provedor.

## Interface web

```bash
streamlit run app.py
```

Abra o endereço local informado pelo Streamlit (normalmente `http://localhost:8501`), cole os links e informe os serviços configurados.

## Linha de comando

```bash
songetter "https://open.spotify.com/track/ID" "https://open.spotify.com/playlist/ID" --service ext:SEU-PROVEDOR
```

Ou use um arquivo `links.txt`, com um link por linha:

```bash
songetter --input links.txt --output ./Music --service ext:SEU-PROVEDOR
```

Repita `--service` para incluir mais de uma extensão.

## Saída

```text
Music/
├── Songetter.m3u8
└── Artista/
    └── Álbum/
        ├── Faixa.flac
        ├── Faixa.lrc
        └── ...
```

A capa é incorporada nos metadados do áudio pelo Mornye. Arquivos LRC são criados quando há letras disponíveis. A playlist é gerada a partir dos caminhos confirmados no relatório JSON do Mornye e ignora arquivos ausentes ou faixas com falha.

## Testes

```bash
pip install pytest
pytest
```

## Licença

MIT. Consulte também as licenças e os termos dos provedores e extensões que decidir utilizar.
