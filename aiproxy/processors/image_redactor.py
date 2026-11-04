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
from aiproxy.processors.secrets_regex import _PATTERNS, _accept


class ImageRedactor(Processor):
    name = "image_redactor"
    handles = frozenset({PartKind.IMAGE, PartKind.PDF, PartKind.OFFICE})

    def __init__(self, config: dict):
        super().__init__(config)
        llm = config.get("llm", {})
        self.ner = build_ner(llm)
        self.max_pages = config.get("max_pages", 20)
        self.dpi = config.get("dpi", 200)  # raster resolution for scanned pages
        # Deterministic regex backstop over OCR text: the small NER model
        # misses emails, card numbers, IBANs and API keys in images/PDFs, and
        # (unlike the plain-text path) secrets_regex never sees OCR'd content.
        # So we run the same fixed-shape patterns here and mask any OCR word
        # that falls inside a match. Default: all patterns (incl. CREDITCARD).
        self.ocr_regex = config.get("ocr_regex", list(_PATTERNS))

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

        # Join the non-empty OCR words into one string (the NER sees this), and
        # remember each word's character span in that string so a regex match
        # can be mapped back to the exact word boxes it covers.
        joined_pieces: list[str] = []
        spans: dict[int, tuple[int, int]] = {}
        pos = 0
        for i, w in enumerate(ocr["text"]):
            if not w.strip():
                continue
            start = pos
            joined_pieces.append(w)
            pos += len(w)
            spans[i] = (start, pos)
            pos += 1  # the single space that `" ".join` inserts after
        joined = " ".join(joined_pieces)

        # 1) NER entities -> per-word flag set (existing behavior).
        flagged = {e.text for e in self.ner.detect(joined)}
        flag_words = self._flag_word_set(flagged)

        # 2) Deterministic regex backstop: any OCR word overlapping an
        #    email/phone/IBAN/API-key/card match is masked, even when OCR split
        #    the value across word boxes (e.g. "+351 912 884 573").
        regex_idx = self._regex_flagged_indices(joined, spans, ocr["text"])

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
            if self._norm(word) in flag_words or i in regex_idx:
                x, y = ocr["left"][i], ocr["top"][i]
                w, h = ocr["width"][i], ocr["height"][i]
                draw.rectangle([x, y, x + w, y + h], fill="black")

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def _regex_flagged_indices(self, joined: str,
                               spans: dict[int, tuple[int, int]],
                               texts: list[str] | None = None) -> set[int]:
        """Word indices to mask from the deterministic backstop.

        Two sources:
          * a pattern match over the joined OCR text (emails/phones/IBANs/
            API keys/cards), mapped back to every word box it overlaps;
          * an '@' heuristic for emails OCR split around the '@' with spaces
            (e.g. "theo.marchetti @ domain.example"), which the strict email
            regex can't match — flag the '@' word and its immediate neighbors.
        One-way image masking, so over-masking a neighbor is acceptable; a
        leaked address is not.
        """
        idx: set[int] = set()
        for etype in self.ocr_regex:
            pat = _PATTERNS.get(etype)
            if not pat:
                continue
            for m in pat.finditer(joined):
                if not _accept(etype, m.group(0)):
                    continue
                s, e = m.span()
                for i, (ws, we) in spans.items():
                    if ws < e and we > s:  # word overlaps the match
                        idx.add(i)

        if texts is not None and "EMAIL" in self.ocr_regex:
            order = sorted(spans)  # ascending non-empty word indices
            pos = {wi: k for k, wi in enumerate(order)}
            for wi in order:
                if "@" in (texts[wi] or ""):
                    k = pos[wi]
                    idx.add(wi)
                    if k > 0:
                        idx.add(order[k - 1])
                    if k + 1 < len(order):
                        idx.add(order[k + 1])
        return idx

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
