# aiproxy

Anonymizing reverse proxy for LLM APIs. Point Claude Code (or any AI tool)
at aiproxy; it scrubs people/company names and other sensitive content with
a **local** LLM before forwarding to Anthropic, then restores the originals
in the reply. Pluggable filter/processor interface. Handles text, images,
PDFs.

```
AI tool ──▶ aiproxy ──(redacted)──▶ Anthropic
   ▲            │                       │
   └─(restored)─┴───────(tokens)────────┘
```

- Reversible anonymization: originals ↔ tokens kept in a local encrypted store.
- One `Processor` interface — drop in your own filters.
- Ships: `secrets_regex`, `anonymizer` (local-LLM NER), `image_redactor`, `logger`.
- `/dryrun` endpoint to preview redactions before trusting it.

Full install + usage: [`docs/MANUAL.md`](docs/MANUAL.md).

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[media]"
ollama pull qwen2.5
cp config.example.yaml config.yaml
export AIPROXY_STORE_KEY="$(python -m aiproxy genkey)"
python -m aiproxy serve --config config.yaml
# then: export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
```

## Status
Working, 36 passing tests. Not yet smoke-tested against live providers
except the local echo round-trip below.

- **Providers**: Anthropic (`/v1/messages`) and OpenAI
  (`/v1/chat/completions`), streaming and non-streaming (SSE tokens restored
  even when split across chunks). Transparent auth passthrough — the proxy
  holds no key; it forwards the caller's `x-api-key` / `Authorization`
  bearer, so a Claude subscription works unchanged.
- **Anonymizer**: local-LLM NER via Ollama, reversible tokens, encrypted
  SQLite store. Configurable model, prompt, and entity types. Ensemble
  (`MultiNER`, union of several models) or a single model; `keep_alive`,
  cache TTL, and startup pre-warm for VRAM-tight boxes.
- **Documents**: images (OCR→mask), PDFs (true text redaction + embedded
  images + scanned-page rasterize), and OOXML Office `.docx`/`.pptx`/`.xlsx`
  (body text tokenized reversibly + `*/media/` images masked).
- **Caching**: in-memory NER cache (TTL) + persistent per-document
  redaction cache (re-presented docs skip re-work).
- **Verified live**: local echo upstream — real name/email/company went
  upstream as tokens and came back restored to the client.
- **Not done**: Office text split across runs only masked within one node;
  Office over the Anthropic API needs a client that sends the raw OOXML
  bytes.

## Layout
```
aiproxy/proxy/        server, provider adapters (Anthropic/OpenAI), SSE streaming
aiproxy/pipeline/     Processor interface + runner + registry
aiproxy/processors/   anonymizer, secrets_regex, image_redactor, logger
aiproxy/store/        sqlite store (mappings + redaction cache) + at-rest encryption
aiproxy/llm/          Ollama NER client, MultiNER ensemble, build_ner
aiproxy/config/       YAML loader (+ AIPROXY_UPSTREAM_* env overrides)
docs/MANUAL.md        install + usage + extending
```
