"""NER cache TTL + persistent per-document redaction cache."""

from __future__ import annotations

import tempfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.llm.ollama_ner import Entity, OllamaNER
from aiproxy.pipeline.base import SessionContext
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _store():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return SqliteStore(db, Cipher.null())


# ---- NER cache TTL -------------------------------------------------------

def test_ner_cache_hit_skips_http(monkeypatch):
    import aiproxy.llm.ollama_ner as m
    calls = {"n": 0}

    class _R:
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": '{"entities":[{"text":"X","type":"ORG"}]}'}}

    def post(*a, **k):
        calls["n"] += 1
        return _R()

    monkeypatch.setattr(m.httpx, "post", post)
    n = OllamaNER("http://x", "mdl", cache_ttl=1000)
    n.detect("hello")
    n.detect("hello")
    assert calls["n"] == 1  # second served from cache


def test_ner_cache_expires(monkeypatch):
    import aiproxy.llm.ollama_ner as m
    calls = {"n": 0}

    class _R:
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": '{"entities":[]}'}}

    monkeypatch.setattr(m.httpx, "post", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or _R()))
    clock = {"t": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])
    n = OllamaNER("http://x", "mdl", cache_ttl=10)
    n.detect("hello")
    clock["t"] += 5
    n.detect("hello")          # still fresh
    assert calls["n"] == 1
    clock["t"] += 20           # now past ttl
    n.detect("hello")
    assert calls["n"] == 2


# ---- persistent redaction cache -----------------------------------------

def test_redaction_cache_skips_ner_on_repeat(monkeypatch):
    store = _store()
    a = Anonymizer({"entities": {"ORG": "anonymize"}})
    calls = {"n": 0}

    def detect(_t):
        calls["n"] += 1
        return [Entity("Globex", "ORG")]

    monkeypatch.setattr(a.ner, "detect", detect)

    ctx = SessionContext("s", store, {})
    doc1 = Document(parts=[Part(PartKind.TEXT, text="the Globex deal")])
    out1 = a.process_request(doc1, ctx).parts[0].text
    assert "Globex" not in out1 and "⟦ORG" in out1

    # Same text again -> cache hit, NER not called a second time.
    doc2 = Document(parts=[Part(PartKind.TEXT, text="the Globex deal")])
    out2 = a.process_request(doc2, ctx).parts[0].text
    assert out2 == out1
    assert calls["n"] == 1


def test_redaction_cache_survives_restart(monkeypatch):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    a = Anonymizer({"entities": {"ORG": "anonymize"}})
    monkeypatch.setattr(a.ner, "detect", lambda _t: [Entity("Globex", "ORG")])

    ctx = SessionContext("s", SqliteStore(db, Cipher.null()), {})
    out1 = a.process_request(
        Document(parts=[Part(PartKind.TEXT, text="Globex")]), ctx).parts[0].text

    # New store object on the same file = simulated proxy restart.
    a2 = Anonymizer({"entities": {"ORG": "anonymize"}})
    def boom(_t):
        raise AssertionError("NER should not run on a cached doc after restart")
    monkeypatch.setattr(a2.ner, "detect", boom)
    ctx2 = SessionContext("s", SqliteStore(db, Cipher.null()), {})
    out2 = a2.process_request(
        Document(parts=[Part(PartKind.TEXT, text="Globex")]), ctx2).parts[0].text
    assert out2 == out1
