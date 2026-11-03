"""Internal normalized representation.

Proxy adapters convert provider-specific request/response bodies into a
Document (provider-agnostic), processors transform it, adapters serialize
it back to the original provider schema. This keeps every processor
independent of whether the caller spoke Anthropic or OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PartKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"


@dataclass
class Part:
    """One piece of a message: a chunk of text or a media blob."""

    kind: PartKind
    # TEXT: the string lives in `text`.
    # IMAGE/PDF: raw bytes in `data`, media_type set (e.g. image/png).
    text: str | None = None
    data: bytes | None = None
    media_type: str | None = None
    # Back-reference so an adapter can re-serialize into the exact slot it
    # came from (role, message index, content index, provider field name).
    origin: dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    """All transformable content of one request or response."""

    parts: list[Part] = field(default_factory=list)
    # Provider + endpoint this came from, so the pipeline can log/branch.
    provider: str = ""
    # Opaque carrier for the original parsed body; adapters own its shape.
    raw: Any = None

    def text_parts(self) -> list[Part]:
        return [p for p in self.parts if p.kind is PartKind.TEXT]

    def media_parts(self) -> list[Part]:
        return [p for p in self.parts if p.kind in (PartKind.IMAGE, PartKind.PDF)]
