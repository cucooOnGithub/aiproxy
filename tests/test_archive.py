"""Archive dir doubles as a disk document cache: a re-presented document is
served from disk and NOT re-processed (no OCR/NER second run)."""

from __future__ import annotations

import io
import tempfile
import zipfile

from aiproxy.archive import DocArchive, build_archive
from aiproxy.document import Document, Part, PartKind
from aiproxy.llm.ollama_ner import Entity
from aiproxy.pipeline.base import Pipeline, SessionContext
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.processors.image_redactor import ImageRedactor
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore

_DOCX_MT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _archive():
    d = tempfile.mkdtemp()
    return DocArchive(d)


def _ctx(archive):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return SessionContext("s", SqliteStore(db, Cipher.null()), {}, archive)


def test_build_archive_toggle():
    assert build_archive({}) is None
    d = tempfile.mkdtemp()
    assert build_archive({"archive": {"dir": d}}) is not None


def test_image_cached_on_disk_skips_second_run(monkeypatch):
    arch = _archive()
    red = ImageRedactor({"llm": {}})
    calls = {"n": 0}

    def fake(data, mt):
        calls["n"] += 1
        return b"\x89PNG-masked"

    monkeypatch.setattr(red, "_redact_image", fake)
    ctx = _ctx(arch)

    img = b"\x89PNG-original-bytes"
    for _ in range(2):
        doc = Document(parts=[Part(PartKind.IMAGE, data=img, media_type="image/png")])
        out = Pipeline([red]).run_request(doc, ctx).parts[0].data
        assert out == b"\x89PNG-masked"
    assert calls["n"] == 1  # second request served from the disk archive


def test_archive_stores_redacted_not_original(monkeypatch):
    arch = _archive()
    red = ImageRedactor({"llm": {}})
    monkeypatch.setattr(red, "_redact_image", lambda d, mt: b"MASKED")
    ctx = _ctx(arch)
    img = b"ORIGINAL-secret"
    Pipeline([red]).run_request(
        Document(parts=[Part(PartKind.IMAGE, data=img, media_type="image/png")]), ctx)
    # The archived bytes are the redacted output, never the original.
    key = arch.key("media", img)
    assert arch.get(key) == b"MASKED"


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("word/document.xml",
               f'<w:document xmlns:w="http://x"><w:t>{text}</w:t></w:document>')
    z.close()
    return buf.getvalue()


def test_office_text_disk_cache_skips_ner(monkeypatch):
    arch = _archive()
    a = Anonymizer({"entities": {"PERSON": "anonymize"}})
    calls = {"n": 0}

    def detect(_t):
        calls["n"] += 1
        return [Entity("Alice", "PERSON")]

    monkeypatch.setattr(a.ner, "detect", detect)
    ctx = _ctx(arch)

    data = _docx("Call Alice")
    for _ in range(2):
        doc = Document(parts=[Part(PartKind.OFFICE, data=data, media_type=_DOCX_MT)])
        out = a.process_request(doc, ctx).parts[0].data
        xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode()
        assert "Alice" not in xml and "⟦PERSON" in xml
    assert calls["n"] == 1  # second presentation hit the disk cache, no NER
