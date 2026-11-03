#!/usr/bin/env bash
# aiproxy installer for macOS and Linux.
# Sets up: Python venv + deps, Ollama (+ serve), Tesseract OCR, the NER
# models your config needs, a store encryption key, and config.yaml.
#
# Usage:   ./install.sh
# Re-runnable (idempotent). System-package steps are best-effort; if one
# can't auto-install, it prints the manual command and continues.
set -euo pipefail

cd "$(dirname "$0")"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }

OS="$(uname -s)"   # Darwin | Linux

# --- 1. Python ----------------------------------------------------------
say "Checking Python (need >= 3.11)"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)'; then
      PY="$c"; break
    fi
  fi
done
[ -n "$PY" ] || { warn "Python >= 3.11 not found. Install it, then re-run."; exit 1; }
echo "Using $("$PY" --version) ($PY)"

# --- 2. venv + deps -----------------------------------------------------
say "Creating virtualenv .venv and installing aiproxy[media]"
"$PY" -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e ".[media]"

# --- 3. Tesseract OCR (image/PDF redaction) -----------------------------
say "Checking Tesseract OCR"
if command -v tesseract >/dev/null 2>&1; then
  echo "tesseract present: $(command -v tesseract)"
elif [ "$OS" = "Darwin" ]; then
  if command -v brew >/dev/null 2>&1; then brew install tesseract || warn "brew install tesseract failed"
  else warn "Install Homebrew then: brew install tesseract"; fi
else
  if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y tesseract-ocr || warn "apt install tesseract-ocr failed"
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y tesseract || warn "dnf install tesseract failed"
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm tesseract || warn "pacman install tesseract failed"
  else warn "Install the 'tesseract' package with your package manager."; fi
fi

# --- 4. Ollama ----------------------------------------------------------
say "Checking Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then brew install ollama || warn "brew install ollama failed"
    else warn "Install Ollama from https://ollama.com/download then re-run."; fi
  else
    echo "Installing Ollama via official script..."
    curl -fsSL https://ollama.com/install.sh | sh || warn "Ollama install script failed; see https://ollama.com/download"
  fi
fi

say "Ensuring Ollama is running"
if ! curl -fsS -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  if command -v ollama >/dev/null 2>&1; then
    nohup ollama serve >/tmp/ollama.log 2>&1 &
    for _ in $(seq 1 15); do
      curl -fsS -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
  fi
fi
curl -fsS -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 \
  && echo "Ollama up on :11434" || warn "Ollama not reachable; start it with: ollama serve"

# --- 5. config.yaml -----------------------------------------------------
say "Setting up config.yaml"
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "Created config.yaml from config.example.yaml"
else
  echo "config.yaml already exists, keeping it"
fi

# --- 6. Pull the models the config needs --------------------------------
say "Pulling NER models from config.yaml"
MODELS="$(python -c 'import yaml,sys; c=yaml.safe_load(open("config.yaml")) or {}; l=c.get("llm",{}); print("\n".join(l.get("models") or [l.get("model","qwen2.5:3b-instruct")]))')"
if command -v ollama >/dev/null 2>&1; then
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    echo "ollama pull $m"
    ollama pull "$m" || warn "pull of $m failed (check VRAM/disk)"
  done <<< "$MODELS"
else
  warn "Ollama missing; pull manually: $(echo "$MODELS" | tr '\n' ' ')"
fi

# --- 7. Store encryption key -------------------------------------------
say "Store encryption key"
if grep -Eq '^\s*encrypt:\s*true' config.yaml; then
  KEY="$(python -m aiproxy genkey)"
  cat <<EOF
Encryption is ON in config.yaml. Export this key before running (keep it
secret; losing it makes stored mappings unrecoverable):

  export AIPROXY_STORE_KEY="$KEY"
EOF
else
  echo "Encryption is OFF in config.yaml (dev). Set store.encrypt: true for real use."
fi

# --- Done ---------------------------------------------------------------
say "Done"
cat <<'EOF'
Run the proxy:
  source .venv/bin/activate
  python -m aiproxy serve --config config.yaml

Then point your AI tool at it:
  export ANTHROPIC_BASE_URL=http://127.0.0.1:8088   # match listen.port
Dashboard:  http://127.0.0.1:8088/dashboard
Preview redactions without sending:  POST http://127.0.0.1:8088/dryrun
EOF
