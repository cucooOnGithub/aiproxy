"""PDF part flows through the pipeline into image_redactor and the flagged
text is PHYSICALLY removed (true redaction), not just covered.

NER is mocked (no Ollama). PyMuPDF is required; test skips without it.
"""

from __future__ import annotations

import pytest

pymupdf = pytest.importorskip("pymupdf")

from aiproxy.document import Document, Part, PartKind  # noqa: E402
from aiproxy.llm.ollama_ner import Entity  # noqa: E402
from aiproxy.pipeline.base import Pipeline, SessionContext  # noqa: E402
from aiproxy.processors.image_redactor import ImageRedactor  # noqa: E402


def _pdf_with(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_flagged_text_removed(monkeypatch):
    src = _pdf_with("Call Alice about lunch")
    # Sanity: the name is present before redaction.
    before = pymupdf.open(stream=src, filetype="pdf")
    assert "Alice" in before[0].get_text()
    before.close()

    red = ImageRedactor({"llm": {}})
    monkeypatch.setattr(red.ner, "detect", lambda _t: [Entity("Alice", "PERSON")])

    doc = Document(parts=[Part(PartKind.PDF, data=src,
                               media_type="application/pdf")])
    out = Pipeline([red]).run_request(doc, SessionContext("s", None, {}))

    after = pymupdf.open(stream=out.parts[0].data, filetype="pdf")
    text = after[0].get_text()
    after.close()
    assert "Alice" not in text      # physically gone
    assert "lunch" in text          # untouched
