"""/dryrun shows the redacted request without forwarding, and honors a
custom llm.prompt (the configured template reaches the local model)."""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

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
        # load_config normally merges top-level llm into each processor; here
        # we build the app directly, so put it where the processor reads it.
        "anonymizer": {"entities": {"PERSON": "anonymize"}, "mode": "fail_closed",
                       "llm": {"model": "m", "prompt": "CUSTOM {types} :: {text}",
                               "entity_types": ["PERSON"]}},
    }


def test_dryrun_redacts_with_custom_prompt(monkeypatch):
    import aiproxy.llm.ollama_ner as m
    seen = {}

    def post(url, json, timeout):
        seen["prompt"] = json["messages"][0]["content"]
        return _R()

    monkeypatch.setattr(m.httpx, "post", post)

    app = create_app(_cfg())
    client = TestClient(app)
    body = {"model": "claude-x", "max_tokens": 10,
            "messages": [{"role": "user", "content": "Ask John Smith to call."}]}
    r = client.post("/dryrun", json=body)
    assert r.status_code == 200

    out = r.json()
    sent_text = out["messages"][0]["content"]
    assert "John Smith" not in sent_text
    assert "⟦PERSON" in sent_text
    # The configured custom prompt template was actually used.
    assert seen["prompt"] == "CUSTOM PERSON :: Ask John Smith to call."
