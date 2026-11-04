"""Postgres backend for horizontal scale (Phase 1).

Same contract as `SqliteStore` (see store/base.py), but a shared multi-writer
database so N stateless proxy frontends behind a load balancer can share one
mapping store. SQLite is single-writer and local, which blocks scale-out;
Postgres removes that.

`psycopg` (v3) is imported lazily so the dependency is only needed when
`store.backend: postgres` is actually used. Install with:

    pip install 'psycopg[binary]'

Originals/redacted blobs are encrypted at rest by the same `Cipher` as the
SQLite backend, so the database row is useless without the key.

Concurrency: `next_seq` is made atomic across replicas with an UPSERT +
RETURNING inside a transaction, so two frontends minting a token for the same
session never collide on the sequence number.
"""

from __future__ import annotations

import hashlib
import re
import threading

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mappings (
        session_id   TEXT NOT NULL,
        token        TEXT NOT NULL,
        original     BYTEA NOT NULL,
        entity_type  TEXT NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, token)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sess_type ON mappings (session_id, entity_type)",
    """CREATE TABLE IF NOT EXISTS redaction_cache (
        session_id  TEXT NOT NULL,
        stage       TEXT NOT NULL,
        text_hash   TEXT NOT NULL,
        redacted    BYTEA NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, stage, text_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS seq_counters (
        session_id  TEXT PRIMARY KEY,
        n           BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_terms (
        user_id     TEXT NOT NULL,
        kind        TEXT NOT NULL,
        value       TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, kind, value)
    )""",
]

_TOKEN_SEQ_RE = re.compile(r"_(\d+)\D*$")


class PostgresStore:
    def __init__(self, dsn: str, cipher=None):
        import psycopg  # lazy: only needed for this backend

        from aiproxy.store.crypto import Cipher
        self._psycopg = psycopg
        self.dsn = dsn
        self.cipher = cipher or Cipher.null()
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            for stmt in _SCHEMA:
                cur.execute(stmt)

    def _reconnect_if_needed(self) -> None:
        if self._conn.closed:
            self._conn = self._psycopg.connect(self.dsn, autocommit=True)

    # --- reversible token mappings -------------------------------------
    def put(self, session_id: str, token: str, original: str,
            entity_type: str) -> None:
        enc = self.cipher.encrypt(original.encode())
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mappings "
                    "(session_id, token, original, entity_type) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (session_id, token, enc, entity_type),
                )

    def next_seq(self, session_id: str) -> int:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT n FROM seq_counters WHERE session_id=%s",
                    (session_id,))
                row = cur.fetchone()
                if row is None:
                    seed = self._max_token_seq(cur, session_id) + 1
                    # Atomic across replicas: if another frontend seeded first,
                    # take the greater and bump it.
                    cur.execute(
                        "INSERT INTO seq_counters (session_id, n) VALUES (%s,%s) "
                        "ON CONFLICT (session_id) DO UPDATE "
                        "SET n = seq_counters.n + 1 RETURNING n",
                        (session_id, seed))
                    return cur.fetchone()[0]
                cur.execute(
                    "UPDATE seq_counters SET n = n + 1 WHERE session_id=%s "
                    "RETURNING n", (session_id,))
                return cur.fetchone()[0]

    def _max_token_seq(self, cur, session_id: str) -> int:
        cur.execute("SELECT token FROM mappings WHERE session_id=%s",
                    (session_id,))
        mx = 0
        for (tok,) in cur.fetchall():
            m = _TOKEN_SEQ_RE.search(tok)
            if m:
                mx = max(mx, int(m.group(1)))
        return mx

    def token_for(self, session_id: str, original: str,
                  entity_type: str) -> str | None:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT token, original FROM mappings "
                    "WHERE session_id=%s AND entity_type=%s",
                    (session_id, entity_type))
                rows = cur.fetchall()
        for token, enc in rows:
            if self.cipher.decrypt(bytes(enc)).decode() == original:
                return token
        return None

    def all_mappings(self, session_id: str) -> list[tuple[str, str, str]]:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT original, token, entity_type FROM mappings "
                    "WHERE session_id=%s", (session_id,))
                rows = cur.fetchall()
        return [(self.cipher.decrypt(bytes(enc)).decode(), tok, et)
                for enc, tok, et in rows]

    def original_for(self, session_id: str, token: str) -> str | None:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT original FROM mappings "
                    "WHERE session_id=%s AND token=%s", (session_id, token))
                row = cur.fetchone()
        return self.cipher.decrypt(bytes(row[0])).decode() if row else None

    def purge_session(self, session_id: str) -> int:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM mappings WHERE session_id=%s",
                            (session_id,))
                n = cur.rowcount
                cur.execute("DELETE FROM redaction_cache WHERE session_id=%s",
                            (session_id,))
                cur.execute("DELETE FROM seq_counters WHERE session_id=%s",
                            (session_id,))
        return n

    def stats(self) -> dict:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM mappings")
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(DISTINCT session_id) FROM mappings")
                sessions = cur.fetchone()[0]
                cur.execute("SELECT entity_type, COUNT(*) FROM mappings "
                            "GROUP BY entity_type")
                by_type = dict(cur.fetchall())
                cur.execute("SELECT COUNT(*) FROM redaction_cache")
                cached = cur.fetchone()[0]
        return {
            "total_mappings": total,
            "sessions": sessions,
            "mappings_by_type": by_type,
            "cached_documents": cached,
        }

    # --- per-user term lists (Phase 3) ---------------------------------
    def user_terms(self, user_id: str) -> dict[str, list[str]]:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute("SELECT kind, value FROM user_terms "
                            "WHERE user_id=%s", (user_id,))
                rows = cur.fetchall()
        out: dict[str, list[str]] = {}
        for kind, value in rows:
            out.setdefault(kind, []).append(value)
        return out

    def add_user_term(self, user_id: str, kind: str, value: str) -> None:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_terms (user_id, kind, value) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, kind, value))

    def remove_user_term(self, user_id: str, kind: str, value: str) -> int:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_terms "
                    "WHERE user_id=%s AND kind=%s AND value=%s",
                    (user_id, kind, value))
                return cur.rowcount

    # --- per-text redaction cache --------------------------------------
    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def cache_get(self, session_id: str, stage: str,
                  text_hash: str) -> str | None:
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT redacted FROM redaction_cache "
                    "WHERE session_id=%s AND stage=%s AND text_hash=%s",
                    (session_id, stage, text_hash))
                row = cur.fetchone()
        return self.cipher.decrypt(bytes(row[0])).decode() if row else None

    def cache_put(self, session_id: str, stage: str, text_hash: str,
                  redacted: str) -> None:
        enc = self.cipher.encrypt(redacted.encode())
        with self._lock:
            self._reconnect_if_needed()
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO redaction_cache "
                    "(session_id, stage, text_hash, redacted) "
                    "VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (session_id, stage, text_hash) "
                    "DO UPDATE SET redacted = EXCLUDED.redacted",
                    (session_id, stage, text_hash, enc))
