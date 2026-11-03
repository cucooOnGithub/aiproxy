# Build a single self-contained aiproxy.exe for Windows.
#
# PyInstaller cannot cross-compile: run this on Windows to get a Windows
# binary. Tesseract OCR and Ollama are NOT bundled - they stay external.
#
# Output: dist\aiproxy.exe
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$venv = ".venv-build"

Write-Host "==> Creating build venv ($venv)"
& $py -m venv $venv
$activate = Join-Path $venv "Scripts\Activate.ps1"
. $activate

Write-Host "==> Installing aiproxy + media extras + PyInstaller"
pip install -q --upgrade pip
pip install -q -e ".[media]" pyinstaller

Write-Host "==> Building binary"
pyinstaller --clean --noconfirm aiproxy.spec

Write-Host "==> Done: $PSScriptRoot\dist\aiproxy.exe"
Write-Host "    Run it: .\dist\aiproxy.exe doctor --config config.yaml"
