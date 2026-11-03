"""Scanned (image-only) PDF page is routed through the rasterize path.

A scanned page has no text layer, so get_text() is empty and the text
redaction branch does nothing. This proves the page is instead rasterized
and handed to the image redactor (OCR -> NER -> mask), and the rebuilt page
carries the masked raster. OCR/NER are not exercised here — _redact_image
is spied to confirm the routing and that it receives a real PNG raster.
"""

from __future__ import annotations

import io

import pytest

pymupdf = pytest.importorskip("pymupdf")
PIL = pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from aiproxy.document import Document, Part, PartKind  # noqa: E402
from aiproxy.pipeline.base import Pipeline, SessionContext  # noqa: E402
from aiproxy.processors.image_redactor import ImageRedactor  # noqa: E402


def _scanned_pdf() -> bytes:
    """A page whose only content is an image (no selectable text)."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=100)
    img = Image.new("RGB", (300, 100), "white")
    b = io.BytesIO()
    img.save(b, format="PNG")
    page.insert_image(page.rect, stream=b.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


def _red_png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (300, 100), (255, 0, 0)).save(b, format="PNG")
    return b.getvalue()


def test_scanned_pdf_uses_rasterize_path(monkeypatch):
    src = _scanned_pdf()
    # Sanity: no text layer -> text-redaction branch would be a no-op.
    d = pymupdf.open(stream=src, filetype="pdf")
    assert d[0].get_text().strip() == ""
    d.close()

    red = ImageRedactor({"llm": {}})
    seen = {}

    def spy(data, media_type):
        seen["called"] = True
        seen["is_png"] = data[:4] == b"\x89PNG"
        return _red_png()

    monkeypatch.setattr(red, "_redact_image", spy)

    doc = Document(parts=[Part(PartKind.PDF, data=src,
                               media_type="application/pdf")])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    # Scanned page went through the image path with a real PNG raster.
    assert seen.get("called") is True
    assert seen.get("is_png") is True

    # Rebuilt page embeds the (masked) raster image.
    result = pymupdf.open(stream=out.parts[0].data, filetype="pdf")
    assert len(result) == 1
    assert result[0].get_images(), "rebuilt page should contain an image"
    result.close()
