"""Phase 3: per-user term tier merges with org terms, isolated per user."""

from __future__ import annotations

import tempfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.pipeline.base import SessionContext
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.store.base import USER_TERM_KINDS
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


class _NoNER:
    def detect(self, text):
        return []


def _anon(cfg=None):
    a = Anonymizer({"entities": {}, "user_terms_ttl": 0, **(cfg or {})})
    a.ner = _NoNER()  # deterministic: only term lists act
    return a


def _store():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return SqliteStore(db, Cipher.null())


def test_store_user_terms_crud():
    s = _store()
    s.add_user_term("alice", "always_tokenize", "Moonshot")
    s.add_user_term("alice", "denylist", "SSN123")
    assert set(s.user_terms("alice")["always_tokenize"]) == {"Moonshot"}
    assert s.user_terms("bob") == {}
    assert s.remove_user_term("alice", "always_tokenize", "Moonshot") == 1
    assert "always_tokenize" not in s.user_terms("alice")


def test_user_term_masked_only_for_that_user():
    s = _store()
    s.add_user_term("alice", "always_tokenize", "Moonshot")
    anon = _anon()

    def run(user_id):
        ctx = SessionContext(f"{user_id}/s", s, {}, user_id=user_id)
        doc = Document(parts=[Part(PartKind.TEXT, text="ship Moonshot now")])
        return anon.process_request(doc, ctx).parts[0].text

    alice_out = run("alice")
    bob_out = run("bob")
    assert "Moonshot" not in alice_out and "⟦TERM_" in alice_out
    assert "Moonshot" in bob_out  # bob never added it


def test_org_terms_always_apply_on_top():
    s = _store()
    anon = _anon({"always_tokenize": ["OrgSecret"]})
    ctx = SessionContext("bob/s", s, {}, user_id="bob")
    doc = Document(parts=[Part(PartKind.TEXT, text="the OrgSecret value")])
    out = anon.process_request(doc, ctx).parts[0].text
    assert "OrgSecret" not in out


def test_only_additive_kinds_exist():
    assert "allowlist" not in USER_TERM_KINDS
    assert set(USER_TERM_KINDS) == {"always_tokenize", "denylist"}
