"""Optional on-disk archive + cache of REDACTED documents.

When `archive.dir` is configured, each redacted document (image / PDF /
Office bytes) is written to that directory, named by a hash of its ORIGINAL
input. That same store is the cache: if the identical original is presented
again, the already-redacted version is read from disk and the local LLM /
OCR is not run again.

Only redacted output is written — never the original PII-bearing bytes.

Scoping via the `scope` argument:
  - one-way media (image/PDF) uses a content-only scope, so any session
    reusing the same file gets the cache hit;
  - reversible content (Office body text, which carries session-specific
    tokens) is scoped by session id, so a cached hit's tokens still resolve
    against that session's mappings.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class DocArchive:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(scope: str, data: bytes) -> str:
        h = hashlib.sha256()
        h.update(scope.encode())
        h.update(b":")
        h.update(data)
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / key

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            return p.read_bytes() if p.exists() else None
        except OSError:
            return None

    def put(self, key: str, data: bytes) -> None:
        try:
            self._path(key).write_bytes(data)
        except OSError:
            pass  # archive is best-effort; never block a request on disk


def build_archive(cfg: dict) -> "DocArchive | None":
    directory = (cfg.get("archive") or {}).get("dir")
    return DocArchive(directory) if directory else None


class RedactionLog:
    """Append-only JSONL debug log of WHAT each request redacted.

    One JSON line per request. It records only token-bearing (already
    redacted) text and document metadata so an operator can confirm the
    pipeline is masking the right things. It NEVER writes an original PII
    value: those remain encrypted in the mapping store (map.db) and can be
    recovered from there with AIPROXY_STORE_KEY. Keeping originals out of
    this plaintext log is the whole point - do not change that.
    """

    def __init__(self, path: str, debug: bool = False, log_all: bool = False):
        self.path = Path(path)
        # WARNING: debug=True also writes ORIGINAL message text (plaintext
        # PII) into this log. Off by default; opt in only in a trusted env.
        self.debug = debug
        # log_all=True logs EVERY text block, not just the redacted ones
        # (operational blocks still show as placeholders in normal mode).
        self.log_all = log_all
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def write(self, entry: dict) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # best-effort; never block a request on disk


def build_redaction_log(cfg: dict) -> "RedactionLog | None":
    rl = cfg.get("redaction_log") or {}
    path = rl.get("path")
    return RedactionLog(path, debug=bool(rl.get("debug")),
                        log_all=bool(rl.get("log_all"))) if path else None
