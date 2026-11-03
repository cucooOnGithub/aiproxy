#!/usr/bin/env bash
# Build a single self-contained `aiproxy` binary for THIS OS (macOS/Linux).
#
# PyInstaller cannot cross-compile: run this on each OS you want a binary for
# (or in CI). Tesseract OCR and Ollama are NOT bundled — they stay external.
#
# Output: dist/aiproxy
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv-build"

echo "==> Creating build venv ($VENV)"
"$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing aiproxy + media extras + PyInstaller"
pip install -q --upgrade pip
pip install -q -e ".[media]" pyinstaller

echo "==> Building binary"
pyinstaller --clean --noconfirm aiproxy.spec

echo "==> Done: $(pwd)/dist/aiproxy"
echo "    Run it: ./dist/aiproxy doctor --config config.yaml"
