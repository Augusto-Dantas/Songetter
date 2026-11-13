# Songetter

Aplicação Python para processar links Spotify em lote usando [Mornye-Python](https://github.com/mornyeapp/Mornye-Python) e os provedores do [Mornye Extension Store](https://github.com/mornyeapp/Mornye-Extension).

## Provedores integrados

O Songetter adiciona automaticamente o catálogo unificado, sem apagar registries que você já configurou:

https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry-unified.json

Provedores padrão (ordem de fallback): `ext:tidal-web`, `ext:qobuz-web`, `ext:deezer`, `ext:amazon`, `ext:ytmusic-spotiflac`, `ext:soundcloud` e `ext:jiosaavn`.

O catálogo inclui também `utilities`, pacote Python de utilitários de metadados, sessão e resolução de navegador. O catálogo unificado separa os runtimes Python e JavaScript; Mornye seleciona os pacotes compatíveis e instala os provedores necessários a partir do registry.

`beatport` e `jellyfin` não entram na lista padrão porque podem exigir infraestrutura, conta ou configuração específica; podem ser selecionados manualmente quando estiverem preparados. Extensões são código de terceiros: revise manifests, permissões, requisitos, licenças e termos dos serviços.

## Requisitos

Python 3.11+, `mornye>=2.0.6` (instalado automaticamente pelo `pip install -e .`) e Node.js para os provedores JavaScript. O primeiro uso consulta o registry; a instalação das extensões pode exigir acesso à internet e configuração própria do provedor.

## Instalação

```bash
git clone https://github.com/Augusto-Dantas/Songetter.git
cd Songetter
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

## Interface web

```bash
streamlit run app.py
```

Abra `http://localhost:8501`, cole os links e ajuste a lista/ordem dos provedores se desejar.

## CLI

```bash
songetter <link-spotify>
songetter --input links.txt --output ./Music
songetter --input links.txt --service ext:tidal-web --service ext:qobuz-web
```

O último exemplo substitui a lista padrão. Registries adicionais podem ser configurados em `MORNYE_REGISTRIES` (separados por vírgula); os existentes são preservados.

## Saída

A biblioteca é organizada por artista/álbum. Mornye incorpora tags e capas, incorpora letras quando disponíveis e o Songetter exporta `.lrc` e uma playlist geral `.m3u8` com caminhos relativos.

Use somente conteúdo que você tenha autorização para baixar e respeite os termos dos serviços.

## Testes

```bash
pip install pytest
pytest
```

## Licença

MIT. Consulte também as licenças dos provedores e extensões.