"""LLM-backed anonymizer — the headline filter.

Runs local-LLM NER over each text part, then for each detected entity
whose type is configured to `anonymize`, replaces every occurrence with a
reversible token and records token<->original in the store. On the
response path, tokens are swapped back to originals so the caller sees
real values while upstream only ever saw tokens.

Config:
    llm:       {url, model}
    entities:  {PERSON: anonymize|redact|ignore, ORG: ..., ...}
    allowlist: [strings never redacted]
    denylist:  [strings always redacted as type OTHER]
    mode:      fail_closed (default) | fail_open
"""

from __future__ import annotations

from aiproxy.document import Document, PartKind
from aiproxy.llm.ollama_ner import Entity, build_ner
from aiproxy.metrics import METRICS
from aiproxy.pipeline.base import Processor, SessionContext
from aiproxy.processors.tokens import TOKEN_RE, make_token


class AnonymizerError(RuntimeError):
    pass


class Anonymizer(Processor):
    name = "anonymizer"
    handles = frozenset({PartKind.TEXT, PartKind.OFFICE})

    def __init__(self, config: dict):
        super().__init__(config)
        llm = config.get("llm", {})
        self.ner = build_ner(llm)
        self.actions: dict[str, str] = config.get("entities", {})
        self.allow = set(config.get("allowlist", []))
        self.deny = set(config.get("denylist", []))
        self.mode = config.get("mode", "fail_closed")

    def _seq(self, ctx: SessionContext) -> int:
        n = getattr(ctx, "_seq_counter", 0) + 1
        ctx._seq_counter = n  # type: ignore[attr-defined]
        return n

    def _action(self, etype: str) -> str:
        return self.actions.get(etype, "ignore")

    def _detect(self, text: str) -> list[Entity]:
        """NER + denylist entities. Raises on NER failure (caller decides)."""
        ents = list(self.ner.detect(text))
        ents += [Entity(d, "OTHER") for d in self.deny if d in text]
        return ents

    def _tokenize(self, text: str, ents: list[Entity],
                  ctx: SessionContext) -> str:
        """Replace each actionable entity with a reversible token (or
        [REDACTED]), recording token<->original in the store. Shared by the
        plain-text and Office paths so tokens stay consistent."""
        # Longest-first so overlapping names replace cleanly.
        for ent in sorted(ents, key=lambda e: len(e.text), reverse=True):
            if ent.text in self.allow or not ent.text.strip():
                continue
            # Never re-tokenize a token an earlier processor already made
            # (e.g. the NER model flagging ⟦EMAIL_1⟧ as an email).
            if TOKEN_RE.search(ent.text):
                continue
            action = "redact" if ent.text in self.deny else self._action(ent.type)
            if action == "ignore":
                continue
            token = ctx.store.token_for(ctx.session_id, ent.text, ent.type)
            if token is None:
                token = make_token(ent.type, self._seq(ctx))
                ctx.store.put(ctx.session_id, token, ent.text, ent.type)
                METRICS.inc("tokens_created")
            METRICS.inc(f"entity:{ent.type}")
            replacement = "[REDACTED]" if action == "redact" else token
            text = text.replace(ent.text, replacement)
        return text

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            text = part.text or ""

            # Document-level cache: if this exact input text was redacted
            # before in this session, reuse the stored output and skip NER
            # entirely. The token<->original mappings from that first pass
            # are still in the store, so the response path restores normally.
            h = ctx.store.text_hash(text)
            cached = ctx.store.cache_get(ctx.session_id, self.name, h)
            if cached is not None:
                part.text = cached
                METRICS.inc("cache_hits")
                continue

            try:
                ents = self._detect(text)
            except Exception as e:  # noqa: BLE001
                if self.mode == "fail_closed":
                    METRICS.inc("ner_blocks")
                    raise AnonymizerError(f"NER failed, blocking send: {e}") from e
                continue  # fail_open: send as-is

            text = self._tokenize(text, ents, ctx)
            part.text = text
            ctx.store.cache_put(ctx.session_id, self.name, h, text)
            METRICS.inc("text_parts_redacted")

        # Office documents: tokenize the body text inside the OOXML XML parts
        # (reversibly, same tokens as above). Images in them are handled by
        # image_redactor; here we only touch text.
        for part in doc.parts:
            if part.kind is PartKind.OFFICE and part.data:
                try:
                    part.data = self._redact_office(part.data, ctx)
                    METRICS.inc("doc:office")
                except AnonymizerError:
                    raise
                except Exception as e:  # noqa: BLE001
                    if self.mode == "fail_closed":
                        raise AnonymizerError(
                            f"office redaction failed, blocking send: {e}") from e
        return doc

    def _redact_office(self, data: bytes, ctx: SessionContext) -> bytes:
        """Tokenize body text inside an OOXML doc (docx/pptx/xlsx), reversibly.

        Text lives in `<w:t>` / `<a:t>` / `<t>` elements. We edit those text
        nodes in place with a regex on the raw XML, leaving every other byte
        (namespaces, formatting, relationships) untouched - far safer than
        re-serializing with a generic XML writer. NER runs ONCE over all node
        text concatenated (better context), then each node is tokenized via
        the shared `_tokenize`, so tokens match the plain-text path and the
        response restores them.

        Limitation: an entity split across multiple runs (separate `<w:t>`
        nodes) is detected but only masked if it sits inside one node."""
        import io as _io
        import re as _re
        import zipfile

        # Disk cache: same original Office doc in the same session -> reuse the
        # already text-redacted bytes and skip the local LLM entirely. Scoped
        # by session so the cached tokens resolve against this session's
        # mappings (which the first pass stored).
        arch = getattr(ctx, "archive", None)
        akey = arch.key(f"office-text:{ctx.session_id}", data) if arch else None
        if arch:
            cached = arch.get(akey)
            if cached is not None:
                METRICS.inc("document_cache_hits")
                return cached

        # <w:t ...>text</w:t>, <a:t>text</a:t>, <t ...>text</t> (not <w:tbl> etc)
        pat = _re.compile(r"<(w:t|a:t|t)(?=[ >/])([^>]*)>(.*?)</\1>", _re.DOTALL)

        def is_text_part(name: str) -> bool:
            n = name.lower()
            return n.endswith(".xml") and (
                n == "word/document.xml"
                or n.startswith("word/header") or n.startswith("word/footer")
                or n.startswith("ppt/slides/slide")
                or n.startswith("ppt/notesslides/")
                or n == "xl/sharedstrings.xml")

        src = zipfile.ZipFile(_io.BytesIO(data), "r")
        texts: dict[str, str] = {}
        inners: list[str] = []
        for info in src.infolist():
            if not is_text_part(info.filename):
                continue
            s = src.read(info.filename).decode("utf-8")
            texts[info.filename] = s
            for m in pat.finditer(s):
                if m.group(3).strip():
                    inners.append(m.group(3))

        ents: list[Entity] = self._detect("\n".join(inners)) if inners else []

        def repl(m: "_re.Match") -> str:
            inner = m.group(3)
            if not inner.strip():
                return m.group(0)
            return f"<{m.group(1)}{m.group(2)}>{self._tokenize(inner, ents, ctx)}" \
                   f"</{m.group(1)}>"

        out_buf = _io.BytesIO()
        out = zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED)
        for info in src.infolist():
            if info.filename in texts:
                out.writestr(info, pat.sub(repl, texts[info.filename]).encode("utf-8"))
            else:
                out.writestr(info, src.read(info.filename))
        out.close()
        src.close()
        result = out_buf.getvalue()
        if arch:
            arch.put(akey, result)
        return result

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            text = part.text or ""

            def repl(m) -> str:
                orig = ctx.store.original_for(ctx.session_id, m.group(0))
                return orig if orig is not None else m.group(0)

            part.text = TOKEN_RE.sub(repl, text)
        return doc
