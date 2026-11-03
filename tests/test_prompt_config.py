"""llm.prompt and llm.entity_types are configurable and reach the request."""

from __future__ import annotations

import pytest

from aiproxy.llm.ollama_ner import OllamaNER, build_ner


class _R:
    def raise_for_status(self): pass
    def json(self): return {"message": {"content": '{"entities":[]}'}}


def _capture(monkeypatch):
    import aiproxy.llm.ollama_ner as m
    sent = {}
    def post(url, json, timeout):
        sent["json"] = json
        return _R()
    monkeypatch.setattr(m.httpx, "post", post)
    return sent


def test_custom_prompt_and_types_used(monkeypatch):
    sent = _capture(monkeypatch)
    n = OllamaNER("http://x", "m",
                  entity_types=["NAME", "ACCOUNT"],
                  prompt="Find {types}. TEXT: {text}")
    n.detect("hello world")
    content = sent["json"]["messages"][0]["content"]
    assert content == "Find NAME, ACCOUNT. TEXT: hello world"
    # Schema type is enum-constrained to the configured types.
    assert sent["json"]["format"]["properties"]["entities"]["items"][
        "properties"]["type"]["enum"] == ["NAME", "ACCOUNT"]


def test_defaults_when_unset(monkeypatch):
    sent = _capture(monkeypatch)
    n = OllamaNER("http://x", "m")
    n.detect("hi")
    content = sent["json"]["messages"][0]["content"]
    assert "PERSON, ORG, LOCATION, EMAIL, PHONE" in content


def test_prompt_missing_text_placeholder_rejected():
    with pytest.raises(ValueError):
        OllamaNER("http://x", "m", prompt="no placeholder here")


def test_build_ner_passes_prompt_and_types():
    n = build_ner({"model": "m", "prompt": "P {text}",
                   "entity_types": ["X"]})
    assert n.prompt == "P {text}" and n.entity_types == ["X"]
