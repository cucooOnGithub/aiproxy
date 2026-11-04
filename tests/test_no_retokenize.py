"""Regression: anonymizer must not re-tokenize a token an earlier processor
already produced (live echo proof showed NER flagging ⟦EMAIL_1⟧ as EMAIL,
creating a nested ⟦EMAIL_3⟧ -> ⟦EMAIL_1⟧ mapping)."""

from __future__ import annotations

import tempfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.llm.ollama_ner import Entity
from aiproxy.pipeline.base import SessionContext
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _ctx():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return SessionContext("s", SqliteStore(db, Cipher.null()), {})


def test_existing_token_not_retokenized(monkeypatch):
    a = Anonymizer({"entities": {"EMAIL": "anonymize", "PERSON": "anonymize"}})
    # NER returns a real name AND (wrongly) the existing token as an email.
    monkeypatch.setattr(a.ner, "detect", lambda _t: [
        Entity("Jane Doe", "PERSON"),
        Entity("⟦EMAIL_1⟧", "EMAIL"),
    ])
    ctx = _ctx()
    doc = Document(parts=[Part(PartKind.TEXT,
                              text="⟦EMAIL_1⟧ is Jane Doe")])
    out = a.process_request(doc, ctx).parts[0].text

    assert "⟦EMAIL_1⟧" in out            # left intact, not wrapped
    assert "⟦PERSON" in out                   # name tokenized
    # No mapping whose original is itself a token.
    import sqlite3
    rows = ctx.store._conn.execute("SELECT original FROM mappings").fetchall()
    assert all(not r[0].decode().startswith("⟦") for r in rows)


def test_absent_entity_creates_no_mapping(monkeypatch):
    """NER sometimes returns an entity string that isn't literally in the text
    (a garbled echo of an existing token). It must be skipped entirely, not
    stored as an orphan mapping that never appears in the output."""
    a = Anonymizer({"entities": {"PHONE": "anonymize", "PERSON": "anonymize"}})
    monkeypatch.setattr(a.ner, "detect", lambda _t: [
        Entity("Jane Doe", "PERSON"),        # present -> tokenized
        Entity("@@PHONE_1@@", "PHONE"),      # NOT in text -> must be skipped
    ])
    ctx = _ctx()
    doc = Document(parts=[Part(PartKind.TEXT, text="call Jane Doe")])
    out = a.process_request(doc, ctx).parts[0].text

    assert "⟦PERSON" in out
    rows = ctx.store._conn.execute(
        "SELECT original FROM mappings").fetchall()
    origs = [r[0].decode() for r in rows]
    assert "@@PHONE_1@@" not in origs
    assert origs == ["Jane Doe"]


def test_known_value_sweep_remasks_missed_value(monkeypatch):
    """A value tokenized in one block must be re-masked in a later block even
    when the NER misses it there - the main upstream-leak gap. Here NER flags
    'Acme Corp' only the first time; the sweep must catch the second."""
    a = Anonymizer({"entities": {"ORG": "anonymize"}})
    calls = {"n": 0}

    def fake_detect(_t):
        calls["n"] += 1
        return [Entity("Acme Corp", "ORG")] if calls["n"] == 1 else []

    monkeypatch.setattr(a.ner, "detect", fake_detect)
    ctx = _ctx()

    first = a.process_request(
        Document(parts=[Part(PartKind.TEXT, text="deck by Acme Corp")]), ctx)
    assert "Acme Corp" not in first.parts[0].text
    tok = first.parts[0].text.split("deck by ")[1].strip()

    # Second block, NER returns nothing, but the value is already known.
    second = a.process_request(
        Document(parts=[Part(PartKind.TEXT, text="more about Acme Corp here")]), ctx)
    assert "Acme Corp" not in second.parts[0].text      # swept, not leaked
    assert tok in second.parts[0].text                 # same token reused


def test_always_tokenize_is_reversible(monkeypatch):
    """always_tokenize must mask with a REVERSIBLE token (restored on the
    response), not the irreversible [REDACTED] the denylist uses - so a known
    client name is hidden upstream yet reproduced in Claude's output."""
    a = Anonymizer({"entities": {}, "always_tokenize": ["Acme Corp"]})
    monkeypatch.setattr(a.ner, "detect", lambda _t: [])
    ctx = _ctx()
    out = a.process_request(
        Document(parts=[Part(PartKind.TEXT, text="deck by Acme Corp")]), ctx
    ).parts[0].text
    assert "Acme Corp" not in out
    assert "[REDACTED]" not in out            # NOT the irreversible path
    tok = out.split("deck by ")[1].strip()
    assert tok.startswith("⟦") and tok.endswith("⟧")
    # Reversible: the token maps back to the original in the store.
    assert ctx.store.original_for("s", tok) == "Acme Corp"


def test_denylist_still_irreversible(monkeypatch):
    a = Anonymizer({"entities": {}, "denylist": ["Secret Corp"]})
    monkeypatch.setattr(a.ner, "detect", lambda _t: [])
    ctx = _ctx()
    out = a.process_request(
        Document(parts=[Part(PartKind.TEXT, text="at Secret Corp today")]), ctx
    ).parts[0].text
    assert "Secret Corp" not in out and "[REDACTED]" in out
