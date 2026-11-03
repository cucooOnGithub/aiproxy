"""Provider schema <-> Document adapters.

Each adapter knows how to pull every transformable text/media part OUT of
a provider request body into a Document, and how to write transformed
parts BACK into a body of the same schema. Parts carry an `origin` back-
reference so writing back lands in the exact slot.

Only Anthropic Messages is fleshed out here; OpenAI follows the same
shape (choices/messages/content). PDF/image extraction from base64
`source` blocks is stubbed where marked TODO.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod

from aiproxy.document import Document, Part, PartKind


class Adapter(ABC):
    provider: str

    @abstractmethod
    def to_document(self, body: dict) -> Document: ...

    @abstractmethod
    def from_document(self, doc: Document) -> dict:
        """Write transformed parts back into doc.raw and return it."""

    @abstractmethod
    def response_to_document(self, body: dict) -> Document: ...

    @abstractmethod
    def response_from_document(self, doc: Document) -> dict: ...


class AnthropicAdapter(Adapter):
    provider = "anthropic"

    def to_document(self, body: dict) -> Document:
        doc = Document(provider=self.provider, raw=body)
        for mi, msg in enumerate(body.get("messages", [])):
            role = msg.get("role", "")
            content = msg.get("content")
            if isinstance(content, str):
                doc.parts.append(
                    Part(PartKind.TEXT, text=content,
                         origin={"path": ("messages", mi, "content"),
                                 "role": role})
                )
            elif isinstance(content, list):
                for ci, block in enumerate(content):
                    doc.parts.append(self._block_to_part(block, mi, ci, role))
        # System prompt is a top-level string (or list) in Anthropic.
        sys = body.get("system")
        if isinstance(sys, str):
            doc.parts.append(
                Part(PartKind.TEXT, text=sys,
                     origin={"path": ("system",), "role": "system"})
            )
        return doc

    def _block_to_part(self, block: dict, mi: int, ci: int,
                       role: str = "") -> Part:
        path = ("messages", mi, "content", ci)
        btype = block.get("type")
        if btype == "text":
            return Part(PartKind.TEXT, text=block["text"],
                        origin={"path": path, "role": role})
        if btype == "image":
            src = block.get("source", {})
            data = base64.b64decode(src.get("data", "")) if src.get("data") else None
            mt = src.get("media_type")
            return Part(PartKind.IMAGE, data=data, media_type=mt,
                        origin={"path": path, "role": role,
                                "filename": _doc_name(block),
                                "ext": _ext_for(mt)})
        if btype == "document":  # PDF or OOXML office doc
            src = block.get("source", {})
            data = base64.b64decode(src.get("data", "")) if src.get("data") else None
            mt = src.get("media_type") or ""
            kind = PartKind.OFFICE if _is_office(mt) else PartKind.PDF
            return Part(kind, data=data, media_type=mt,
                        origin={"path": path, "role": role,
                                "filename": _doc_name(block),
                                "ext": _ext_for(mt)})
        # Unknown block: keep as inert text so it round-trips untouched.
        return Part(PartKind.TEXT, text="",
                    origin={"path": path, "role": role, "inert": True})

    def from_document(self, doc: Document) -> dict:
        body = doc.raw
        for part in doc.parts:
            if part.origin.get("inert"):
                continue
            self._write_part(body, part)
        return body

    def _write_part(self, body: dict, part: Part) -> None:
        path = part.origin["path"]
        if path == ("system",):
            body["system"] = part.text
            return
        if len(path) == 3:  # messages[mi].content (string form)
            _, mi, _ = path
            body["messages"][mi]["content"] = part.text
            return
        # content block form
        _, mi, _, ci = path
        block = body["messages"][mi]["content"][ci]
        if part.kind is PartKind.TEXT:
            block["text"] = part.text
        else:
            block["source"]["data"] = base64.b64encode(part.data or b"").decode()

    def response_to_document(self, body: dict) -> Document:
        doc = Document(provider=self.provider, raw=body)
        for ci, block in enumerate(body.get("content", [])):
            if block.get("type") == "text":
                doc.parts.append(
                    Part(PartKind.TEXT, text=block["text"],
                         origin={"path": ("content", ci)})
                )
        return doc

    def response_from_document(self, doc: Document) -> dict:
        body = doc.raw
        for part in doc.parts:
            _, ci = part.origin["path"]
            body["content"][ci]["text"] = part.text
        return body


_OFFICE_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
})

# media type -> file extension, for naming archived redacted output when the
# original carried no filename.
_EXT_BY_TYPE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
}


def _is_office(media_type: str) -> bool:
    return media_type in _OFFICE_TYPES


def _ext_for(media_type: str | None) -> str:
    return _EXT_BY_TYPE.get(media_type or "", "")


def _doc_name(block: dict) -> str | None:
    """Best-effort original filename for a document/image block (clients vary;
    the Anthropic API exposes `title`)."""
    src = block.get("source", {}) if isinstance(block.get("source"), dict) else {}
    return (block.get("title") or block.get("filename")
            or src.get("filename") or src.get("title"))


def _parse_data_url(url: str) -> tuple[str, bytes] | None:
    """`data:image/png;base64,XXXX` -> (media_type, bytes); None if not one."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    head, b64 = url.split(",", 1)
    media_type = head[len("data:"):].split(";", 1)[0]
    return media_type, base64.b64decode(b64)


class OpenAIAdapter(Adapter):
    """OpenAI Chat Completions schema.

    Text lives in `messages[].content` (string) or in content-part lists
    (`{type:text}` / `{type:image_url}`). Images are redactable only when
    inlined as a `data:` URL; remote `http(s)` image URLs can't be masked
    locally and pass through untouched (marked inert). PDFs aren't part of
    this API.
    """

    provider = "openai"

    def to_document(self, body: dict) -> Document:
        doc = Document(provider=self.provider, raw=body)
        for mi, msg in enumerate(body.get("messages", [])):
            role = msg.get("role", "")
            content = msg.get("content")
            if isinstance(content, str):
                doc.parts.append(
                    Part(PartKind.TEXT, text=content,
                         origin={"path": ("messages", mi, "content"),
                                 "role": role})
                )
            elif isinstance(content, list):
                for ci, block in enumerate(content):
                    doc.parts.append(self._block_to_part(block, mi, ci, role))
        return doc

    def _block_to_part(self, block: dict, mi: int, ci: int,
                       role: str = "") -> Part:
        path = ("messages", mi, "content", ci)
        btype = block.get("type")
        if btype == "text":
            return Part(PartKind.TEXT, text=block.get("text", ""),
                        origin={"path": path, "role": role})
        if btype == "image_url":
            url = block.get("image_url", {}).get("url", "")
            parsed = _parse_data_url(url)
            if parsed:
                media_type, data = parsed
                return Part(PartKind.IMAGE, data=data, media_type=media_type,
                            origin={"path": path, "role": role,
                                    "ext": _ext_for(media_type)})
            # Remote URL: cannot redact; leave untouched.
            return Part(PartKind.TEXT, text="",
                        origin={"path": path, "role": role, "inert": True})
        return Part(PartKind.TEXT, text="",
                    origin={"path": path, "role": role, "inert": True})

    def from_document(self, doc: Document) -> dict:
        body = doc.raw
        for part in doc.parts:
            if part.origin.get("inert"):
                continue
            path = part.origin["path"]
            if len(path) == 3:  # messages[mi].content string
                _, mi, _ = path
                body["messages"][mi]["content"] = part.text
                continue
            _, mi, _, ci = path
            block = body["messages"][mi]["content"][ci]
            if part.kind is PartKind.TEXT:
                block["text"] = part.text
            else:  # image data URL
                b64 = base64.b64encode(part.data or b"").decode()
                block["image_url"]["url"] = f"data:{part.media_type};base64,{b64}"
        return body

    def response_to_document(self, body: dict) -> Document:
        doc = Document(provider=self.provider, raw=body)
        for idx, choice in enumerate(body.get("choices", [])):
            content = choice.get("message", {}).get("content")
            if isinstance(content, str):
                doc.parts.append(
                    Part(PartKind.TEXT, text=content,
                         origin={"path": ("choices", idx, "message", "content")})
                )
        return doc

    def response_from_document(self, doc: Document) -> dict:
        body = doc.raw
        for part in doc.parts:
            _, idx, _, _ = part.origin["path"]
            body["choices"][idx]["message"]["content"] = part.text
        return body


ADAPTERS: dict[str, Adapter] = {
    "anthropic": AnthropicAdapter(),
    "openai": OpenAIAdapter(),
}
