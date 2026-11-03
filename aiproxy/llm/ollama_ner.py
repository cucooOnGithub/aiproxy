"""Local-LLM named-entity recognition via Ollama.

Asks a local model to return JSON entity spans. Output is constrained
with a JSON schema (Ollama `format`) so it parses deterministically.
Results are cached by text hash so repeated conversation turns don't
re-run the model.
"""

from __future__ import annotations

import hashlib
import json
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
    def __init__(self, url: str, model: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._cache: dict[str, list[Entity]] = {}

    def detect(self, text: str) -> list[Entity]:
        if not text.strip():
            return []
        key = hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]

        resp = httpx.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "format": _SCHEMA,
                "stream": False,
                "messages": [
                    {"role": "user", "content": _PROMPT.format(text=text)}
                ],
            },
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
        self._cache[key] = ents
        return ents
