# PyInstaller spec — one self-contained `aiproxy` executable.
#
# Build:  pyinstaller --clean --noconfirm aiproxy.spec   (or ./build.sh)
# Output: dist/aiproxy  (dist/aiproxy.exe on Windows)
#
# Note: the binary bundles only Python + its libraries (pypdfium2's native
# PDFium lib included). Tesseract OCR and Ollama are separate programs and
# are NOT bundled — install them on the host as usual.

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Packages with data files / native libs / dynamic imports that the static
# analysis misses. collect_all grabs submodules, datas and binaries.
for pkg in ("pypdfium2", "pypdfium2_raw", "uvicorn", "ruamel.yaml",
            "cryptography"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# uvicorn imports its loops/protocols/lifespan backends by string at runtime.
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    ["packaging/pyi_entry.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="aiproxy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
