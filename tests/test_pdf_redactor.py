"""PDF part flows through the pipeline into image_redactor.

Every page is rasterized and run through the image path (OCR -> NER -> mask),
then the masked rasters are reassembled into an image-only PDF. NER/OCR are
not exercised: _redact_image is spied to confirm each page is routed through
it with a real PNG raster, and the output is checked to be a valid multi-page
PDF. Uses pypdfium2 + Pillow (no PyMuPDF / AGPL).
"""

from __future__ import annotations

import io

import pytest

pdfium = pytest.importorskip("pypdfium2")
PIL = pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from aiproxy.document import Document, Part, PartKind  # noqa: E402
from aiproxy.pipeline.base import Pipeline, SessionContext  # noqa: E402
from aiproxy.processors.image_redactor import ImageRedactor  # noqa: E402


def _pdf(n_pages: int) -> bytes:
    """A PDF with n_pages solid-white pages, built with Pillow."""
    pages = [Image.new("RGB", (300, 200), "white") for _ in range(n_pages)]
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def _red_png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (300, 200), (255, 0, 0)).save(b, format="PNG")
    return b.getvalue()


def test_every_page_rasterized_through_image_path(monkeypatch):
    red = ImageRedactor({"llm": {}})
    seen = {"calls": 0, "all_png": True}

    def spy(data, media_type):
        seen["calls"] += 1
        if data[:4] != b"\x89PNG":
            seen["all_png"] = False
        return _red_png()

    monkeypatch.setattr(red, "_redact_image", spy)

    doc = Document(parts=[Part(PartKind.PDF, data=_pdf(3),
                               media_type="application/pdf")])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    # One image-path call per page, each handed a real PNG raster.
    assert seen["calls"] == 3
    assert seen["all_png"] is True

    # Output is a valid PDF with the same page count.
    result = pdfium.PdfDocument(out.parts[0].data)
    assert len(result) == 3
    result.close()


def test_max_pages_caps_work(monkeypatch):
    red = ImageRedactor({"llm": {}, "max_pages": 2})
    calls = {"n": 0}

    def spy(data, media_type):
        calls["n"] += 1
        return _red_png()

    monkeypatch.setattr(red, "_redact_image", spy)

    doc = Document(parts=[Part(PartKind.PDF, data=_pdf(5),
                               media_type="application/pdf")])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    assert calls["n"] == 2
    result = pdfium.PdfDocument(out.parts[0].data)
    assert len(result) == 2
    result.close()
