# Writing a processor (plugin) for aiproxy

A **processor** is aiproxy's single plug point. It sees each request on the way
out (redact here) and each response on the way back (restore here). Built-in
processors live in `aiproxy/processors/`; this doc is about **custom** ones an
organization adds without modifying the aiproxy codebase (Phase 6).

> ⚠️ **Loading a plugin executes arbitrary code with full proxy privileges** —
> the mapping store, the at-rest decryption key, and all traffic in the clear.
> This is an **admin** path, never user self-service. Users *submit* a
> processor; an admin *reviews and places* it. Keep this distinct from the
> per-user term self-service (`/terms`), which is safe and additive.

## The interface

```python
from aiproxy.pipeline.base import Processor, SessionContext
from aiproxy.document import Document, PartKind

class MyProcessor(Processor):
    name = "myprocessor"                       # unique; used in pipeline:
    handles = frozenset({PartKind.TEXT})       # which part kinds it touches

    def process_request(self, doc: Document, ctx: SessionContext) -> Document:
        # Outbound (caller -> upstream). Redact/transform here.
        return doc

    def process_response(self, doc: Document, ctx: SessionContext) -> Document:
        # Inbound (upstream -> caller). Undo exactly what you did outbound.
        return doc
```

Rules the loader enforces (fail closed) at load time:

- Must subclass `Processor` and be defined in the plugin module.
- Must set a non-empty, unique `name` (not `"unnamed"`).
- Must implement both `process_request` and `process_response`.
- Its `name` may **not** shadow a built-in or another plugin.

Conventions you must follow by hand (not machine-checked):

- **Scope every stored value by `ctx.session_id`.** Never mix sessions.
- Reversible masking goes through the store so `process_response` can restore
  it. Use irreversible `[REDACTED]` only when you never need the value back.
- Respect `ctx.config["mode"]`: on an internal error, `fail_closed` must raise
  (block the send), `fail_open` may pass the content through.
- The request path runs processors top→bottom; the response path runs them
  **bottom→top**, so your restore undoes your own redaction symmetrically.

## Packaging

Two ways to ship a reviewed processor:

### 1. Loose file (simple)
Drop the `.py` into the configured directory:

```yaml
plugins:
  enabled: true
  dir: /etc/aiproxy/plugins     # root-owned, reviewed files only
```

Files whose name starts with `_` are ignored. Every `Processor` subclass
defined in the file is registered by its `name`.

### 2. Pip package via entry point (recommended for orgs)
Versioned, uninstallable, no loose code on disk:

```toml
# pyproject.toml of your plugin package
[project.entry-points."aiproxy.processors"]
myprocessor = "my_pkg.module:MyProcessor"
```

```yaml
plugins:
  enabled: true
  entry_points: true
  entry_point_group: aiproxy.processors   # default
```

## Enabling a vetted plugin

Adding the processor is a **one-line config change**, never a code change:

```yaml
pipeline:
  - logger
  - secrets_regex
  - anonymizer
  - myprocessor        # <- placed after review
  - image_redactor
```

Order matters: put a redactor before `anonymizer` only if it should run first.

## Review & signing workflow (admin)

1. Developer submits the processor as a merge request / ticket (never drops it
   on a server directly).
2. Security reviews the code: what it stores, where it sends data, whether it
   touches the key or the raw traffic, dependency provenance.
3. On approval, an admin places the file (or releases the pip package) in the
   controlled location and adds its `name` to `pipeline:` in central config.
4. Optionally restrict which names may load:
   ```yaml
   plugins:
     allow: [myprocessor]     # only these names load, even if others are present
   ```
5. aiproxy logs every loaded plugin at startup/reload:
   `loaded plugin processor name=… source=… sha256=…`. Capture that in your
   central logging as the audit record; alert on any unexpected `name`/`sha256`.

### On sandboxing — stated honestly
There is **no reliable in-process sandbox for Python**: a loaded module can
reach the store, the key and all traffic. The realistic controls are
**review + signing + least privilege** (run the proxy as a user with no more
access than it needs, keep the key in a secrets manager, segment the network),
not runtime isolation. If you need hard isolation, run the untrusted processor
as a separate network service and call it from a thin built-in processor, so
the privilege boundary is a process/host boundary, not an `import`.
