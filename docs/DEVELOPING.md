# aiproxy — Developer Guide

For contributors working on aiproxy itself: how the system is built, where to
make a change, and how to get it merged. If you only want to *run* aiproxy, see
the [Manual](MANUAL.md); if you want to ship a custom redaction filter without
touching the core, see [Writing a Processor](WRITING_A_PROCESSOR.md).

> **License note.** aiproxy is under the PolyForm Noncommercial License 1.0.0.
> Contributions are accepted under the same terms. By opening a PR you agree
> your contribution is licensed that way.

---

## 1. What the project is, technically

aiproxy is an **anonymizing reverse proxy** that mimics the Anthropic
(`/v1/messages`) and OpenAI (`/v1/chat/completions`) HTTP APIs. A request flows:

```
caller → adapter.to_document → pipeline.run_request (redact + store tokens)
       → adapter.from_document → upstream provider
upstream → adapter.response_to_document → pipeline.run_response (restore)
        → adapter.response_from_document → caller
```

The invariant that drives every design decision: **recall beats precision,
because reversing is cheap and a leak is forever.** The proxy would rather
over-mask (and undo it on the response) than let one real value reach upstream.
See the data-flow and org-topology diagrams in
[MANUAL §1b](MANUAL.md#1b-architecture).

**Stack:** Python ≥ 3.11, FastAPI + uvicorn, SQLite or Postgres for the store,
a local LLM (Ollama) for NER, Tesseract for OCR, PyInstaller for the binary.
Async HTTP via httpx. No cloud dependency — detection runs locally.

### The three core abstractions

**`Document` / `Part`** (`aiproxy/document.py`) — the provider-agnostic
internal representation. An adapter parses a provider body into a `Document`
of `Part`s (`TEXT`, `IMAGE`, `PDF`, `OFFICE`), each carrying an `origin` dict
so the adapter can serialize it back into the exact slot it came from.
Processors only ever see `Document`s, so they never care whether the caller
spoke Anthropic or OpenAI.

**`Processor`** (`aiproxy/pipeline/base.py`) — the single plug point. Two
methods, `process_request` (redact) and `process_response` (restore). The
request path runs processors in config order; the response path runs them in
**reverse**, so each restore undoes exactly the redaction applied last. A
`handles` frozenset of `PartKind`s lets the runner skip a processor for a
document it doesn't touch.

**`Store`** (`aiproxy/store/base.py`) — the reversible token↔original map, the
per-session sequence counter, the per-text redaction cache, and the per-user
term tier. It *is* the de-anonymization key; originals are encrypted at rest.
Two backends implement the same `Store` Protocol: `SqliteStore` (default,
local) and `PostgresStore` (shared, multi-replica). Per-user isolation is done
by namespacing the session id as `<user_id>/<raw_session>` before it reaches
the store.

### Code map

| Path | Responsibility |
|---|---|
| `aiproxy/proxy/server.py` | FastAPI app, request handling, endpoints, hot-reload `_State`, `/terms` API |
| `aiproxy/proxy/adapters.py` | Anthropic/OpenAI body ↔ `Document` |
| `aiproxy/proxy/stream.py` | SSE streaming + token restoration across chunk boundaries |
| `aiproxy/pipeline/base.py` | `Processor`, `Pipeline`, `SessionContext`, operational-content masking (`scope`) |
| `aiproxy/pipeline/registry.py` | name → processor class; composes built-ins + plugins |
| `aiproxy/pipeline/plugins.py` | drop-in plugin discovery + security gates |
| `aiproxy/processors/` | `logger`, `secrets_regex`, `anonymizer`, `image_redactor` |
| `aiproxy/llm/ollama_ner.py` | NER over Ollama, single model or ensemble |
| `aiproxy/store/` | `base` (Protocol + factory), `sqlite_store`, `postgres_store`, `crypto` |
| `aiproxy/config/` | `loader` (env overlay + defaults), `schema` (validation) |
| `aiproxy/identity.py` | resolve `user_id`, namespace the session |
| `aiproxy/archive.py` | optional on-disk redacted-document archive/cache + redaction log |
| `aiproxy/__main__.py` | CLI: `serve`, `purge`, `genkey`, `doctor`, `config`, `--version` |
| `tests/` | pytest suite (currently 97 tests) |

### Scope: what gets redacted
Only genuine user messages and attached documents are redacted. Claude Code's
own operational content — the system prompt, `<system-reminder>`/command
blocks, and untagged operational blocks matched by `scope.skip_prefixes` /
`skip_regexes` — is masked with inert placeholders *before* the redactors run
and restored verbatim afterward, so redaction never corrupts how the client
operates. This logic lives in `Pipeline._mask` / `_unmask`.

---

## 2. Dev setup

```bash
git clone <repo> && cd aiproxy
python -m venv .venv && source .venv/bin/activate
pip install -e ".[media,dev]"      # core + image/PDF/Office + test deps
ollama pull qwen2.5:3b-instruct    # the NER model (see config)
cp config.example.yaml config.yaml
export AIPROXY_STORE_KEY="$(python -m aiproxy genkey)"
python -m aiproxy doctor --config config.yaml   # verify the toolchain
```

`doctor` checks deps, the Tesseract binary, Ollama reachability and the
configured model — run it first when something won't start.

### Running
```bash
python -m aiproxy serve --config config.yaml
```
Many tests need **no** LLM: the deterministic `secrets_regex` filter proves the
tokenize→store→restore round-trip without Ollama.

---

## 3. Tests

```bash
pytest -q                          # whole suite
pytest tests/test_roundtrip.py -q  # one file
pytest -k user_terms -q            # by keyword
```

Conventions:
- Every behavioral change ships with a test. Bug fixes get a regression test
  that fails before the fix.
- Prefer the deterministic path (`secrets_regex`, direct `Store` calls, a fake
  NER returning fixed entities) over requiring a live model — tests must pass
  in CI with no GPU and no Ollama. See `tests/test_user_terms.py` for the
  `_NoNER` pattern.
- HTTP-level tests use FastAPI's `TestClient` against `create_app(cfg)` with a
  temp SQLite path and `prewarm: false` (see `tests/test_org_endpoints.py`).
- The suite must stay green **and** not require network access.

---

## 4. Making common changes

**Add a redaction filter.** Implement a `Processor`, register it in
`registry.py` (`BUILTINS[name] = cls`), add it to `pipeline:` in config. Scope
every stored mapping by `ctx.session_id`; use `ctx.store` + the helpers in
`processors/tokens.py` for reversible tokens. Full walkthrough and the
drop-in/plugin alternative: [WRITING_A_PROCESSOR.md](WRITING_A_PROCESSOR.md).

**Add a store backend.** Implement every method on the `Store` Protocol in
`store/base.py` with identical semantics to `SqliteStore` (that file is the
reference), wire it into `build_store`, and add a schema-validation branch in
`config/schema.py`. Mirror `tests/` coverage for both backends.

**Add a config key.** Read it where it's used, document it in
`config.example.yaml` with a comment, and — if it is safety-relevant — validate
it in `config/schema.py` so a bad value fails closed on load/reload.

**Add an entity type.** List it under `llm.entity_types` and give it an action
under `anonymizer.entities`. The NER prompt/schema is derived from
`entity_types`; no code change needed for a standard type.

**Touch the detection path?** Measure recall before and after. Over-masking is
acceptable (reversed on response); a new way for a real value to reach upstream
is a release blocker.

### Coding conventions
- Match the surrounding style; keep functions small and the data flow obvious.
- Fail closed on the redaction path: if a filter errors in `fail_closed` mode
  it must raise (block the send), never silently pass content through.
- Keep comments about *why*, not *what*; the existing files set the tone.
- No new runtime dependency without a reason — the dependency surface is part
  of the threat model.
- Never log original PII except behind the explicit `redaction_log.debug` flag.

---

## 5. Versioning & releases

Versions follow [SemVer](https://semver.org) and are **derived from git tags**
by setuptools-scm — there is no version literal to edit.

- `MAJOR` — a breaking change to the HTTP API, config schema, or `Store`/`Processor` interface.
- `MINOR` — a backward-compatible feature.
- `PATCH` — a backward-compatible fix.

A maintainer cuts a release by tagging:
```bash
git tag -a v0.3.0 -m "…"   # build between tags → 0.3.1.dev5+g<sha>
```
`aiproxy --version` and `GET /healthz` report the resolved version. Don't tag
in a PR — propose the bump in the PR description and let a maintainer tag after
merge.

---

## 6. Submitting a PR

1. **Open an issue first** for anything non-trivial, so the approach is agreed
   before you build it. Small fixes can go straight to a PR.
2. **Branch** off `main` with a descriptive name (`feat/…`, `fix/…`, `docs/…`).
3. **Make the change** with tests; keep commits focused and the message in the
   imperative mood (`fix: stop re-tokenizing existing tokens`). A body
   explaining *why* helps review.
4. **Before pushing**, locally:
   ```bash
   pytest -q                 # all green, no network needed
   python -m aiproxy doctor --config config.yaml
   ```
   If you changed the detection path, include a short before/after recall note
   (a `/dryrun` against a known-PII sample is enough).
5. **Open the PR** against `main`. In the description cover: what changed and
   why, the security/recall impact (does any value now reach upstream that
   didn't before?), config keys added/changed, and how you tested. Link the
   issue.
6. **Review.** Expect questions focused on the leak surface and on keeping the
   recall-over-precision invariant. Keep the branch rebased on `main`; respond
   to comments with follow-up commits (don't force-push away the review
   history until it's ready to land).
7. **Docs.** Update `config.example.yaml`, the Manual, and this guide in the
   same PR when behavior or config changes — docs drift is treated as a bug.

### PR checklist
- [ ] Tests added/updated and the full suite passes offline.
- [ ] No new way for real data to reach upstream (or it's called out and justified).
- [ ] `fail_closed` still blocks on filter errors.
- [ ] New/changed config documented and (if safety-relevant) schema-validated.
- [ ] No original PII written to disk outside the `debug` flag.
- [ ] Docs updated; no version literal edited (tags own the version).

Thanks for contributing — the whole point of the project is that sensitive data
never leaks, so careful review of the detection and forwarding paths is a
feature, not friction.
