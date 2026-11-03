"""A text-layer PDF that also embeds a raster image: the image is inspected
(masked) in addition to text redaction."""

from __future__ import annotations

import io

import pytest

pymupdf = pytest.importorskip("pymupdf")
PIL = pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from aiproxy.document import Document, Part, PartKind  # noqa: E402
from aiproxy.llm.ollama_ner import Entity  # noqa: E402
from aiproxy.pipeline.base import Pipeline, SessionContext  # noqa: E402
from aiproxy.processors.image_redactor import ImageRedactor  # noqa: E402


def _pdf_text_and_image() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((72, 72), "Call Alice", fontsize=14)
    img = Image.new("RGB", (100, 60), "white")
    b = io.BytesIO(); img.save(b, format="PNG")
    page.insert_image(pymupdf.Rect(40, 100, 140, 160), stream=b.getvalue())
    data = doc.tobytes(); doc.close()
    return data


def test_text_pdf_embedded_image_inspected(monkeypatch):
    red = ImageRedactor({"llm": {}})
    monkeypatch.setattr(red.ner, "detect", lambda _t: [Entity("Alice", "PERSON")])
    hits = {"n": 0}

    # _redact_image needs tesseract; replace with a counter returning a png.
    def fake(data, mt):
        hits["n"] += 1
        b = io.BytesIO(); Image.new("RGB", (2, 2), "black").save(b, "PNG")
        return b.getvalue()

    monkeypatch.setattr(red, "_redact_image", fake)

    doc = Document(parts=[Part(PartKind.PDF, data=_pdf_text_and_image(),
                               media_type="application/pdf")])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    # Embedded image was routed through the image path at least once.
    assert hits["n"] >= 1
    # Text redaction still happened.
    res = pymupdf.open(stream=out.parts[0].data, filetype="pdf")
    assert "Alice" not in res[0].get_text()
    res.close()
