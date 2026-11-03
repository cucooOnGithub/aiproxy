# aiproxy — Manual

Anonymizing reverse proxy for LLM APIs. Point any AI tool (Claude Code,
Claude Desktop, OpenAI SDKs, …) at aiproxy instead of the real provider.
aiproxy scrubs sensitive content locally, forwards the redacted request
upstream, and restores the originals in the reply — so the provider never
sees the real values but you still read them.

---

## 1. What it does and does not do

**Does**
- Detects people names, company/org names, locations, emails, phones (and
  high-confidence secrets like API keys, IBANs) using a *local* LLM + regex.
- Replaces each with a reversible token (`⟦PERSON_1⟧`), stored locally.
- Restores originals in the model's response (text only).
- Masks sensitive text in images and PDFs before sending.

**Does not**
- Guarantee 100% detection. An LLM can miss an entity. Use `fail_closed`
  mode, the `/dryrun` endpoint, and allow/deny lists to reduce risk.
- Restore redacted images/PDFs — pixel/true-redaction is **one-way**.
- Protect the mapping store. That file is the de-anonymization key; anyone
  with it reverses everything. Encrypt it and guard it.

### Threat model
aiproxy protects against the *upstream provider* (and anyone reading
upstream logs) seeing your sensitive data. It does **not** protect against
an attacker who already has your local machine or the mapping store.

---

## 2. Install

Requires Python ≥ 3.11. The installer handles everything else (Ollama,
Tesseract, models, key, config).

### Automatic (recommended)
From the repo root:

**macOS / Linux**
```bash
./install.sh
```
**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer is re-runnable and does:
1. find Python ≥ 3.11, create `.venv`, install `aiproxy[media]`;
2. install Tesseract OCR (Homebrew / apt / dnf / pacman / winget);
3. install Ollama and start it (`:11434`);
4. create `config.yaml` from the example if missing;
5. `ollama pull` every model listed in `config.yaml` (`llm.models` or
   `llm.model`);
6. if `store.encrypt: true`, generate a key and print the
   `AIPROXY_STORE_KEY` to export.

System-package steps are best-effort; if one can't auto-install it prints
the manual command and continues. Then verify with the doctor (§4a).

### Manual
```bash
cd aiproxy
python -m venv .venv && source .venv/bin/activate
pip install -e .            # core (text anonymization)
pip install -e ".[media]"   # add image/PDF/Office redaction
pip install -e ".[dev]"     # add test deps
```
Local LLM (Ollama):
```bash
# install ollama (see ollama.com), then:
ollama pull qwen2.5:3b-instruct
ollama serve     # usually auto-starts; listens on :11434
```
Image/PDF/Office extras also need the Tesseract OCR binary:
```bash
sudo apt install tesseract-ocr     # Debian/Kali/Ubuntu
brew install tesseract             # macOS
winget install UB-Mannheim.TesseractOCR   # Windows
```

---

## 3. Configure

```bash
cp config.example.yaml config.yaml
```

Generate and export the store encryption key (required when
`store.encrypt: true`):
```bash
export AIPROXY_STORE_KEY="$(python -m aiproxy genkey)"
```
Keep this key out of the config file and out of version control. Losing it
makes existing mappings unreadable (= permanent anonymization).

Key config fields (full list in `config.example.yaml`):

| Key | Meaning |
|-----|---------|
| `listen` | host/port aiproxy binds to |
| `upstreams.<provider>.base_url` | real API aiproxy forwards to (override with env `AIPROXY_UPSTREAM_<PROVIDER>`, e.g. `AIPROXY_UPSTREAM_ANTHROPIC`; env wins and can add a provider) |
| `llm.url` / `llm.model` | local Ollama endpoint + model for NER |
| `llm.entity_types` | entity labels the model may return; enum-constrains the schema (default `PERSON, ORG, LOCATION, EMAIL, PHONE`). Add a custom type's action under `anonymizer.entities` too |
| `llm.prompt` | override the NER prompt; must contain `{text}`, optional `{types}` filled from `entity_types` |
| `llm.cache_ttl` | seconds to keep in-memory NER results (`null` = forever) |
| `llm.timeout` | seconds per NER request (default 60; model-load counts toward it) |
| `llm.warm_timeout` | seconds allowed for the startup pre-warm cold load (default 300) |
| `prewarm` | load the model at startup so the first request skips the cold-load cost (default true) |
| `pipeline` | ordered processor list (request top→bottom, restore bottom→top) |
| `mode` | `fail_closed` (block on filter error) or `fail_open` |
| `anonymizer.entities` | per-type action: `anonymize` / `redact` / `ignore` |
| `anonymizer.allowlist` | strings never redacted |
| `anonymizer.denylist` | strings always redacted |
| `store.encrypt` | encrypt originals at rest (needs `AIPROXY_STORE_KEY`) |

---

## 4a. Doctor — verify the setup

Before (or any time after) running, check everything is in place:
```bash
python -m aiproxy doctor --config config.yaml
```
It verifies Python, dependencies, the Tesseract binary, config load, the
store (and `AIPROXY_STORE_KEY` when `store.encrypt: true`), the archive dir
(if set), Ollama reachability, and that every model in the config is pulled.
Required failures exit non-zero; optional gaps (e.g. missing Tesseract) are
`WARN`. Example line: `[FAIL] model pulled: qwen2.5:3b-instruct — run:
ollama pull qwen2.5:3b-instruct`.

## 4. Run

```bash
python -m aiproxy serve --config config.yaml
# listening on http://127.0.0.1:8088   (per listen.port)
```

Health check: `curl http://127.0.0.1:8080/healthz`

### Dashboard
`http://127.0.0.1:8080/dashboard` — live counts (auto-refresh): requests,
text parts redacted, tokens created, entities by type, documents by kind
(image/pdf/office), cache hits, NER blocks, sessions, and durable store
totals. Raw JSON at `/metrics`. Counts are non-sensitive (how many, never
the values). Runtime counters reset on restart; store totals persist.

> By default redacted documents are **not** written to disk — they are
> forwarded upstream and discarded; only token↔original mappings and the
> text redaction cache persist in the `store.path` SQLite file. Set
> `archive.dir` to also keep redacted documents on disk (see below).

### Document archive + disk cache (`archive.dir`)
Set `archive.dir` to a directory and each redacted document (image / PDF /
Office) is written there, named by a hash of its **original**. That store
doubles as a cache: presenting the same original again reads the redacted
version from disk and **skips the local LLM / OCR** entirely
(`document_cache_hits` on the dashboard). Only redacted output is written —
never the original PII-bearing bytes. Image/PDF entries are content-scoped
(reused across sessions); Office entries are session-scoped (their tokens
must resolve against that session's mappings). Omit `archive.dir` to
disable. Delete the directory to clear the cache.

### Point your tool at it

**Claude Code / Anthropic SDK:**
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-ant-...   # your real key; aiproxy passes it through
```

The proxy exposes `/v1/messages` (Anthropic) and `/v1/chat/completions`
(OpenAI). For OpenAI tools set the base URL to `http://127.0.0.1:8080/v1`
and add `upstreams.openai` in config. Images redact only when inlined as a
`data:` URL; remote `http(s)` image URLs pass through unmasked. The OpenAI
Chat Completions API has no PDF part.

Streaming works: `stream:true` requests are forwarded as a stream and
tokens are restored on the fly, even when a `⟦PERSON_1⟧` sentinel is split
across SSE chunks (`aiproxy/proxy/stream.py`). Only text deltas are
restored; tool-call argument fragments (`input_json_delta`) pass through
untouched.

### Preview redactions without sending (`/dryrun`)
```bash
curl -s http://127.0.0.1:8080/dryrun \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"email bob@acme.com"}]}'
```
Returns the body *as it would be forwarded* — inspect it to tune your
config before trusting the proxy with real traffic.

---

## 5. Managing the mapping store

Sessions are keyed per-conversation (by `x-aiproxy-session` header, else a
hash of the API key + first message).

Purge one session's mappings:
```bash
python -m aiproxy purge --config config.yaml --session <session-id>
```

Back up `map.db` **and** `AIPROXY_STORE_KEY` together, or treat both as
ephemeral — one without the other is useless.

---

## 6. Writing a custom processor

A processor is the single plug point. Implement two methods:

```python
from aiproxy.document import Document, PartKind
from aiproxy.pipeline.base import Processor, SessionContext

class MyFilter(Processor):
    name = "my_filter"
    handles = frozenset({PartKind.TEXT})

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        for part in doc.text_parts():
            part.text = part.text.replace("secret", "[gone]")
        return doc

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        return doc
```

Register it in `aiproxy/pipeline/registry.py`:
```python
from aiproxy.processors.my_filter import MyFilter
REGISTRY[MyFilter.name] = MyFilter
```

Add its name to `pipeline:` in config. For reversible redaction, use
`ctx.store.put/token_for/original_for` and the token helpers in
`aiproxy/processors/tokens.py`. Scope every stored mapping by
`ctx.session_id`.

---

## 7. Image & PDF behavior

- Images: OCR → NER → opaque black boxes over flagged words → re-encoded
  PNG sent upstream.
- Text-layer PDFs: true redaction (PyMuPDF removes the underlying text) AND
  every raster image embedded in the page is inspected and masked.
- Scanned PDFs: rasterized, then treated as images.
- Office docs (OOXML `.docx`/`.pptx`/`.xlsx`, incl. SharePoint exports):
  body **text** in `<w:t>`/`<a:t>`/`<t>` nodes is tokenized reversibly
  (by the anonymizer, same tokens as plain text — so the model's reply
  restores), and every image under `*/media/` is inspected and masked
  (by image_redactor). All other bytes (namespaces, formatting,
  relationships) are preserved. Limit: an entity split across multiple
  runs (separate `<w:t>` nodes) is detected but only masked if it sits in
  one node — reflow the source or convert to PDF for such cases.
- `image_redactor.max_pages` caps work per PDF.
- One-way: nothing about a masked region is restorable.

### Caching
- **NER result cache** (in-memory, per model+text): a reloaded model never
  re-runs NER on text it already saw. Bounded by `llm.cache_ttl` seconds.
- **Per-document redaction cache** (persistent, in the store, per session):
  the exact redacted output of a text is stored by hash, so a re-presented
  document (e.g. the same conversation history resent each turn) skips NER
  and replacement entirely. Cleared by `purge`.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `NER failed, blocking send` | Ollama not running / wrong `llm.url`. Start `ollama serve`. |
| Names still reaching upstream | LLM missed them → add to `denylist`; run `/dryrun` to verify. |
| `AIPROXY_STORE_KEY not set` | export the key, or set `store.encrypt: false` (dev only). |
| `tesseract not found` | install the Tesseract binary (§2). |
| Slow responses | NER + OCR are local-compute heavy; results cache by text hash. |

---

## 9. Tests

```bash
pytest
```
`tests/test_roundtrip.py` proves tokenize→store→restore with the
deterministic secrets filter (no LLM needed).
