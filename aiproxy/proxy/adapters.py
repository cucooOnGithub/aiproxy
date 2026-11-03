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
            content = msg.get("content")
            if isinstance(content, str):
                doc.parts.append(
                    Part(PartKind.TEXT, text=content,
                         origin={"path": ("messages", mi, "content")})
                )
            elif isinstance(content, list):
                for ci, block in enumerate(content):
                    doc.parts.append(self._block_to_part(block, mi, ci))
        # System prompt is a top-level string (or list) in Anthropic.
        sys = body.get("system")
        if isinstance(sys, str):
            doc.parts.append(
                Part(PartKind.TEXT, text=sys, origin={"path": ("system",)})
            )
        return doc

    def _block_to_part(self, block: dict, mi: int, ci: int) -> Part:
        path = ("messages", mi, "content", ci)
        btype = block.get("type")
        if btype == "text":
            return Part(PartKind.TEXT, text=block["text"], origin={"path": path})
        if btype == "image":
            src = block.get("source", {})
            data = base64.b64decode(src.get("data", "")) if src.get("data") else None
            return Part(PartKind.IMAGE, data=data,
                        media_type=src.get("media_type"), origin={"path": path})
        if btype == "document":  # PDF
            src = block.get("source", {})
            data = base64.b64decode(src.get("data", "")) if src.get("data") else None
            return Part(PartKind.PDF, data=data,
                        media_type=src.get("media_type"), origin={"path": path})
        # Unknown block: keep as inert text so it round-trips untouched.
        return Part(PartKind.TEXT, text="", origin={"path": path, "inert": True})

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
            content = msg.get("content")
            if isinstance(content, str):
                doc.parts.append(
                    Part(PartKind.TEXT, text=content,
                         origin={"path": ("messages", mi, "content")})
                )
            elif isinstance(content, list):
                for ci, block in enumerate(content):
                    doc.parts.append(self._block_to_part(block, mi, ci))
        return doc

    def _block_to_part(self, block: dict, mi: int, ci: int) -> Part:
        path = ("messages", mi, "content", ci)
        btype = block.get("type")
        if btype == "text":
            return Part(PartKind.TEXT, text=block.get("text", ""),
                        origin={"path": path})
        if btype == "image_url":
            url = block.get("image_url", {}).get("url", "")
            parsed = _parse_data_url(url)
            if parsed:
                media_type, data = parsed
                return Part(PartKind.IMAGE, data=data, media_type=media_type,
                            origin={"path": path})
            # Remote URL: cannot redact; leave untouched.
            return Part(PartKind.TEXT, text="", origin={"path": path, "inert": True})
        return Part(PartKind.TEXT, text="", origin={"path": path, "inert": True})

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
