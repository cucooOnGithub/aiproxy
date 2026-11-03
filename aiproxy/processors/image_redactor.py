"""Image + PDF redaction (one-way).

Pipeline per media part:
  1. OCR (pytesseract) -> words with bounding boxes.
  2. Reuse LLM NER over the OCR'd text to flag sensitive words.
  3. Optionally a local vision model (llava via Ollama) to catch faces /
     logos / handwriting OCR misses. [hook left as TODO]
  4. Paint opaque boxes over flagged regions (Pillow).
  5. PDFs: text-layer pages get TRUE redaction via PyMuPDF (text removed,
     not just covered); scanned pages are rasterized through step 1-4.

Image/PDF redaction is NOT reversible — a masked pixel region cannot be
restored. Only text tokens round-trip. The response path is a no-op here.
"""

from __future__ import annotations

import io

from aiproxy.document import Document, Part, PartKind
from aiproxy.llm.ollama_ner import OllamaNER
from aiproxy.pipeline.base import Processor, SessionContext


class ImageRedactor(Processor):
    name = "image_redactor"
    handles = frozenset({PartKind.IMAGE, PartKind.PDF})

    def __init__(self, config: dict):
        super().__init__(config)
        llm = config.get("llm", {})
        self.ner = OllamaNER(
            url=llm.get("url", "http://localhost:11434"),
            model=llm.get("model", "qwen2.5"),
        )
        self.max_pages = config.get("max_pages", 20)
        self.dpi = config.get("dpi", 200)  # raster resolution for scanned pages

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.media_parts():
            if part.kind is PartKind.IMAGE:
                part.data = self._redact_image(part.data, part.media_type)
            elif part.kind is PartKind.PDF:
                part.data = self._redact_pdf(part.data)
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
        import pymupdf  # PyMuPDF (>=1.24; `import fitz` is deprecated)

        src = pymupdf.open(stream=data, filetype="pdf")
        out = pymupdf.open()
        for page in src[: self.max_pages]:
            text = page.get_text()
            if text.strip():
                # Text-layer page: true redaction removes the glyphs.
                flagged = {e.text for e in self.ner.detect(text)}
                for term in flagged:
                    for rect in page.search_for(term):
                        page.add_redact_annot(rect, fill=(0, 0, 0))
                page.apply_redactions()
                out.insert_pdf(src, from_page=page.number, to_page=page.number)
            else:
                # Scanned/image-only page: rasterize, run the image path
                # (OCR -> NER -> mask), rebuild the page from the masked raster.
                pix = page.get_pixmap(dpi=self.dpi)
                masked = self._redact_image(pix.tobytes("png"), "image/png")
                new = out.new_page(width=page.rect.width, height=page.rect.height)
                new.insert_image(page.rect, stream=masked)
        buf = io.BytesIO()
        out.save(buf)
        out.close()
        src.close()
        return buf.getvalue()
