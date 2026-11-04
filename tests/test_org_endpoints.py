"""Phases 2-5: server endpoints — identity, /api/terms, /readyz, hot-reload."""

from __future__ import annotations

import tempfile

import yaml
from fastapi.testclient import TestClient

from aiproxy.config.loader import load_config
from aiproxy.proxy.server import create_app

_CFG = {
    "upstreams": {"anthropic": {"base_url": "http://127.0.0.1:1"}},
    "pipeline": ["secrets_regex"],
    "store": {"backend": "sqlite"},
    "mode": "fail_closed",
    "secrets_regex": {"patterns": ["EMAIL"]},
    "anonymizer": {"entities": {}},
    "identity": {"source": "header", "header": "x-auth-user"},
    "prewarm": False,
}


def _cfg(tmp_path):
    cfg = dict(_CFG)
    cfg["store"] = {"backend": "sqlite", "path": str(tmp_path / "map.db")}
    return cfg


def test_healthz_readyz(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as c:
        assert c.get("/healthz").json()["ok"] is True
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["ready"] is True


def test_terms_api_per_user(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as c:
        # alice adds a term
        r = c.post("/api/terms", json={"kind": "always_tokenize", "value": "X"},
                   headers={"x-auth-user": "alice"})
        assert r.status_code == 201
        # alice sees it
        got = c.get("/api/terms", headers={"x-auth-user": "alice"}).json()
        assert got["user_id"] == "alice"
        assert got["terms"]["always_tokenize"] == ["X"]
        # bob does not
        got_b = c.get("/api/terms", headers={"x-auth-user": "bob"}).json()
        assert got_b["terms"] == {}
        # delete
        c.request("DELETE", "/api/terms",
                  json={"kind": "always_tokenize", "value": "X"},
                  headers={"x-auth-user": "alice"})
        assert c.get("/api/terms", headers={"x-auth-user": "alice"}
                     ).json()["terms"] == {}


def test_terms_api_rejects_bad_kind(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as c:
        r = c.post("/api/terms", json={"kind": "allowlist", "value": "X"},
                   headers={"x-auth-user": "alice"})
        assert r.status_code == 400


def test_terms_page_served(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as c:
        assert "My redaction terms" in c.get("/terms").text


def test_hot_reload_swaps_config(tmp_path):
    p = tmp_path / "config.yaml"
    cfg = _cfg(tmp_path)
    p.write_text(yaml.safe_dump(cfg))
    load_config(str(p))
    app = create_app(load_config(str(p)), config_path=str(p))
    with TestClient(app) as c:
        c.get("/healthz")
    # change upstream, write, reload via exposed hook
    cfg["upstreams"]["anthropic"]["base_url"] = "http://changed:9"
    p.write_text(yaml.safe_dump(cfg))
    app.state.reload()
    assert app.state.runtime.upstreams["anthropic"]["base_url"] == "http://changed:9"


def test_hot_reload_rejects_bad_config(tmp_path):
    p = tmp_path / "config.yaml"
    cfg = _cfg(tmp_path)
    p.write_text(yaml.safe_dump(cfg))
    app = create_app(load_config(str(p)), config_path=str(p))
    before = app.state.runtime.upstreams["anthropic"]["base_url"]
    # write invalid config (bad mode) and reload: must be rejected, not raise
    cfg["mode"] = "yolo"
    cfg["upstreams"]["anthropic"]["base_url"] = "http://changed:9"
    p.write_text(yaml.safe_dump(cfg))
    app.state.reload()  # logs + keeps old config; must not raise
    assert app.state.runtime.upstreams["anthropic"]["base_url"] == before
