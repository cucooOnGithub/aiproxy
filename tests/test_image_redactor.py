"""Inline image flows through the pipeline into image_redactor and gets masked.

OCR (pytesseract) and NER (Ollama) are both mocked, so no tesseract binary
and no local LLM are needed. Proves: a Document IMAGE part reaches the
redactor via Pipeline, flagged words are blacked out, non-flagged words are
left alone.
"""

from __future__ import annotations

import io

import pytest

PIL = pytest.importorskip("PIL")
pytest.importorskip("pytesseract")

from PIL import Image  # noqa: E402

from aiproxy.document import Document, Part, PartKind  # noqa: E402
from aiproxy.llm.ollama_ner import Entity  # noqa: E402
from aiproxy.pipeline.base import Pipeline, SessionContext  # noqa: E402
from aiproxy.processors.image_redactor import ImageRedactor  # noqa: E402


def _white_png() -> bytes:
    img = Image.new("RGB", (200, 60), "white")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _fake_ocr(_img, **_kw):
    # Two words: "Alice" at x=10, "lunch" at x=120.
    return {
        "text": ["Alice", "lunch"],
        "left": [10, 120], "top": [20, 20],
        "width": [60, 50], "height": [20, 20],
        "conf": [96, 95],
    }


def test_inline_image_masked_through_pipeline(monkeypatch):
    import pytesseract

    monkeypatch.setattr(pytesseract, "image_to_data", _fake_ocr)

    red = ImageRedactor({"llm": {}})
    # NER flags only the person name.
    monkeypatch.setattr(red.ner, "detect", lambda _t: [Entity("Alice", "PERSON")])

    doc = Document(parts=[Part(PartKind.IMAGE, data=_white_png(),
                               media_type="image/png")])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    img = Image.open(io.BytesIO(out.parts[0].data)).convert("RGB")
    # "Alice" box (10,20)-(70,40) must now be black at its center.
    assert img.getpixel((40, 30)) == (0, 0, 0)
    # "lunch" box (120,20)-(170,40) must stay white.
    assert img.getpixel((145, 30)) == (255, 255, 255)
