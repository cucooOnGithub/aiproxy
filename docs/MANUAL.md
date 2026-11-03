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

Requires Python ≥ 3.11 and a running local LLM (Ollama recommended).

```bash
cd aiproxy
python -m venv .venv && source .venv/bin/activate
pip install -e .            # core (text anonymization)
pip install -e ".[media]"   # add image/PDF redaction
pip install -e ".[dev]"     # add test deps
```

**Local LLM (Ollama):**
```bash
# install ollama (see ollama.com), then:
ollama pull qwen2.5
ollama serve     # usually auto-starts; listens on :11434
```

**Image/PDF extras** also need the Tesseract OCR binary:
```bash
# Debian/Kali/Ubuntu
sudo apt install tesseract-ocr
# macOS
brew install tesseract
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
| `pipeline` | ordered processor list (request top→bottom, restore bottom→top) |
| `mode` | `fail_closed` (block on filter error) or `fail_open` |
| `anonymizer.entities` | per-type action: `anonymize` / `redact` / `ignore` |
| `anonymizer.allowlist` | strings never redacted |
| `anonymizer.denylist` | strings always redacted |
| `store.encrypt` | encrypt originals at rest (needs `AIPROXY_STORE_KEY`) |

---

## 4. Run

```bash
python -m aiproxy serve --config config.yaml
# listening on http://127.0.0.1:8080
```

Health check: `curl http://127.0.0.1:8080/healthz`

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
- Text-layer PDFs: true redaction (PyMuPDF removes the underlying text).
- Scanned PDFs: rasterized, then treated as images.
- `image_redactor.max_pages` caps work per PDF.
- One-way: nothing about a masked region is restorable.

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
