"""Local-LLM named-entity recognition via Ollama.

Asks a local model to return JSON entity spans. Output is constrained
with a JSON schema (Ollama `format`) so it parses deterministically.
Results are cached by text hash so repeated conversation turns don't
re-run the model.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

import httpx

_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string"},
                },
                "required": ["text", "type"],
            },
        }
    },
    "required": ["entities"],
}

_PROMPT = (
    "You are a PII detector. Find every person name, organization/company "
    "name, location, email, and phone number in the text. Return JSON only. "
    "Use types: PERSON, ORG, LOCATION, EMAIL, PHONE. Copy the entity text "
    "exactly as it appears. Text:\n\n{text}"
)


@dataclass
class Entity:
    text: str
    type: str


class OllamaNER:
    def __init__(self, url: str, model: str, timeout: float = 60.0,
                 warm_timeout: float = 300.0, keep_alive=None,
                 cache_ttl: float | None = 3600.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout          # per-detect() request budget
        self.warm_timeout = warm_timeout  # longer; covers cold model load
        # Ollama keep_alive: how long the model stays resident after a call.
        # Set 0 (or "0s") so an ensemble unloads each model before loading
        # the next on a VRAM-tight GPU. None = Ollama default (5m).
        self.keep_alive = keep_alive
        # In-memory NER result cache. Survives Ollama unloading the model
        # (keep_alive:0), so a reloaded model never re-runs NER on text it
        # already saw. cache_ttl bounds entry age in seconds; None = forever.
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, list[Entity]]] = {}

    def _ka(self, body: dict) -> dict:
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        return body

    def warm(self) -> None:
        """Force the model to load now (blocking) so the first real detect()
        doesn't pay the cold-load cost against the shorter per-request
        timeout. Safe to call repeatedly; errors propagate to the caller."""
        httpx.post(
            f"{self.url}/api/chat",
            json=self._ka({"model": self.model, "stream": False,
                           "messages": [{"role": "user", "content": "ok"}]}),
            timeout=self.warm_timeout,
        ).raise_for_status()

    def detect(self, text: str) -> list[Entity]:
        if not text.strip():
            return []
        key = hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest()
        hit = self._cache.get(key)
        if hit is not None:
            ts, ents = hit
            if self.cache_ttl is None or (time.monotonic() - ts) < self.cache_ttl:
                return ents
            del self._cache[key]  # expired

        resp = httpx.post(
            f"{self.url}/api/chat",
            json=self._ka({
                "model": self.model,
                "format": _SCHEMA,
                "stream": False,
                "messages": [
                    {"role": "user", "content": _PROMPT.format(text=text)}
                ],
            }),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        try:
            data = json.loads(content)
            ents = [Entity(e["text"], e["type"]) for e in data.get("entities", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            # Fail-closed callers treat an empty list differently; here we
            # surface nothing found and let the pipeline's mode decide.
            ents = []
        self._cache[key] = (time.monotonic(), ents)
        return ents


class MultiNER:
    """Run several NER backends and UNION their entities (higher recall).

    Same duck-typed shape as OllamaNER (detect/warm/model) so the
    anonymizer treats it identically. Dedupe is by normalized text; a
    non-OTHER type wins over OTHER when the same text is seen twice.

    Cost: latency is the SUM of all backends, and holding several models
    on a small GPU makes Ollama load/unload (thrash). Order the list
    cheap-model-first.
    """

    def __init__(self, backends: list[OllamaNER]):
        if not backends:
            raise ValueError("MultiNER needs at least one backend")
        self.backends = backends
        self.model = "+".join(b.model for b in backends)

    def warm(self) -> None:
        for b in self.backends:
            b.warm()

    def detect(self, text: str) -> list[Entity]:
        best: dict[str, Entity] = {}
        for b in self.backends:
            for e in b.detect(text):
                k = e.text.strip().lower()
                if not k:
                    continue
                prev = best.get(k)
                if prev is None or (prev.type == "OTHER" and e.type != "OTHER"):
                    best[k] = e
        return list(best.values())


def build_ner(llm: dict) -> "OllamaNER | MultiNER":
    """Construct the NER backend from the `llm` config block.

    - `models: [a, b, ...]` -> MultiNER union over those models
    - `model: x` (or neither) -> a single OllamaNER
    Timeouts/url are shared across all backends.
    """
    url = llm.get("url", "http://localhost:11434")
    timeout = llm.get("timeout", 60.0)
    warm_timeout = llm.get("warm_timeout", 300.0)
    keep_alive = llm.get("keep_alive")
    cache_ttl = llm.get("cache_ttl", 3600.0)

    def _one(model: str) -> OllamaNER:
        return OllamaNER(url=url, model=model, timeout=timeout,
                         warm_timeout=warm_timeout, keep_alive=keep_alive,
                         cache_ttl=cache_ttl)

    models = llm.get("models")
    if models:
        return MultiNER([_one(m) for m in models])
    return _one(llm.get("model", "qwen2.5"))
