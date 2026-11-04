"""`aiproxy doctor` — verify the install is complete and runnable.

Checks Python, dependencies, Tesseract, config, store, Ollama, and that the
NER models the config asks for are actually pulled. Prints one line per
check and returns a non-zero exit code if any REQUIRED check failed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass


@dataclass
class Result:
    name: str
    ok: bool
    required: bool
    detail: str = ""


def _mod(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_checks(config_path: str) -> list[Result]:
    out: list[Result] = []

    # Python
    v = sys.version_info
    out.append(Result("Python >= 3.11", v[:2] >= (3, 11), True,
                      f"{v.major}.{v.minor}.{v.micro}"))

    # Core deps
    for m in ("fastapi", "uvicorn", "httpx", "yaml", "cryptography"):
        out.append(Result(f"dep: {m}", _mod(m), True,
                          "" if _mod(m) else "pip install -e ."))

    # Media deps (optional — needed only for image/PDF/Office redaction)
    for m in ("PIL", "pytesseract", "pypdfium2"):
        shown = {"PIL": "pillow"}.get(m, m)
        out.append(Result(f"media dep: {shown}", _mod(m), False,
                          "" if _mod(m) else 'pip install -e ".[media]"'))

    # Tesseract binary
    tpath = shutil.which("tesseract")
    out.append(Result("tesseract binary", bool(tpath), False,
                      tpath or "needed for image/scanned-PDF OCR"))

    # Config
    cfg = None
    try:
        from aiproxy.config.loader import load_config
        cfg = load_config(config_path)
        out.append(Result(f"config: {config_path}", True, True, "loaded"))
    except Exception as e:  # noqa: BLE001
        out.append(Result(f"config: {config_path}", False, True, str(e)))

    if cfg is not None:
        # Store openable + encryption key present when required
        s = cfg.get("store", {})
        enc = bool(s.get("encrypt"))
        key_source = (s.get("key_source") or "env").lower()
        if enc and key_source == "env" and not os.environ.get(
                s.get("key_env", "AIPROXY_STORE_KEY")):
            out.append(Result("store key (AIPROXY_STORE_KEY)", False, True,
                              "encrypt:true but key not set; run: aiproxy genkey"))
        else:
            try:
                from aiproxy.proxy.server import _build_store
                _build_store(cfg)
                out.append(Result("store openable", True, True,
                                  s.get("path", "?")))
            except Exception as e:  # noqa: BLE001
                out.append(Result("store openable", False, True, str(e)))

        # Archive dir writable (if configured)
        adir = (cfg.get("archive") or {}).get("dir")
        if adir:
            try:
                from pathlib import Path
                Path(adir).mkdir(parents=True, exist_ok=True)
                probe = Path(adir) / ".doctor_write_test"
                probe.write_bytes(b"ok"); probe.unlink()
                out.append(Result("archive dir writable", True, False, adir))
            except Exception as e:  # noqa: BLE001
                out.append(Result("archive dir writable", False, False, str(e)))

        # Ollama reachable + configured models pulled
        llm = cfg.get("llm", {})
        url = llm.get("url", "http://localhost:11434").rstrip("/")
        models = llm.get("models") or [llm.get("model", "qwen2.5:3b-instruct")]
        tags: list[str] = []
        reachable = False
        try:
            import httpx
            r = httpx.get(f"{url}/api/tags", timeout=5)
            r.raise_for_status()
            tags = [m.get("name", "") for m in r.json().get("models", [])]
            reachable = True
            out.append(Result("ollama reachable", True, True, url))
        except Exception as e:  # noqa: BLE001
            out.append(Result("ollama reachable", False, True,
                              f"{url}: {e}; run: ollama serve"))

        if reachable:
            def present(m: str) -> bool:
                # ollama tags often carry a :latest / :tag suffix
                return any(t == m or t.split(":")[0] == m.split(":")[0]
                           for t in tags)
            for m in models:
                ok = present(m)
                out.append(Result(f"model pulled: {m}", ok, True,
                                  "" if ok else f"run: ollama pull {m}"))

    return out


def main(config_path: str) -> int:
    results = run_checks(config_path)
    print("aiproxy doctor\n")
    failed_required = 0
    for r in results:
        if r.ok:
            mark = "\033[32mOK  \033[0m"
        elif r.required:
            mark = "\033[31mFAIL\033[0m"; failed_required += 1
        else:
            mark = "\033[33mWARN\033[0m"
        line = f"[{mark}] {r.name}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)
    print()
    if failed_required:
        print(f"\033[31m{failed_required} required check(s) failed.\033[0m")
        return 1
    print("\033[32mAll required checks passed.\033[0m")
    return 0
