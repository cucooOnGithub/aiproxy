"""No-op processor that logs part counts. Proves the interface + ordering.

Never logs content — only shapes — so it is safe to leave enabled.
"""

from __future__ import annotations

import logging

from aiproxy.document import Document, PartKind
from aiproxy.pipeline.base import Processor, SessionContext

log = logging.getLogger("aiproxy.logger")


class Logger(Processor):
    name = "logger"
    handles = frozenset({PartKind.TEXT, PartKind.IMAGE, PartKind.PDF})

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        log.info("req session=%s parts=%d", ctx.session_id, len(doc.parts))
        return doc

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        log.info("resp session=%s parts=%d", ctx.session_id, len(doc.parts))
        return doc
