"""Shared token format for reversible redaction.

Tokens the model sees are human-readable and sequential per entity type
(e.g. PERSON_1) so the LLM reasons naturally, NOT raw GUIDs. The GUID is
the stable storage key; the readable label maps to it in the store.

Sentinel delimiters use rare bracket chars so restoration can find tokens
with a single regex and they survive provider tokenization as a unit.
"""

from __future__ import annotations

import re

_OPEN = "⟦"  # ⟦
_CLOSE = "⟧"  # ⟧

TOKEN_RE = re.compile(rf"{_OPEN}([A-Z]+)_(\d+){_CLOSE}")


def make_token(entity_type: str, seq: int) -> str:
    return f"{_OPEN}{entity_type}_{seq}{_CLOSE}"
