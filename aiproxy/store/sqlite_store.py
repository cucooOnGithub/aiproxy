"""Reversible token<->original mapping store (SQLite).

This store IS the de-anonymization key. Treat its file as a secret:
anyone with it can reverse every redaction. Originals are encrypted at
rest when a key is configured (see crypto.py). GUID tokens are useless
without this store, so losing it = permanent anonymization.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path

from aiproxy.store.crypto import Cipher

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mappings (
    session_id   TEXT NOT NULL,
    token        TEXT NOT NULL,
    original     BLOB NOT NULL,   -- encrypted when a key is set
    entity_type  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, token)
);
CREATE INDEX IF NOT EXISTS idx_sess_orig
    ON mappings (session_id, entity_type);

-- Per-text redaction cache: hash of a processor's INPUT text -> its already
-- redacted OUTPUT, so a re-presented document (e.g. the same conversation
-- history resent each turn) is not re-analyzed. The token<->original
-- mappings produced the first time stay in `mappings`, so restoration of a
-- cached-hit output still works. `redacted` is encrypted at rest like
-- originals, since it embeds the source structure.
CREATE TABLE IF NOT EXISTS redaction_cache (
    session_id  TEXT NOT NULL,
    stage       TEXT NOT NULL,   -- which processor produced it
    text_hash   TEXT NOT NULL,   -- sha256 of the input text
    redacted    BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, stage, text_hash)
);

-- Monotonic token sequence per session. The token number MUST be stable and
-- unique across a whole session, not per-request: a SessionContext is created
-- fresh for every request, so a request-local counter restarts at 1 each time
-- and reissues e.g. PERSON_1 to a different original, colliding in `mappings`
-- (PK session_id+token, INSERT OR IGNORE) and corrupting restoration.
CREATE TABLE IF NOT EXISTS seq_counters (
    session_id  TEXT PRIMARY KEY,
    n           INTEGER NOT NULL
);
"""

# Trailing "_<number>" in a token like ⟦PERSON_12⟧ (entity types carry no digits).
_TOKEN_SEQ_RE = re.compile(r"_(\d+)\D*$")


class SqliteStore:
    def __init__(self, path: str, cipher: Cipher | None = None):
        self.path = path
        self.cipher = cipher or Cipher.null()
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def put(self, session_id: str, token: str, original: str, entity_type: str) -> None:
        enc = self.cipher.encrypt(original.encode())
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO mappings "
                "(session_id, token, original, entity_type) VALUES (?,?,?,?)",
                (session_id, token, enc, entity_type),
            )
            self._conn.commit()

    def next_seq(self, session_id: str) -> int:
        """Return the next token sequence number for a session, persisted and
        monotonic across requests so tokens never collide. Seeds from any
        tokens already stored for the session (a store written before this
        counter existed) so an existing number is never reissued.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT n FROM seq_counters WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                n = self._max_token_seq(session_id) + 1
                self._conn.execute(
                    "INSERT INTO seq_counters (session_id, n) VALUES (?,?)",
                    (session_id, n),
                )
            else:
                n = row[0] + 1
                self._conn.execute(
                    "UPDATE seq_counters SET n=? WHERE session_id=?",
                    (n, session_id),
                )
            self._conn.commit()
            return n

    def _max_token_seq(self, session_id: str) -> int:
        """Highest "_<n>" across existing tokens for a session (0 if none).
        Called under self._lock."""
        rows = self._conn.execute(
            "SELECT token FROM mappings WHERE session_id=?", (session_id,)
        ).fetchall()
        mx = 0
        for (tok,) in rows:
            m = _TOKEN_SEQ_RE.search(tok)
            if m:
                mx = max(mx, int(m.group(1)))
        return mx

    def token_for(self, session_id: str, original: str, entity_type: str) -> str | None:
        """Reverse lookup: has this original already been tokenized?

        Linear scan + decrypt because originals are stored encrypted (no
        plaintext index). Fine for per-session sizes; optimize with a
        keyed HMAC index column if sessions get large.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, original FROM mappings "
                "WHERE session_id=? AND entity_type=?",
                (session_id, entity_type),
            ).fetchall()
        for token, enc in rows:
            if self.cipher.decrypt(enc).decode() == original:
                return token
        return None

    def all_mappings(self, session_id: str) -> list[tuple[str, str, str]]:
        """Every (original, token, entity_type) for a session, decrypted.

        Used by the anonymizer's known-value sweep to re-mask any value that
        was tokenized earlier in the session, even in blocks where the NER
        misses it this turn - closing the 'caught once, leaked later' gap.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT original, token, entity_type FROM mappings "
                "WHERE session_id=?", (session_id,)
            ).fetchall()
        return [(self.cipher.decrypt(enc).decode(), tok, et)
                for enc, tok, et in rows]

    def original_for(self, session_id: str, token: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT original FROM mappings WHERE session_id=? AND token=?",
                (session_id, token),
            ).fetchone()
        return self.cipher.decrypt(row[0]).decode() if row else None

    def purge_session(self, session_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM mappings WHERE session_id=?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM redaction_cache WHERE session_id=?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM seq_counters WHERE session_id=?", (session_id,)
            )
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict:
        """Durable, non-sensitive totals for the dashboard (counts only)."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM mappings").fetchone()[0]
            sessions = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM mappings").fetchone()[0]
            by_type = dict(self._conn.execute(
                "SELECT entity_type, COUNT(*) FROM mappings "
                "GROUP BY entity_type").fetchall())
            cached = self._conn.execute(
                "SELECT COUNT(*) FROM redaction_cache").fetchone()[0]
        return {
            "total_mappings": total,
            "sessions": sessions,
            "mappings_by_type": by_type,
            "cached_documents": cached,
        }

    # --- per-text redaction cache --------------------------------------
    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def cache_get(self, session_id: str, stage: str, text_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT redacted FROM redaction_cache "
                "WHERE session_id=? AND stage=? AND text_hash=?",
                (session_id, stage, text_hash),
            ).fetchone()
        return self.cipher.decrypt(row[0]).decode() if row else None

    def cache_put(self, session_id: str, stage: str, text_hash: str,
                  redacted: str) -> None:
        enc = self.cipher.encrypt(redacted.encode())
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO redaction_cache "
                "(session_id, stage, text_hash, redacted) VALUES (?,?,?,?)",
                (session_id, stage, text_hash, enc),
            )
            self._conn.commit()
