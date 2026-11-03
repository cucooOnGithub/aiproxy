"""Processor interface + pipeline runner.

A Processor is the single plug point. Implement two methods, register the
class, list its name in config `pipeline:` and it runs. Request path runs
processors in listed order; response path runs them in REVERSE order so a
restore undoes exactly the redaction that was applied last.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from aiproxy.document import Document, PartKind


class SessionContext:
    """Per-conversation state handed to every processor.

    Holds the reversible mapping store handle and the resolved session id.
    Processors must scope all stored mappings by `session_id`.
    """

    def __init__(self, session_id: str, store, config: dict, archive=None):
        self.session_id = session_id
        self.store = store
        self.config = config
        self.archive = archive  # DocArchive or None (disk doc cache/archive)


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

    def run_request(self, doc: Document, ctx: SessionContext) -> Document:
        for p in self.processors:
            if self._relevant(p, doc):
                doc = p.process_request(doc, ctx)
        return doc

    def run_response(self, doc: Document, ctx: SessionContext) -> Document:
        for p in reversed(self.processors):
            if self._relevant(p, doc):
                doc = p.process_response(doc, ctx)
        return doc
