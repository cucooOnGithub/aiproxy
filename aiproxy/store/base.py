"""Store interface + backend factory.

The store IS the de-anonymization key: it holds token<->original mappings,
the per-session sequence counter, the per-text redaction cache and (Phase 3)
per-user term lists. Any backend MUST implement every method below with the
same semantics as `SqliteStore`, which is the reference implementation.

Two backends ship:
  * `sqlite`   - single-file, single-writer, local. Default/standalone.
  * `postgres` - multi-writer, for horizontally scaled (multi-replica)
                 deployments where many stateless proxy frontends share one
                 store (see docs/ORG_DEPLOYMENT.md, Phases 1 & 5).

Per-user isolation (Phase 2) is achieved by namespacing `session_id` as
``<user_id>/<raw_session>`` before it reaches the store, so one user can
never reverse another's tokens and sequence numbers never collide across
users. The store itself therefore stays user-agnostic for the mapping
methods; only the term-list methods take an explicit `user_id`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """The contract every store backend implements."""

    # --- reversible token mappings -------------------------------------
    def put(self, session_id: str, token: str, original: str,
            entity_type: str) -> None: ...

    def next_seq(self, session_id: str) -> int: ...

    def token_for(self, session_id: str, original: str,
                  entity_type: str) -> str | None: ...

    def all_mappings(self, session_id: str) -> list[tuple[str, str, str]]: ...

    def original_for(self, session_id: str, token: str) -> str | None: ...

    def purge_session(self, session_id: str) -> int: ...

    def stats(self) -> dict: ...

    # --- per-text redaction cache --------------------------------------
    @staticmethod
    def text_hash(text: str) -> str: ...

    def cache_get(self, session_id: str, stage: str,
                  text_hash: str) -> str | None: ...

    def cache_put(self, session_id: str, stage: str, text_hash: str,
                  redacted: str) -> None: ...

    # --- per-user term lists (Phase 3) ---------------------------------
    def user_terms(self, user_id: str) -> dict[str, list[str]]: ...

    def add_user_term(self, user_id: str, kind: str, value: str) -> None: ...

    def remove_user_term(self, user_id: str, kind: str, value: str) -> int: ...


# Kinds a user may self-manage. Governance rule (Phase 3): the user tier may
# only ADD reversible/irreversible masking terms; it can never remove or
# allowlist anything in the org tier (which lives in central config, not in
# this table), so there is no 'allowlist' kind here by design.
USER_TERM_KINDS = ("always_tokenize", "denylist")


def build_store(cfg: dict):
    """Construct the configured store backend.

    `store.backend`: "sqlite" (default) or "postgres".
    Encryption (`store.encrypt`) and key sourcing are resolved here so every
    backend gets the same `Cipher`.
    """
    from aiproxy.store.crypto import Cipher

    s = cfg.get("store", {}) or {}
    backend = (s.get("backend") or "sqlite").lower()
    cipher = Cipher.from_config(s) if s.get("encrypt") else Cipher.null()

    if backend == "sqlite":
        from aiproxy.store.sqlite_store import SqliteStore
        return SqliteStore(s.get("path", "./map.db"), cipher)
    if backend == "postgres":
        from aiproxy.store.postgres_store import PostgresStore
        dsn = s.get("dsn") or s.get("url")
        if not dsn:
            raise ValueError(
                "store.backend: postgres requires store.dsn "
                "(e.g. postgresql://user:pass@host:5432/aiproxy)")
        return PostgresStore(dsn, cipher)
    raise ValueError(f"unknown store.backend '{backend}'; "
                     "known: sqlite, postgres")
