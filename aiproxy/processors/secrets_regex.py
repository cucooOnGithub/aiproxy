"""Deterministic secret stripper — runs before the LLM anonymizer.

Catches high-confidence patterns (API keys, emails, IBANs, phone numbers)
with regex: cheaper, faster, and more reliable than an LLM for things
with a fixed shape. Reversible via the same token+store mechanism as the
anonymizer.
"""

from __future__ import annotations

import re

from aiproxy.document import Document, PartKind
from aiproxy.pipeline.base import Processor, SessionContext
from aiproxy.processors.tokens import TOKEN_RE, make_token

_PATTERNS: dict[str, re.Pattern] = {
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "APIKEY": re.compile(r"\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9]{16,}\b"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    "PHONE": re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"),
}


class SecretsRegex(Processor):
    name = "secrets_regex"
    handles = frozenset({PartKind.TEXT})

    def _seq(self, ctx: SessionContext) -> int:
        # Monotonic per-session counter kept on the context object.
        n = getattr(ctx, "_seq_counter", 0) + 1
        ctx._seq_counter = n  # type: ignore[attr-defined]
        return n

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        enabled = self.config.get("patterns") or list(_PATTERNS)
        for part in doc.text_parts():
            text = part.text or ""
            for etype in enabled:
                pat = _PATTERNS.get(etype)
                if not pat:
                    continue
                for m in list(pat.finditer(text)):
                    original = m.group(0)
                    token = ctx.store.token_for(ctx.session_id, original, etype)
                    if token is None:
                        token = make_token(etype, self._seq(ctx))
                        ctx.store.put(ctx.session_id, token, original, etype)
                    text = text.replace(original, token)
            part.text = text
        return doc

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            text = part.text or ""

            def repl(m: re.Match) -> str:
                orig = ctx.store.original_for(ctx.session_id, m.group(0))
                return orig if orig is not None else m.group(0)

            part.text = TOKEN_RE.sub(repl, text)
        return doc
