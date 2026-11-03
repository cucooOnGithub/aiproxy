"""Reversible token<->original mapping store (SQLite).

This store IS the de-anonymization key. Treat its file as a secret:
anyone with it can reverse every redaction. Originals are encrypted at
rest when a key is configured (see crypto.py). GUID tokens are useless
without this store, so losing it = permanent anonymization.
"""

from __future__ import annotations

import hashlib
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
"""


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
            self._conn.commit()
            return cur.rowcount

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
