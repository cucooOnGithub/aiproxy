"""Load + validate YAML config into a plain dict."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

_REQUIRED = ("upstreams", "pipeline", "store")
_UPSTREAM_ENV_PREFIX = "AIPROXY_UPSTREAM_"


def _apply_upstream_env(cfg: dict) -> None:
    """Env var AIPROXY_UPSTREAM_<PROVIDER> overrides that provider's base_url.

    Point the proxy at the real Anthropic endpoint or at another proxy
    (e.g. a second aiproxy, a corporate egress gateway) without editing the
    config file:

        AIPROXY_UPSTREAM_ANTHROPIC=https://api.anthropic.com
        AIPROXY_UPSTREAM_OPENAI=http://127.0.0.1:9000

    The env value wins over config. A provider named only in the env is
    created on the fly, so you can add an upstream purely via environment.
    """
    ups = cfg.setdefault("upstreams", {})
    for key, val in os.environ.items():
        if not key.startswith(_UPSTREAM_ENV_PREFIX) or not val.strip():
            continue
        provider = key[len(_UPSTREAM_ENV_PREFIX):].lower()
        ups.setdefault(provider, {})["base_url"] = val.strip()


def load_config(path: str) -> dict:
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    missing = [k for k in _REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"config missing required keys: {missing}")
    _apply_upstream_env(cfg)
    cfg.setdefault("mode", "fail_closed")
    cfg.setdefault("listen", {"host": "127.0.0.1", "port": 8080})
    # Propagate shared llm block into processors that need it.
    shared_llm = cfg.get("llm")
    if shared_llm:
        for p in ("anonymizer", "image_redactor"):
            cfg.setdefault(p, {}).setdefault("llm", shared_llm)
    # Propagate global mode into anonymizer unless overridden.
    cfg.setdefault("anonymizer", {}).setdefault("mode", cfg["mode"])
    # Fail closed on invalid config: refuse to start/reload (Phase 0).
    from aiproxy.config.schema import validate_config
    validate_config(cfg)
    return cfg
