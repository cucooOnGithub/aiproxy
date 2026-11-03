"""OFFICE (OOXML) docs: embedded images are inspected; other entries kept.

Adapter classifies office media types as PartKind.OFFICE; image_redactor
unzips, runs each media image through the image path, rezips. _redact_image
is spied (no tesseract/Ollama needed)."""

from __future__ import annotations

import io
import zipfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.pipeline.base import Pipeline, SessionContext
from aiproxy.processors.image_redactor import ImageRedactor
from aiproxy.proxy.adapters import AnthropicAdapter

_DOCX_MT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _fake_docx() -> bytes:
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("[Content_Types].xml", "<types/>")
    z.writestr("word/document.xml", "<w:document>Call Alice</w:document>")
    z.writestr("word/media/image1.png", b"\x89PNG-original")
    z.close()
    return buf.getvalue()


def test_adapter_classifies_office():
    a = AnthropicAdapter()
    body = {"messages": [{"role": "user", "content": [
        {"type": "document",
         "source": {"type": "base64", "media_type": _DOCX_MT, "data": ""}}]}]}
    doc = a.to_document(body)
    assert doc.parts[0].kind is PartKind.OFFICE


def test_office_images_inspected_others_kept(monkeypatch):
    red = ImageRedactor({"llm": {}})
    seen = {}

    def spy(data, mt):
        seen["data"] = data
        return b"\x89PNG-masked"

    monkeypatch.setattr(red, "_redact_image", spy)

    doc = Document(parts=[Part(PartKind.OFFICE, data=_fake_docx(),
                               media_type=_DOCX_MT)])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    z = zipfile.ZipFile(io.BytesIO(out.parts[0].data), "r")
    # Embedded image was sent to the image path and replaced.
    assert seen["data"] == b"\x89PNG-original"
    assert z.read("word/media/image1.png") == b"\x89PNG-masked"
    # Non-media entries untouched.
    assert z.read("word/document.xml") == b"<w:document>Call Alice</w:document>"
    assert z.read("[Content_Types].xml") == b"<types/>"
