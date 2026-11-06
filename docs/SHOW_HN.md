# Show HN draft — aiproxy

## Title
```
Show HN: aiproxy – Redact PII locally before it reaches Claude/OpenAI
```

## Body (post as the first comment right after submitting)

I built aiproxy because I wanted to use Claude Code and the OpenAI API on real
work without shipping customer names, client/NDA data, and secrets to a hosted
provider.

It's a reverse proxy that mimics the Anthropic (`/v1/messages`) and OpenAI
(`/v1/chat/completions`) endpoints. You point your AI tool at it; it scrubs
people/company names and other sensitive content using a **local** LLM (Ollama
NER) plus deterministic regex/gazetteer backstops, forwards only tokens
upstream, and restores the originals in the reply. The upstream provider never
sees the real values. Mappings live in a local encrypted SQLite (or Postgres)
store, so redaction is reversible.

A few design decisions that mattered:

- **Deterministic backstops, not just the LLM.** A 3B NER model misses
  less-salient names, so there's a regex layer, a name gazetteer (joins
  `john.rivera`, `J. Rivera` even inside a path), and a known-value sweep that
  re-masks any value tokenized once anywhere later in the session — so an entity
  caught inconsistently doesn't leak in a later turn.
- **Scope is narrow on purpose.** Only genuine user messages and attached
  documents get redacted; Claude Code's own system prompt, CLAUDE.md, skills,
  and command output pass through verbatim, or the tool breaks.
- **Handles docs**, not just chat: images (OCR→mask), PDFs (true text redaction
  + scanned pages), and OOXML `.docx/.pptx/.xlsx`.
- **No key held by the proxy** — it forwards your `x-api-key`/bearer, so a
  Claude subscription works unchanged.

Honest limits: detection is **not guaranteed** — a local model can miss an
entity, so a real value can still reach the provider. Run it in `fail_closed`,
preview with `/dryrun`, and treat the mapping store as a secret. It's working
with 97 passing tests but not yet broadly smoke-tested against live providers
beyond a local echo round-trip.

MIT-style it is not — PolyForm Noncommercial (commercial license on request).

Repo: https://github.com/cucooOnGithub/aiproxy

Would love feedback on the recall/backstop approach and the threat model.

## Posting notes
- **Timing:** weekday, ~7–10am US Eastern lands best on HN.
- **One ask per post** — no Buy-Me-a-Coffee in the Show HN body; HN reacts
  badly to monetization in a launch post. Keep it in the README only.
- **A GIF** of Claude Code talking through aiproxy with a redaction visible in
  the logs would roughly double click-through. Worth recording before posting.
- Same body (trimmed) works for r/LocalLLaMA and r/selfhosted; r/netsec wants
  the threat-model angle up front.
