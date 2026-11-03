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
from aiproxy.llm.ollama_ner import Entity, OllamaNER
from aiproxy.pipeline.base import Processor, SessionContext
from aiproxy.processors.tokens import TOKEN_RE, make_token


class AnonymizerError(RuntimeError):
    pass


class Anonymizer(Processor):
    name = "anonymizer"
    handles = frozenset({PartKind.TEXT})

    def __init__(self, config: dict):
        super().__init__(config)
        llm = config.get("llm", {})
        self.ner = OllamaNER(
            url=llm.get("url", "http://localhost:11434"),
            model=llm.get("model", "qwen2.5"),
            timeout=llm.get("timeout", 60.0),
            warm_timeout=llm.get("warm_timeout", 300.0),
        )
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

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            text = part.text or ""
            try:
                ents: list[Entity] = self.ner.detect(text)
            except Exception as e:  # noqa: BLE001
                if self.mode == "fail_closed":
                    raise AnonymizerError(f"NER failed, blocking send: {e}") from e
                continue  # fail_open: send as-is

            # Denylist entries become forced entities.
            ents += [Entity(d, "OTHER") for d in self.deny if d in text]

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
                replacement = "[REDACTED]" if action == "redact" else token
                text = text.replace(ent.text, replacement)
            part.text = text
        return doc

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            text = part.text or ""

            def repl(m) -> str:
                orig = ctx.store.original_for(ctx.session_id, m.group(0))
                return orig if orig is not None else m.group(0)

            part.text = TOKEN_RE.sub(repl, text)
        return doc
