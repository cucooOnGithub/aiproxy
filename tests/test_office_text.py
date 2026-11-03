"""Office body-text redaction: <w:t>/<a:t>/<t> nodes tokenized reversibly;
non-text bytes preserved; tokens restore via the response path."""

from __future__ import annotations

import io
import tempfile
import zipfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.llm.ollama_ner import Entity
from aiproxy.pipeline.base import SessionContext
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore

_DOCX_MT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx(body_runs: list[str]) -> bytes:
    runs = "".join(f"<w:r><w:t>{t}</w:t></w:r>" for t in body_runs)
    doc_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<w:document xmlns:w="http://x"><w:body><w:p>'
               + runs + "</w:p></w:body></w:document>")
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("[Content_Types].xml", "<types/>")
    z.writestr("word/document.xml", doc_xml)
    z.writestr("word/media/image1.png", b"\x89PNG")
    z.close()
    return buf.getvalue()


def _ctx():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return SessionContext("s", SqliteStore(db, Cipher.null()), {})


def test_office_text_tokenized_and_reversible(monkeypatch):
    a = Anonymizer({"entities": {"PERSON": "anonymize", "ORG": "anonymize"}})
    monkeypatch.setattr(a.ner, "detect",
                        lambda _t: [Entity("Alice", "PERSON"),
                                    Entity("Globex", "ORG")])
    ctx = _ctx()

    data = _docx(["Call Alice", " about Globex"])
    doc = Document(parts=[Part(PartKind.OFFICE, data=data, media_type=_DOCX_MT)])
    out = a.process_request(doc, ctx)

    z = zipfile.ZipFile(io.BytesIO(out.parts[0].data), "r")
    xml = z.read("word/document.xml").decode()
    assert "Alice" not in xml and "Globex" not in xml
    assert "⟦PERSON" in xml and "⟦ORG" in xml
    # Non-text entries preserved byte-for-byte.
    assert z.read("word/media/image1.png") == b"\x89PNG"
    assert z.read("[Content_Types].xml") == b"<types/>"
    # XML declaration kept.
    assert xml.startswith("<?xml")

    # Reversible: the tokens the model would echo back restore to originals.
    import re
    from aiproxy.processors.tokens import TOKEN_RE
    restored = TOKEN_RE.sub(
        lambda m: ctx.store.original_for("s", m.group(0)) or m.group(0), xml)
    assert "Alice" in restored and "Globex" in restored


def test_office_no_entities_leaves_text(monkeypatch):
    a = Anonymizer({"entities": {"PERSON": "anonymize"}})
    monkeypatch.setattr(a.ner, "detect", lambda _t: [])
    ctx = _ctx()
    data = _docx(["just plain words"])
    out = a.process_request(
        Document(parts=[Part(PartKind.OFFICE, data=data, media_type=_DOCX_MT)]), ctx)
    xml = zipfile.ZipFile(io.BytesIO(out.parts[0].data)).read("word/document.xml").decode()
    assert "just plain words" in xml
