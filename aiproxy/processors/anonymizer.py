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

import re as _re_mod

# Deterministic email catch for the Office path: embedded document text never
# passes through secrets_regex (that runs on plain-text parts only), so the
# small NER model is otherwise the only thing catching emails in a .docx and
# it misses some. This guarantees every well-formed email is tokenized.
_EMAIL_RE = _re_mod.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


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
        # always_tokenize: strings always masked but REVERSIBLY (a ⟦TERM_n⟧
        # token, restored on the response), unlike denylist which replaces
        # with irreversible [REDACTED]. Use for known-sensitive values you
        # still want Claude to reproduce in its output (e.g. a client name
        # that must appear in generated documents but never reach upstream).
        self.always_tok = set(config.get("always_tokenize", []))
        self.mode = config.get("mode", "fail_closed")
        # Optional name gazetteer: a file of known name PARTS (one per line,
        # given names and surnames mixed). Adjacent capitalized tokens whose
        # lowercase form is in the set are joined into one PERSON entity, so
        # "Maria" + "Silva" -> "Maria Silva". Deterministic recall boost for
        # names the LLM misses. min_parts sets the shortest run that counts.
        self.gazetteer = self._load_gazetteer(config.get("name_gazetteer"))
        self.gaz_min_parts = max(1, int(config.get("name_gazetteer_min_parts", 1)))
        # Known-value sweep: re-mask any value already tokenized earlier in the
        # session, even in blocks where the NER misses it this turn. Closes the
        # 'caught once, leaked later' recall gap (the biggest leak source). A
        # min length avoids pathological substring matches on short strings.
        self.sweep_known = bool(config.get("known_value_sweep", True))
        self.sweep_min_len = max(1, int(config.get("known_value_sweep_min_len", 4)))
        # Per-user term tier (Phase 3): org terms above are mandatory for all
        # users; a user may ADD their own always_tokenize/denylist entries via
        # the store. Merged at request time, cached per user with a short TTL
        # so a self-service add takes effect within `user_terms_ttl` seconds
        # without a restart. Cache is process-local and thread-safe.
        self.user_terms_ttl = float(config.get("user_terms_ttl", 30))
        self._ut_cache: dict[str, tuple[float, set, set]] = {}
        import threading as _th
        self._ut_lock = _th.Lock()

    @staticmethod
    def _load_gazetteer(path) -> set[str]:
        if not path:
            return set()
        try:
            with open(path, encoding="utf-8") as f:
                return {ln.strip().lower() for ln in f
                        if ln.strip() and not ln.lstrip().startswith("#")}
        except OSError:
            return set()

    # Separators that may join parts of one name: dot, underscore, hyphen and a
    # single space (covers "Maria Silva", "john.rivera", "john_rivera",
    # "J. Rivera"). Kept short so we never join across unrelated text.
    _NAME_SEP = set(". _-")

    def _gazetteer_entities(self, text: str) -> list[Entity]:
        """Find names composed of known gazetteer parts, joined by spaces OR by
        '.'/'_'/'-' (so dotted/underscored identifiers like 'john.rivera',
        including inside a path, are caught), and handle an initial before a
        known surname ('J. Rivera'). Matching is case-insensitive and NOT
        gated on capitalization - that is how the lowercase dotted username was
        leaking - so precision rests on the curated names.txt. The emitted
        entity text is the exact substring so the tokenizer can replace it."""
        if not self.gazetteer:
            return []
        import re as _re
        toks = []
        for m in _re.finditer(r"[A-Za-z]+", text):
            w = m.group(0)
            toks.append({"s": m.start(), "e": m.end(),
                         "known": w.lower() in self.gazetteer,
                         "initial": len(w) == 1 and w.isupper()})
        out: list[Entity] = []
        i, n = 0, len(toks)
        while i < n:
            if not (toks[i]["known"] or toks[i]["initial"]):
                i += 1
                continue
            j = i
            while j + 1 < n:
                gap = text[toks[j]["e"]:toks[j + 1]["s"]]
                nxt = toks[j + 1]
                if (gap and len(gap) <= 2 and all(c in self._NAME_SEP for c in gap)
                        and (nxt["known"] or nxt["initial"])):
                    j += 1
                else:
                    break
            run = toks[i:j + 1]
            # Need enough parts AND at least one real (non-initial) known part,
            # so a lone/bare initial never counts as a name.
            if len(run) >= self.gaz_min_parts and any(r["known"] for r in run):
                out.append(Entity(text[run[0]["s"]:run[-1]["e"]], "PERSON"))
            i = j + 1
        return out

    def _effective_terms(self, ctx: SessionContext) -> tuple[set, set]:
        """(deny, always_tokenize) = org tier ∪ this user's self-added tier.

        The user tier can only ADD (the store write layer enforces that it
        never removes/allowlists an org entry), so a plain union is safe and
        can never weaken org coverage. Cached per user_id with a TTL.
        """
        user_id = getattr(ctx, "user_id", "anon")
        store = getattr(ctx, "store", None)
        if store is None or not hasattr(store, "user_terms"):
            return self.deny, self.always_tok
        import time as _t
        now = _t.monotonic()
        with self._ut_lock:
            hit = self._ut_cache.get(user_id)
            if hit and (now - hit[0]) < self.user_terms_ttl:
                return hit[1], hit[2]
        try:
            ut = store.user_terms(user_id)
        except Exception:  # noqa: BLE001 - store hiccup must not drop org terms
            return self.deny, self.always_tok
        deny = self.deny | set(ut.get("denylist", []))
        always = self.always_tok | set(ut.get("always_tokenize", []))
        with self._ut_lock:
            self._ut_cache[user_id] = (now, deny, always)
        return deny, always

    def _seq(self, ctx: SessionContext) -> int:
        # Per-session, persisted in the store: a SessionContext is recreated
        # per request, so a ctx-local counter restarts at 1 each request and
        # reissues the same token number to different originals (collision).
        return ctx.store.next_seq(ctx.session_id)

    def _action(self, etype: str) -> str:
        return self.actions.get(etype, "ignore")

    def _detect(self, text: str, deny: set | None = None,
                always: set | None = None) -> list[Entity]:
        """NER + denylist entities. Raises on NER failure (caller decides).

        `deny`/`always` default to the org tier; callers pass the effective
        (org ∪ user) sets so per-user terms are detected too."""
        deny = self.deny if deny is None else deny
        always = self.always_tok if always is None else always
        ents = list(self.ner.detect(text))
        ents += [Entity(d, "OTHER") for d in deny if d in text]
        ents += [Entity(s, "TERM") for s in always if s in text]
        ents += self._gazetteer_entities(text)
        return ents

    def _tokenize(self, text: str, ents: list[Entity],
                  ctx: SessionContext, deny: set | None = None,
                  always: set | None = None) -> str:
        """Replace each actionable entity with a reversible token (or
        [REDACTED]), recording token<->original in the store. Shared by the
        plain-text and Office paths so tokens stay consistent."""
        deny = self.deny if deny is None else deny
        always = self.always_tok if always is None else always
        # Longest-first so overlapping names replace cleanly.
        for ent in sorted(ents, key=lambda e: len(e.text), reverse=True):
            if ent.text in self.allow or not ent.text.strip():
                continue
            # Never re-tokenize a token an earlier processor already made
            # (e.g. the NER model flagging ⟦EMAIL_1⟧ as an email).
            if TOKEN_RE.search(ent.text):
                continue
            # The NER model sometimes returns an entity string that is not
            # literally in the text (a normalized/garbled echo of an existing
            # token, e.g. "@@PHONE_1@@"). Replacing it would be a no-op, so
            # skip it entirely - otherwise we mint a token and store a mapping
            # that never appears in the output, polluting the store and counts.
            if ent.text not in text:
                continue
            if ent.text in deny:
                action = "redact"          # irreversible [REDACTED]
            elif ent.text in always:
                action = "anonymize"       # reversible token, forced on
            else:
                action = self._action(ent.type)
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
        return self._sweep_known(text, ctx)

    def _sweep_known(self, text: str, ctx: SessionContext) -> str:
        """Re-mask any value already tokenized in this session, so a value the
        NER caught once never leaks in a later block where the model misses it
        (the biggest recall gap). Reuses each value's existing token. Over-
        masking here is reversed on the response path; a miss is a permanent
        upstream leak, so we err toward masking."""
        if not self.sweep_known:
            return text
        known = [(o, tok) for o, tok, _et
                 in ctx.store.all_mappings(ctx.session_id)
                 if len(o) >= self.sweep_min_len and not TOKEN_RE.search(o)]
        # Longest-first so a value contained in another masks cleanly.
        for original, token in sorted(known, key=lambda m: len(m[0]),
                                      reverse=True):
            if original in text:
                text = text.replace(original, token)
        return text

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        deny, always = self._effective_terms(ctx)
        for part in doc.text_parts():
            text = part.text or ""

            # Document-level cache: if this exact input text was redacted
            # before in this session, reuse the stored output and skip NER
            # entirely. The token<->original mappings from that first pass
            # are still in the store, so the response path restores normally.
            h = ctx.store.text_hash(text)
            cached = ctx.store.cache_get(ctx.session_id, self.name, h)
            if cached is not None:
                # Still sweep: the cached output was masked for what was known
                # when it was first processed; values tokenized since (in other
                # blocks) must be re-masked here too, or they leak on resend.
                part.text = self._sweep_known(cached, ctx)
                METRICS.inc("cache_hits")
                continue

            try:
                ents = self._detect(text, deny, always)
            except Exception as e:  # noqa: BLE001
                if self.mode == "fail_closed":
                    METRICS.inc("ner_blocks")
                    raise AnonymizerError(f"NER failed, blocking send: {e}") from e
                continue  # fail_open: send as-is

            text = self._tokenize(text, ents, ctx, deny, always)
            part.text = text
            ctx.store.cache_put(ctx.session_id, self.name, h, text)
            METRICS.inc("text_parts_redacted")

        # Office documents: tokenize the body text inside the OOXML XML parts
        # (reversibly, same tokens as above). Images in them are handled by
        # image_redactor; here we only touch text.
        for part in doc.parts:
            if part.kind is PartKind.OFFICE and part.data:
                try:
                    part.data = self._redact_office(part.data, ctx, part,
                                                    deny, always)
                    METRICS.inc("doc:office")
                except AnonymizerError:
                    raise
                except Exception as e:  # noqa: BLE001
                    if self.mode == "fail_closed":
                        raise AnonymizerError(
                            f"office redaction failed, blocking send: {e}") from e
        return doc

    def _redact_office(self, data: bytes, ctx: SessionContext,
                       part=None, deny: set | None = None,
                       always: set | None = None) -> bytes:
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

        joined = "\n".join(inners)
        ents: list[Entity] = self._detect(joined, deny, always) if inners else []
        # Deterministic email catch (regex) in addition to NER, so emails in
        # the document body are always tokenized even when the model misses
        # them. Deduped against emails NER already found.
        seen = {e.text for e in ents}
        ents += [Entity(m.group(0), "EMAIL")
                 for m in _EMAIL_RE.finditer(joined)
                 if m.group(0) not in seen]

        def repl(m: "_re.Match") -> str:
            inner = m.group(3)
            if not inner.strip():
                return m.group(0)
            return f"<{m.group(1)}{m.group(2)}>" \
                   f"{self._tokenize(inner, ents, ctx, deny, always)}" \
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
            origin = getattr(part, "origin", {}) or {}
            arch.put(akey, result, origin.get("filename"),
                     origin.get("ext", ""))
        return result

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            text = part.text or ""

            def repl(m) -> str:
                orig = ctx.store.original_for(ctx.session_id, m.group(0))
                return orig if orig is not None else m.group(0)

            part.text = TOKEN_RE.sub(repl, text)
        return doc
