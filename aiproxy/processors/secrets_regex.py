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

# A real phone number has ~9-15 digits (E.164 caps at 15). The loose PHONE
# pattern also matches version/filename strings like "v2.0 (002) (003)", which
# carry too few digits - and redacting them corrupts the task (the model loses
# the literal number). Require a phone-plausible digit count.
_PHONE_MIN_DIGITS = 9
_PHONE_MAX_DIGITS = 15


def _accept(etype: str, match: str) -> bool:
    """Reject regex matches that fit the shape but aren't really the thing."""
    if etype == "PHONE":
        d = sum(c.isdigit() for c in match)
        return _PHONE_MIN_DIGITS <= d <= _PHONE_MAX_DIGITS
    return True


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
        # Monotonic per-session counter, persisted in the store and shared with
        # the anonymizer. A ctx-local counter would restart each request (ctx
        # is recreated per request) and reissue the same token to different
        # originals, colliding in `mappings` and corrupting restoration.
        return ctx.store.next_seq(ctx.session_id)

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
                    if not _accept(etype, original):
                        continue
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
