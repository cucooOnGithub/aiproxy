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
