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
Anthropic (`/v1/messages`) and OpenAI (`/v1/chat/completions`) paths both
work end-to-end, streaming and non-streaming (SSE tokens restored even when
split across chunks). Not yet smoke-tested against the live providers.

## Layout
```
aiproxy/proxy/        server, provider adapters, streaming (stub)
aiproxy/pipeline/     Processor interface + runner + registry
aiproxy/processors/   anonymizer, secrets_regex, image_redactor, logger
aiproxy/store/        sqlite store + at-rest encryption
aiproxy/llm/          Ollama NER client
aiproxy/config/       YAML loader
docs/MANUAL.md        install + usage + extending
```
