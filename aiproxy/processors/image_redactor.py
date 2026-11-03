"""Image + PDF redaction (one-way).

Pipeline per media part:
  1. OCR (pytesseract) -> words with bounding boxes.
  2. Reuse LLM NER over the OCR'd text to flag sensitive words.
  3. Optionally a local vision model (llava via Ollama) to catch faces /
     logos / handwriting OCR misses. [hook left as TODO]
  4. Paint opaque boxes over flagged regions (Pillow).
  5. PDFs: every page is rasterized (pypdfium2) and run through steps 1-4,
     then reassembled into an image-only PDF. No AGPL dependency.

Image/PDF redaction is NOT reversible — a masked pixel region cannot be
restored. Only text tokens round-trip. The response path is a no-op here.
"""

from __future__ import annotations

import io

from aiproxy.document import Document, Part, PartKind
from aiproxy.llm.ollama_ner import build_ner
from aiproxy.metrics import METRICS
from aiproxy.pipeline.base import Processor, SessionContext


class ImageRedactor(Processor):
    name = "image_redactor"
    handles = frozenset({PartKind.IMAGE, PartKind.PDF, PartKind.OFFICE})

    def __init__(self, config: dict):
        super().__init__(config)
        llm = config.get("llm", {})
        self.ner = build_ner(llm)
        self.max_pages = config.get("max_pages", 20)
        self.dpi = config.get("dpi", 200)  # raster resolution for scanned pages

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        arch = getattr(ctx, "archive", None)
        for part in doc.media_parts():
            if part.data is None:
                continue
            # Disk cache: identical input document -> reuse the redacted file,
            # skip OCR/NER entirely. Media redaction here is one-way, so a
            # content-only scope lets any session reuse it.
            key = arch.key("media", part.data) if arch else None
            if arch:
                cached = arch.get(key)
                if cached is not None:
                    part.data = cached
                    METRICS.inc("document_cache_hits")
                    continue

            if part.kind is PartKind.IMAGE:
                part.data = self._redact_image(part.data, part.media_type)
                METRICS.inc("doc:image")
            elif part.kind is PartKind.PDF:
                part.data = self._redact_pdf(part.data)
                METRICS.inc("doc:pdf")
            elif part.kind is PartKind.OFFICE:
                part.data = self._redact_office(part.data)

            if arch:
                arch.put(key, part.data, part.origin.get("filename"),
                         part.origin.get("ext", ""))
        return doc

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        return doc  # one-way; nothing to restore

    # --- image path -----------------------------------------------------
    def _redact_image(self, data: bytes, media_type: str | None) -> bytes:
        from PIL import Image, ImageDraw
        import pytesseract

        img = Image.open(io.BytesIO(data)).convert("RGB")
        ocr = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        words = [w for w in ocr["text"] if w.strip()]
        flagged = {e.text for e in self.ner.detect(" ".join(words))}
        flag_words = self._flag_word_set(flagged)

        draw = ImageDraw.Draw(img)
        conf = ocr.get("conf", [])
        for i, word in enumerate(ocr["text"]):
            # Skip empty tokens and low-confidence OCR noise.
            if not word.strip():
                continue
            try:
                if conf and float(conf[i]) < 0:
                    continue
            except (ValueError, TypeError):
                pass
            if self._norm(word) in flag_words:
                x, y = ocr["left"][i], ocr["top"][i]
                w, h = ocr["width"][i], ocr["height"][i]
                draw.rectangle([x, y, x + w, y + h], fill="black")

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    @staticmethod
    def _norm(s: str) -> str:
        return s.lower().strip(" .,;:!?\"'()[]")

    def _flag_word_set(self, flagged: set[str]) -> set[str]:
        """Explode multi-word entities ("Acme Corp") into matchable words.

        OCR gives one box per word, so mask per-word; exact (normalized)
        word match avoids the substring over-masking of the old code.
        """
        out: set[str] = set()
        for ent in flagged:
            for w in ent.split():
                n = self._norm(w)
                if n:
                    out.add(n)
        return out

    # --- pdf path -------------------------------------------------------
    def _redact_pdf(self, data: bytes) -> bytes:
        """Rasterize every page and run it through the image path.

        Each page (up to max_pages) is rendered to a raster at self.dpi,
        passed through _redact_image (OCR -> NER -> opaque mask boxes), and
        the masked rasters are reassembled into an image-only PDF. This
        covers text, scanned pages, and embedded images uniformly, and
        leaves no hidden text layer upstream. Tradeoff: the output PDF is
        image-based (no selectable text); text masking is OCR-driven.
        Uses pypdfium2 (Apache-2.0/BSD) + Pillow, no AGPL dependency.
        """
        import pypdfium2 as pdfium
        from PIL import Image

        pdf = pdfium.PdfDocument(data)
        scale = self.dpi / 72.0
        pages: list[Image.Image] = []
        try:
            for i in range(min(len(pdf), self.max_pages)):
                pil = pdf[i].render(scale=scale).to_pil().convert("RGB")
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                masked = self._redact_image(buf.getvalue(), "image/png")
                pages.append(Image.open(io.BytesIO(masked)).convert("RGB"))
        finally:
            pdf.close()
        out = io.BytesIO()
        if pages:
            pages[0].save(out, format="PDF", save_all=True,
                          append_images=pages[1:])
        return out.getvalue()

    # --- office (OOXML: docx/pptx/xlsx) path ---------------------------
    def _redact_office(self, data: bytes) -> bytes:
        """Inspect images embedded in an OOXML office document.

        .docx/.pptx/.xlsx are zip archives; pictures live under word/media/,
        ppt/media/, xl/media/ (and other */media/). Each raster image is run
        through the image path (OCR -> NER -> mask) and written back; every
        other archive entry is copied verbatim. Document body TEXT is NOT
        redacted here (that needs format-aware XML editing) - only images.
        """
        import io as _io
        import logging
        import zipfile

        exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
        src = zipfile.ZipFile(_io.BytesIO(data), "r")
        out_buf = _io.BytesIO()
        out = zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED)
        for item in src.infolist():
            blob = src.read(item.filename)
            lower = item.filename.lower()
            if "/media/" in lower and any(lower.endswith(e) for e in exts):
                try:
                    blob = self._redact_image(blob, "image/png")
                    # masked output is PNG; keep the original path/name
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("aiproxy").warning(
                        "office image %s skipped: %s", item.filename, e)
            out.writestr(item, blob)
        out.close()
        src.close()
        return out_buf.getvalue()
