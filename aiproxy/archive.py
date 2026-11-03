"""Optional on-disk archive + cache of REDACTED documents.

When `archive.dir` is configured, each redacted document (image / PDF /
Office bytes) is written to that directory under a readable name:
`<original-stem>_redacted_<guid><ext>` (the guid keeps repeated redactions
of the same-named file distinct). A content hash of the ORIGINAL input is
still the CACHE KEY, kept in a `.index/` pointer file next to the output, so
if the identical original is presented again the already-redacted version is
read from disk and the local LLM / OCR is not run again.

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
import re
import time
import uuid
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class DocArchive:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        # Pointer files map a cache key -> the readable output filename, kept
        # in a hidden subdir so the archive folder shows only the documents.
        self._index = self.dir / ".index"
        try:
            self._index.mkdir(exist_ok=True)
        except OSError:
            pass

    @staticmethod
    def key(scope: str, data: bytes) -> str:
        h = hashlib.sha256()
        h.update(scope.encode())
        h.update(b":")
        h.update(data)
        return h.hexdigest()

    @staticmethod
    def _filename(name: str | None, ext: str = "") -> str:
        """`<stem>_redacted_<guid><ext>` from an original name (+ fallback ext).

        name may be a bare name or include its own extension; that extension
        wins over `ext`. Unknown name -> 'document'."""
        stem, dot_ext = "", ""
        if name:
            base = Path(str(name)).name
            stem, dot_ext = (base.rsplit(".", 1) + [""])[:2]
            dot_ext = f".{dot_ext}" if dot_ext else ""
        stem = _SAFE.sub("_", stem).strip("._") or "document"
        if not dot_ext:
            dot_ext = ext if (not ext or ext.startswith(".")) else f".{ext}"
        return f"{stem}_redacted_{uuid.uuid4().hex}{dot_ext}"

    def _ptr(self, key: str) -> Path:
        return self._index / key

    def get(self, key: str) -> bytes | None:
        ptr = self._ptr(key)
        try:
            if not ptr.exists():
                return None
            fname = ptr.read_text().strip()
            p = self.dir / fname
            return p.read_bytes() if fname and p.exists() else None
        except OSError:
            return None

    def put(self, key: str, data: bytes,
            name: str | None = None, ext: str = "") -> None:
        try:
            # Content-dedup: if these exact redacted bytes were already
            # written (e.g. the anonymizer and image_redactor both archive the
            # same Office doc), reuse that one file and just add a pointer -
            # no duplicate on disk.
            chash = hashlib.sha256(data).hexdigest()
            cptr = self._index / f"content-{chash}"
            if cptr.exists():
                fname = cptr.read_text().strip()
                if not (self.dir / fname).exists():
                    fname = ""
            else:
                fname = ""
            if not fname:
                fname = self._filename(name, ext)
                (self.dir / fname).write_bytes(data)
                cptr.write_text(fname)
            self._ptr(key).write_text(fname)
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
