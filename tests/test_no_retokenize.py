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
