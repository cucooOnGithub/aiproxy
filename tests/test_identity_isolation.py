"""Phase 2: identity resolution + per-user namespacing isolation."""

from __future__ import annotations

import tempfile

from aiproxy.identity import resolve_user_id, scoped_session
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


class _Headers(dict):
    def get(self, k, default=""):
        return super().get(k.lower(), default)


def _h(**kw):
    return _Headers({k.replace("_", "-").lower(): v for k, v in kw.items()})


def test_anonymous_default():
    assert resolve_user_id(_h(), {}) == "anon"


def test_api_key_mapping():
    cfg = {"identity": {"source": "api_key", "keys": {"secret1": "alice"}}}
    assert resolve_user_id(_h(x_api_key="secret1"), cfg) == "alice"


def test_api_key_unknown_gets_own_namespace():
    cfg = {"identity": {"source": "api_key", "keys": {"secret1": "alice"}}}
    uid = resolve_user_id(_h(x_api_key="other"), cfg)
    assert uid.startswith("k_") and uid != "alice"


def test_header_source_trusted():
    cfg = {"identity": {"source": "header", "header": "x-auth-user"}}
    assert resolve_user_id(_h(x_auth_user="bob"), cfg) == "bob"


def test_bearer_prefix_stripped():
    cfg = {"identity": {"source": "api_key", "keys": {"tok": "carol"}}}
    assert resolve_user_id(_h(authorization="Bearer tok"), cfg) == "carol"


def test_cross_user_tokens_isolated():
    """A token minted for alice must not be reversible under bob's scope."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = SqliteStore(db, Cipher.null())
    a = scoped_session("alice", "sess")
    b = scoped_session("bob", "sess")
    store.put(a, "⟦PERSON_1⟧", "Alice Secret", "PERSON")
    assert store.original_for(a, "⟦PERSON_1⟧") == "Alice Secret"
    assert store.original_for(b, "⟦PERSON_1⟧") is None
    # Sequences do not collide across users sharing a raw session id.
    assert store.next_seq(a) == 2  # seeded past existing _1
    assert store.next_seq(b) == 1
