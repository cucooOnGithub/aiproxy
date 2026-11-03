"""Processor interface + pipeline runner.

A Processor is the single plug point. Implement two methods, register the
class, list its name in config `pipeline:` and it runs. Request path runs
processors in listed order; response path runs them in REVERSE order so a
restore undoes exactly the redaction that was applied last.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter

from aiproxy.document import Document, PartKind
from aiproxy.processors.tokens import TOKEN_RE

# Content that belongs to Claude's own operation (not user data) and must
# reach the model verbatim, never redacted. The top-level system prompt
# carries the Claude Code system prompt + CLAUDE.md; the tags below wrap
# skill/command listings, system reminders and local command I/O injected
# INTO otherwise-user messages. See `scope:` in config.
_SCOPE_DEFAULTS = {
    "redact_system": False,
    "skip_tags": [
        "system-reminder",
        "command-name",
        "command-args",
        "command-message",
        "local-command-stdout",
        "local-command-stderr",
    ],
    # A whole text block is protected (never redacted) when its stripped text
    # STARTS WITH any of these. Catches operational blocks Claude Code injects
    # untagged into messages. Every other text block — all user and assistant
    # turns — is redacted normally, so repeated PII maps to the same token.
    "skip_prefixes": [
        "# Environment",
        "You are Claude Code",
        "Direct output style is active",
        "[SUGGESTION MODE",
    ],
    # Same, but the block is protected when any of these regexes matches it
    # (re.search, DOTALL). For operational blocks not pinned to the start.
    "skip_regexes": [],
}

# Inert placeholder for a protected span while the redacting processors run.
# Null-byte delimited so it never appears in real text and so the ⟦..⟧ token
# regex can't match it; swapped back to the original before forwarding.
_KEEP_OPEN = "\x00AIPROXY_KEEP_"
_KEEP_CLOSE = "\x00"


class SessionContext:
    """Per-conversation state handed to every processor.

    Holds the reversible mapping store handle and the resolved session id.
    Processors must scope all stored mappings by `session_id`.
    """

    def __init__(self, session_id: str, store, config: dict, archive=None,
                 log=None):
        self.session_id = session_id
        self.store = store
        self.config = config
        self.archive = archive  # DocArchive or None (disk doc cache/archive)
        self.log = log  # RedactionLog or None (debug JSONL of redactions)


class Processor(ABC):
    name: str = "unnamed"
    # Which part kinds this processor touches; the runner skips it for a
    # document with no matching parts.
    handles: frozenset[PartKind] = frozenset({PartKind.TEXT})

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        """Transform outbound (caller -> upstream). Redact here."""

    @abstractmethod
    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        """Transform inbound (upstream -> caller). Restore here."""


class Pipeline:
    def __init__(self, processors: list[Processor]):
        self.processors = processors

    def _relevant(self, p: Processor, doc: Document) -> bool:
        return any(part.kind in p.handles for part in doc.parts)

    @staticmethod
    def _scope(ctx: SessionContext) -> dict:
        cfg = (getattr(ctx, "config", None) or {}).get("scope") or {}
        return {
            "redact_system": cfg.get("redact_system",
                                     _SCOPE_DEFAULTS["redact_system"]),
            "skip_tags": cfg.get("skip_tags", _SCOPE_DEFAULTS["skip_tags"]),
            "skip_prefixes": cfg.get("skip_prefixes",
                                     _SCOPE_DEFAULTS["skip_prefixes"]),
            "skip_regexes": cfg.get("skip_regexes",
                                    _SCOPE_DEFAULTS["skip_regexes"]),
        }

    def _mask(self, doc: Document, ctx: SessionContext) -> dict[int, list]:
        """Swap operational spans for inert placeholders so the redacting
        processors never touch them. Returns {part_index: [(placeholder,
        original), ...]} to restore after processing.

        A text block is protected WHOLE when it is operational content:
          * the top-level system prompt (unless redact_system is true);
          * its stripped text starts with any scope.skip_prefixes entry;
          * any scope.skip_regexes pattern matches it.
        Otherwise the block is redacted normally, except that any
        <tag>...</tag> region for a tag in scope.skip_tags inside it is
        protected in place (CLAUDE.md, skills, commands, reminders).

        Every real turn (user and assistant) and every attached document is
        therefore redacted, so repeated PII maps to the same token; only
        operational blocks pass through verbatim.
        """
        scope = self._scope(ctx)
        tags = [t for t in scope["skip_tags"] if t]
        tag_re = (re.compile(r"<(" + "|".join(re.escape(t) for t in tags)
                             + r")\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
                  if tags else None)
        prefixes = tuple(p for p in scope["skip_prefixes"] if p)
        regexes = [re.compile(r, re.DOTALL) for r in scope["skip_regexes"] if r]

        def _is_operational(text: str) -> bool:
            stripped = text.lstrip()
            if prefixes and stripped.startswith(prefixes):
                return True
            return any(r.search(text) for r in regexes)

        saved: dict[int, list] = {}
        counter = 0
        for i, part in enumerate(doc.parts):
            if part.kind is not PartKind.TEXT or not part.text:
                continue
            is_system = part.origin.get("path") == ("system",)
            spans: list[tuple[str, str]] = []

            protect_whole = (is_system and not scope["redact_system"]) \
                or _is_operational(part.text)

            if protect_whole:
                ph = f"{_KEEP_OPEN}{counter}{_KEEP_CLOSE}"
                counter += 1
                spans.append((ph, part.text))
                part.text = ph
            elif tag_re is not None:
                def _sub(m, _spans=spans):
                    nonlocal counter
                    ph = f"{_KEEP_OPEN}{counter}{_KEEP_CLOSE}"
                    counter += 1
                    _spans.append((ph, m.group(0)))
                    return ph
                part.text = tag_re.sub(_sub, part.text)

            if spans:
                saved[i] = spans
        return saved

    @staticmethod
    def _unmask(doc: Document, saved: dict[int, list]) -> None:
        for i, spans in saved.items():
            text = doc.parts[i].text or ""
            for ph, original in spans:
                text = text.replace(ph, original)
            doc.parts[i].text = text

    def run_request(self, doc: Document, ctx: SessionContext) -> Document:
        log = getattr(ctx, "log", None)
        # Protect Claude's operational content (system prompt, skills,
        # commands, reminders) before anything redacts; restored verbatim
        # at the end so upstream receives it unchanged.
        saved = self._mask(doc, ctx)
        # In debug mode we need the pre-redaction text to show each
        # original<->redacted pair. Snapshot by part index BEFORE processing;
        # processors edit parts in place and never reorder, so indices align.
        originals = None
        if log is not None and getattr(log, "debug", False):
            originals = {i: p.text for i, p in enumerate(doc.parts)
                         if p.kind is PartKind.TEXT}
        for p in self.processors:
            if self._relevant(p, doc):
                doc = p.process_request(doc, ctx)
        if log is not None:
            log.write(self._log_entry(doc, ctx, originals))
        self._unmask(doc, saved)
        return doc

    @staticmethod
    def _log_entry(doc: Document, ctx: SessionContext,
                   originals: dict | None) -> dict:
        """Build a record of what the request redacted.

        Normal mode: PII-free. Logged text is post-redaction (carries
        ⟦TYPE_n⟧ tokens, not PII); documents are named only.
        Debug mode (originals is not None): also logs the ORIGINAL message
        text in plaintext - PII on disk - for messages only; documents are
        still name-only.
        """
        debug = originals is not None
        messages, docs = [], []
        entity_totals: Counter[str] = Counter()
        for i, part in enumerate(doc.parts):
            if part.kind is PartKind.TEXT and part.text is not None:
                entity_totals.update(m.group(1)
                                     for m in TOKEN_RE.finditer(part.text))
                msg = {"role": part.origin.get("role", ""),
                       "redacted_text": part.text}
                if debug:
                    msg["original_text"] = originals.get(i)
                messages.append(msg)
            elif part.kind in (PartKind.IMAGE, PartKind.PDF, PartKind.OFFICE):
                docs.append({
                    "kind": part.kind.value,
                    "name": part.origin.get("filename")
                    or part.origin.get("name") or "",
                })
        return {
            "session": ctx.session_id,
            "provider": doc.provider,
            "debug": debug,
            "entity_counts": dict(entity_totals),
            "messages": messages,
            "documents": docs,
        }

    def run_response(self, doc: Document, ctx: SessionContext) -> Document:
        for p in reversed(self.processors):
            if self._relevant(p, doc):
                doc = p.process_response(doc, ctx)
        return doc
