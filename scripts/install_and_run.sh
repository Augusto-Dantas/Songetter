#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
    cat <<'EOF'
Usage: ./scripts/install_and_run.sh [OPTIONS] [MODE] [ARGS...]

Setup a Python 3.11+ virtual environment, verify Songetter runtime health, and run a mode.

Modes:
  tui               Launch interactive Textual TUI (default)
  web | serve       Start FastAPI web UI & API server
  streamlit         Start legacy Streamlit UI (app.py)
  doctor            Run environment and dependency diagnostics
  test              Run pytest test suite
  <subcommand>      Forward any other subcommand/flags to `python -m songetter.cli`

Options:
  --install         Force reinstall (`pip install -e ".[dev]"`)
  --no-install      Skip virtual environment installation check
  -h, --help        Show this help message
EOF
}

FORCE_INSTALL=0
SKIP_INSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install)
            FORCE_INSTALL=1
            shift
            ;;
        --no-install)
            SKIP_INSTALL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

cd "${REPO_ROOT}"

if [[ "${SKIP_INSTALL}" -eq 0 ]]; then
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
            echo "Error: ${PYTHON_BIN} not found on PATH." >&2
            exit 1
        fi
        "${PYTHON_BIN}" -c 'import sys; assert sys.version_info >= (3, 11), "Python >= 3.11 required"'
        echo "Creating virtual environment in ${VENV_DIR}..."
        "${PYTHON_BIN}" -m venv "${VENV_DIR}"
    fi

    if [[ "${FORCE_INSTALL}" -eq 1 ]] || ! "${VENV_DIR}/bin/python" -c "import songetter, textual, httpx, fastapi, mutagen" >/dev/null 2>&1; then
        echo "Installing Songetter editable package and dev dependencies..."
        "${VENV_DIR}/bin/pip" install --upgrade pip
        "${VENV_DIR}/bin/pip" install -e ".[dev]"
    fi
fi

VENV_PY="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
    echo "Error: Virtual environment Python not found at ${VENV_PY}" >&2
    exit 1
fi

MODE="${1:-tui}"
if [[ $# -gt 0 ]]; then
    shift
fi

case "${MODE}" in
    tui)
        exec "${VENV_PY}" -m songetter.cli tui "$@"
        ;;
    web|serve)
        exec "${VENV_PY}" -m songetter.cli serve "$@"
        ;;
    streamlit)
        exec "${VENV_DIR}/bin/streamlit" run "${REPO_ROOT}/app.py" "$@"
        ;;
    doctor)
        exec "${VENV_PY}" -m songetter.cli doctor "$@"
        ;;
    test)
        exec "${VENV_DIR}/bin/pytest" -q "$@"
        ;;
    *)
        exec "${VENV_PY}" -m songetter.cli "${MODE}" "$@"
        ;;
esac
