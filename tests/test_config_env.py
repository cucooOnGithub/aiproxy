"""AIPROXY_UPSTREAM_<PROVIDER> env overrides / adds upstream base_url."""

from __future__ import annotations

import tempfile

from aiproxy.config.loader import load_config

_BASE = """
upstreams:
  anthropic:
    base_url: https://api.anthropic.com
pipeline: [logger]
store:
  backend: sqlite
  path: ./map.db
"""


def _cfg_file() -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(_BASE)
    f.close()
    return f.name


def test_env_overrides_existing(monkeypatch):
    monkeypatch.setenv("AIPROXY_UPSTREAM_ANTHROPIC", "http://127.0.0.1:9000")
    cfg = load_config(_cfg_file())
    assert cfg["upstreams"]["anthropic"]["base_url"] == "http://127.0.0.1:9000"


def test_env_adds_new_provider(monkeypatch):
    monkeypatch.setenv("AIPROXY_UPSTREAM_OPENAI", "http://127.0.0.1:9001")
    cfg = load_config(_cfg_file())
    assert cfg["upstreams"]["openai"]["base_url"] == "http://127.0.0.1:9001"


def test_no_env_keeps_config(monkeypatch):
    monkeypatch.delenv("AIPROXY_UPSTREAM_ANTHROPIC", raising=False)
    cfg = load_config(_cfg_file())
    assert cfg["upstreams"]["anthropic"]["base_url"] == "https://api.anthropic.com"
