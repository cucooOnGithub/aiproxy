"""doctor.run_checks flags missing store key and unreachable Ollama."""

from __future__ import annotations

import tempfile

from aiproxy import doctor


def _cfg_file(body: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(body); f.close()
    return f.name


_BASE = """
upstreams: {{anthropic: {{base_url: http://x}}}}
pipeline: [logger]
store: {{path: {db}, encrypt: {enc}}}
llm: {{url: http://127.0.0.1:59999, model: qwen2.5:3b-instruct}}
"""


def _results(path, monkeypatch):
    # Force Ollama unreachable regardless of a real local daemon.
    import httpx
    def boom(*a, **k): raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    return {r.name: r for r in doctor.run_checks(path)}


def test_ollama_unreachable_is_required_fail(monkeypatch):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    path = _cfg_file(_BASE.format(db=db, enc="false"))
    res = _results(path, monkeypatch)
    assert res["ollama reachable"].ok is False
    assert res["ollama reachable"].required is True
    # model check is skipped when unreachable
    assert not any(n.startswith("model pulled") for n in res)


def test_missing_store_key_when_encrypt(monkeypatch):
    monkeypatch.delenv("AIPROXY_STORE_KEY", raising=False)
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    path = _cfg_file(_BASE.format(db=db, enc="true"))
    res = _results(path, monkeypatch)
    r = res["store key (AIPROXY_STORE_KEY)"]
    assert r.ok is False and r.required is True


def test_core_deps_ok(monkeypatch):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    path = _cfg_file(_BASE.format(db=db, enc="false"))
    res = _results(path, monkeypatch)
    assert res["dep: fastapi"].ok and res["dep: httpx"].ok
