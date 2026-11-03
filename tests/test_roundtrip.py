"""Round-trip proof using the deterministic secrets filter (no LLM needed).

Verifies: an email is tokenized on the request path, stored, and restored
byte-for-byte on the response path.
"""

from __future__ import annotations

import tempfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.pipeline.base import Pipeline, SessionContext
from aiproxy.processors.secrets_regex import SecretsRegex
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _ctx():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = SqliteStore(db, Cipher.null())
    return SessionContext("sess1", store, {})


def test_email_redacted_and_restored():
    proc = SecretsRegex({})
    pipe = Pipeline([proc])
    ctx = _ctx()

    doc = Document(parts=[Part(PartKind.TEXT, text="mail me at bob@acme.com ok")])
    out = pipe.run_request(doc, ctx)
    sent = out.parts[0].text
    assert "bob@acme.com" not in sent
    assert "⟦EMAIL_1⟧" in sent

    # Simulate upstream echoing the token back.
    resp = Document(parts=[Part(PartKind.TEXT, text=f"sure, {sent}")])
    restored = pipe.run_response(resp, ctx).parts[0].text
    assert "bob@acme.com" in restored


def test_same_value_same_token():
    proc = SecretsRegex({})
    ctx = _ctx()
    doc = Document(parts=[Part(PartKind.TEXT, text="a@x.com and a@x.com")])
    out = proc.process_request(doc, ctx)
    assert out.parts[0].text.count("⟦EMAIL_1⟧") == 2
