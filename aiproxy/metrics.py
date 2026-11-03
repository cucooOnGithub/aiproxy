"""Process-wide anonymization counters for the dashboard.

A single in-process, thread-safe counter bag. Runtime counts reset when the
proxy restarts; durable totals (tokens per entity type, cached documents,
sessions) come from the SQLite store instead — the dashboard merges both.

Counts are NON-SENSITIVE: how many entities/documents/requests, never the
values themselves.
"""

from __future__ import annotations

import threading
import time
from collections import Counter


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c: Counter = Counter()
        self.started = time.time()

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._c[key] += n

    def snapshot(self) -> dict:
        with self._lock:
            c = dict(self._c)
        out = {
            "uptime_seconds": int(time.time() - self.started),
            "requests_total": c.get("requests", 0),
            "requests_by_provider": _prefix(c, "req:"),
            "text_parts_redacted": c.get("text_parts_redacted", 0),
            "documents_by_kind": _prefix(c, "doc:"),
            "entities_by_type": _prefix(c, "entity:"),
            "tokens_created": c.get("tokens_created", 0),
            "cache_hits": c.get("cache_hits", 0),
            "document_cache_hits": c.get("document_cache_hits", 0),
            "ner_blocks": c.get("ner_blocks", 0),
        }
        return out


def _prefix(c: dict, p: str) -> dict:
    return {k[len(p):]: v for k, v in sorted(c.items()) if k.startswith(p)}


# Single shared instance (single-process uvicorn).
METRICS = Metrics()
