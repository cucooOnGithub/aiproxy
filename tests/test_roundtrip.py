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


def test_tokens_dont_collide_across_requests():
    """Each HTTP request builds a fresh SessionContext for the same session.
    The token sequence must keep counting across requests, not restart at 1,
    or a second original would be reissued ⟦EMAIL_1⟧ and clobber the first on
    restore. Regression for the per-request _seq_counter collision bug.
    """
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = SqliteStore(db, Cipher.null())
    proc = SecretsRegex({})
    pipe = Pipeline([proc])

    # Request 1 — new ctx, new email.
    ctx1 = SessionContext("sess", store, {})
    r1 = pipe.run_request(
        Document(parts=[Part(PartKind.TEXT, text="first alice@x.com")]), ctx1)
    tok1 = r1.parts[0].text.split("first ")[1].strip()
    assert tok1 == "⟦EMAIL_1⟧"

    # Request 2 — fresh ctx (same session), different email.
    ctx2 = SessionContext("sess", store, {})
    r2 = pipe.run_request(
        Document(parts=[Part(PartKind.TEXT, text="second bob@y.com")]), ctx2)
    tok2 = r2.parts[0].text.split("second ")[1].strip()
    assert tok2 == "⟦EMAIL_2⟧", f"expected a fresh number, got {tok2}"
    assert tok1 != tok2

    # Both tokens restore to their own original — no clobbering.
    assert store.original_for("sess", "⟦EMAIL_1⟧") == "alice@x.com"
    assert store.original_for("sess", "⟦EMAIL_2⟧") == "bob@y.com"
