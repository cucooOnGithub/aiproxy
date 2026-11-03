"""llm.timeout/warm_timeout are configurable and reach OllamaNER; warm()
issues a chat request with the warm_timeout."""

from __future__ import annotations

from aiproxy.processors.anonymizer import Anonymizer


def test_timeout_from_config():
    a = Anonymizer({"llm": {"timeout": 12, "warm_timeout": 99},
                    "entities": {}})
    assert a.ner.timeout == 12
    assert a.ner.warm_timeout == 99


def test_timeout_defaults():
    a = Anonymizer({"llm": {}, "entities": {}})
    assert a.ner.timeout == 60.0
    assert a.ner.warm_timeout == 300.0


def test_warm_uses_warm_timeout(monkeypatch):
    import aiproxy.llm.ollama_ner as m

    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, json, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        seen["model"] = json["model"]
        return _Resp()

    monkeypatch.setattr(m.httpx, "post", fake_post)
    a = Anonymizer({"llm": {"model": "m1", "warm_timeout": 123}, "entities": {}})
    a.ner.warm()
    assert seen["timeout"] == 123
    assert seen["url"].endswith("/api/chat")
    assert seen["model"] == "m1"
