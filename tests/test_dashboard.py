"""/metrics counts anonymization and /dashboard serves HTML."""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

from aiproxy.metrics import METRICS
from aiproxy.proxy.server import create_app


class _R:
    def raise_for_status(self): pass
    def json(self): return {"message": {"content":
        '{"entities":[{"text":"John Smith","type":"PERSON"}]}'}}


def _cfg():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return {
        "upstreams": {"anthropic": {"base_url": "http://unused"}},
        "pipeline": ["anonymizer"],
        "store": {"path": db, "encrypt": False},
        "prewarm": False,
        "mode": "fail_closed",
        "anonymizer": {"entities": {"PERSON": "anonymize"}, "mode": "fail_closed",
                       "llm": {"model": "m"}},
    }


def test_dashboard_served():
    app = create_app(_cfg())
    r = TestClient(app).get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "aiproxy" in r.text


def test_metrics_count_after_dryrun(monkeypatch):
    import aiproxy.llm.ollama_ner as m
    monkeypatch.setattr(m.httpx, "post", lambda *a, **k: _R())

    app = create_app(_cfg())
    client = TestClient(app)
    before = METRICS.snapshot()["text_parts_redacted"]

    client.post("/dryrun", json={"model": "x", "max_tokens": 5,
                "messages": [{"role": "user", "content": "Ask John Smith."}]})

    m2 = client.get("/metrics").json()
    assert m2["text_parts_redacted"] == before + 1
    assert m2["entities_by_type"].get("PERSON", 0) >= 1
    assert m2["tokens_created"] >= 1
    assert "store" in m2 and m2["store"]["total_mappings"] >= 1
