<#
  aiproxy installer for Windows (PowerShell 5+).
  Sets up: Python venv + deps, Ollama (+ serve), Tesseract OCR, the NER
  models your config needs, a store encryption key, and config.yaml.

  Usage (from the repo root):
    powershell -ExecutionPolicy Bypass -File .\install.ps1

  Re-runnable. System-package steps use winget and are best-effort; if one
  can't auto-install it prints the manual step and continues.
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!! $m" -ForegroundColor Yellow }

# --- 1. Python ----------------------------------------------------------
Say "Checking Python (need >= 3.11)"
$py = $null
foreach ($c in @("python", "py")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) {
    $ok = & $c -c "import sys; print(1 if sys.version_info[:2] >= (3,11) else 0)" 2>$null
    if ($ok -eq "1") { $py = $c; break }
  }
}
if (-not $py) { Warn "Python >= 3.11 not found. Install from https://python.org then re-run."; exit 1 }
Write-Host "Using $(& $py --version) ($py)"

# --- 2. venv + deps -----------------------------------------------------
Say "Creating virtualenv .venv and installing aiproxy[media]"
& $py -m venv .venv
$pip = ".\.venv\Scripts\pip.exe"
$vpy = ".\.venv\Scripts\python.exe"
& $pip install -q --upgrade pip
& $pip install -q -e ".[media]"

# --- 3. Tesseract OCR ---------------------------------------------------
Say "Checking Tesseract OCR"
if (Get-Command tesseract -ErrorAction SilentlyContinue) {
  Write-Host "tesseract present"
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
  try { winget install -e --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements }
  catch { Warn "winget install Tesseract failed; get it from https://github.com/UB-Mannheim/tesseract/wiki" }
  Warn "Ensure the Tesseract install dir is on PATH (new shell may be needed)."
} else {
  Warn "Install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki and add it to PATH."
}

# --- 4. Ollama ----------------------------------------------------------
Say "Checking Ollama"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    try { winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements }
    catch { Warn "winget install Ollama failed; get it from https://ollama.com/download" }
  } else {
    Warn "Install Ollama from https://ollama.com/download then re-run."
  }
}

Say "Ensuring Ollama is running"
function Ollama-Up {
  try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null; return $true }
  catch { return $false }
}
if (-not (Ollama-Up)) {
  if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Start-Process -WindowStyle Hidden ollama -ArgumentList "serve"
    for ($i = 0; $i -lt 15; $i++) { if (Ollama-Up) { break }; Start-Sleep 1 }
  }
}
if (Ollama-Up) { Write-Host "Ollama up on :11434" } else { Warn "Ollama not reachable; run: ollama serve" }

# --- 5. config.yaml -----------------------------------------------------
Say "Setting up config.yaml"
if (-not (Test-Path config.yaml)) {
  Copy-Item config.example.yaml config.yaml
  Write-Host "Created config.yaml from config.example.yaml"
} else {
  Write-Host "config.yaml already exists, keeping it"
}

# --- 6. Pull the models the config needs --------------------------------
Say "Pulling NER models from config.yaml"
$models = & $vpy -c "import yaml; c=yaml.safe_load(open('config.yaml')) or {}; l=c.get('llm',{}); print('\n'.join(l.get('models') or [l.get('model','qwen2.5:3b-instruct')]))"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  foreach ($m in $models -split "`n") {
    $m = $m.Trim(); if (-not $m) { continue }
    Write-Host "ollama pull $m"
    try { ollama pull $m } catch { Warn "pull of $m failed (check VRAM/disk)" }
  }
} else { Warn "Ollama missing; pull manually: $models" }

# --- 7. Store encryption key -------------------------------------------
Say "Store encryption key"
if (Select-String -Path config.yaml -Pattern '^\s*encrypt:\s*true' -Quiet) {
  $key = & $vpy -m aiproxy genkey
  Write-Host "Encryption is ON. Set this key before running (keep it secret;"
  Write-Host "losing it makes stored mappings unrecoverable):"
  Write-Host "  `$env:AIPROXY_STORE_KEY = `"$key`""
} else {
  Write-Host "Encryption is OFF in config.yaml (dev). Set store.encrypt: true for real use."
}

# --- Done ---------------------------------------------------------------
Say "Done"
Write-Host @"
Run the proxy:
  .\.venv\Scripts\Activate.ps1
  python -m aiproxy serve --config config.yaml

Point your AI tool at it:
  `$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8088"   # match listen.port
Dashboard:  http://127.0.0.1:8088/dashboard
"@
