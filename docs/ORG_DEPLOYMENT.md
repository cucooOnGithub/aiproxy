# aiproxy — Organization Deployment Implementation Plan

How to evolve aiproxy from a single-box tool into a shared service for a large
organization: central config owned by IT/security, per-user custom strings,
horizontal scale, and a safe path for user-contributed processors.

## Guiding constraints
- Recall > precision stays the invariant (reversing happens on return).
- `fail_closed` is the org default; no user may weaken it.
- The proxy holds no upstream key (keep the passthrough auth model).
- Users self-serve *terms*, never config or code.

## Phase 0 — Decouple config from the binary (prerequisite)
Today you rebuild `dist/aiproxy` for code changes and restart for config.
Before anything else:
- Separate **code artifact** (versioned container image / binary) from
  **config delivery**.
- Make config reloadable at runtime (SIGHUP or a watch on the config source)
  so a central change doesn't require restarting every replica.
- Add a config **schema validator** that runs on load and refuses to
  start/reload on invalid config (fail closed).

**Deliverable:** config hot-reload + validation. No behavior change for
single-box users.

## Phase 1 — Pluggable store backend + Postgres
`map.db` SQLite is single-writer and local; it blocks horizontal scale.
- Introduce a `Store` interface (the concrete methods already exist:
  `next_seq`, `put`, `get`, `all_mappings`, `purge_session`).
- Keep `SqliteStore` as the default/standalone backend.
- Add `PostgresStore` implementing the same interface, for multi-replica
  deployments.
- Move at-rest encryption to the store layer (already there) and source
  `AIPROXY_STORE_KEY` from a secrets manager (Phase 4), not env-in-a-file.

**Deliverable:** `store.backend: postgres` works; all existing tests pass
against both backends.

## Phase 2 — Identity + per-user namespacing
Today mappings are keyed by `session_id` only. For a shared proxy:
- Add an **identity resolution** step at request ingress producing a stable
  `user_id`:
  - MVP: per-user API key the proxy issues, mapped to `user_id`.
  - Target: an auth gateway (OIDC/SSO) in front injecting a signed identity
    header the proxy trusts.
- Thread `user_id` into `SessionContext`.
- Change the store key from `(session_id, token)` to
  `(user_id, session_id, token)` so one user can never reverse another's
  tokens and sequences never collide across users.
- Scope archive + redaction log by `user_id` too.

**Deliverable:** two users through one proxy get fully isolated mappings;
reversal is correct per user.

## Phase 3 — Two-tier term management (the per-user strings answer)
Split term lists by owner:

| Tier | Lives in | Owner | Scope |
|------|----------|-------|-------|
| Org | central config | security team | all users, mandatory |
| User | store table `user_terms(user_id, kind, value)` | the user | that user only |

- Anonymizer merges `org_terms ∪ user_terms(user_id)` at request time; org
  tier loaded from config, user tier queried from store (cached, TTL).
- **Governance rule (enforced on write):** user tier may only *add*
  `always_tokenize`/`denylist` entries. It may **not** remove or allowlist
  anything in the org tier. Validate and reject.
- Self-service surface:
  - `GET/POST/DELETE /terms` authenticated as the calling user.
  - A page on the existing dashboard to add/remove your own strings.
- No YAML edits by users, ever. Live effect, no redeploy.

**Deliverable:** a user adds a string via the dashboard/API and it's masked on
their next request; it never affects other users.

## Phase 4 — Secrets & key management
- `AIPROXY_STORE_KEY` sourced from Vault/KMS/cloud secrets manager.
- Consider **per-tenant keys** so a store compromise doesn't expose everyone
  (adds key-routing by `user_id`/tenant).
- Document key-rotation procedure (re-encrypt mappings; or envelope encryption
  to avoid bulk re-encrypt).

**Deliverable:** no long-lived secret on disk; documented rotation.

## Phase 5 — Scale-out & operations
- Run **proxy frontends stateless** behind a load balancer (now possible after
  Phases 1–2).
- Promote **NER to a dedicated shared service**: one GPU host, bigger/warm
  model (the commented 7B/8B ensemble becomes viable when it's warm and shared
  instead of per-request cold-load). All replicas call it over HTTP; add
  health/readiness and a queue.
- Shared object storage (or disable) for the archive.
- **Observability:** per-user/tenant metrics, leak-rate monitoring, aggregated
  dashboard.
- **Harden diagnostics:** `debug`/`log_all` off by default in prod; consider
  removing `mode: observe` from prod configs or gating it behind an explicit,
  audited admin flag (it sends real data upstream in clear).
- Centralize + lock down the redaction log; set retention.

**Deliverable:** N proxy replicas + 1 shared NER service under load; dashboards
live.

## Phase 6 — Drop-in custom processors (plugin loading)
Goal: a user develops a `Processor`, hands it to admins, admins drop the file
in a directory, and it's picked up without editing `registry.py`.

**It's feasible with minimal change** — the interface is already clean
(`Processor` ABC with `name`, `handles`, `process_request`,
`process_response`). Design:

1. **Plugin directory**, e.g. `aiproxy/plugins/` (or a configurable
   `plugins.dir`, outside the repo tree for an org).
2. **Auto-discovery at startup/reload:** scan the directory, `importlib`-load
   each module, find every `Processor` subclass, and register it into
   `REGISTRY` by its `name`. Keep built-ins as the base registry; plugins
   extend it.
   - Alternative (cleaner for packaging): Python **entry points**
     (`importlib.metadata`, group `aiproxy.processors`) so plugins are
     pip-installable packages rather than loose files. Recommended for an org
     — versioned, uninstallable, no loose code.
3. **Pipeline wiring unchanged:** admin adds the plugin's `name` to `pipeline:`
   in central config; `build_pipeline` already resolves names via `REGISTRY`.
   So enabling a vetted plugin is a one-line config change, not a code change.
4. **Name-collision handling:** refuse to load a plugin whose `name` shadows a
   built-in or another plugin (fail closed).

### Security gate (mandatory — this is code execution)
A directory that auto-imports `.py` files runs arbitrary code with proxy
privileges, including access to the store, the decryption key, and all
traffic. So:
- **Not a user self-service path.** Users *submit* processors; **admins review
  and place them**. Keep this distinct from Phase 3 term self-service.
- **Review checklist / signing:** require a security review before a plugin
  lands; optionally verify a signature/manifest so only approved plugins load.
- **Default off:** plugin loading disabled unless `plugins.enabled: true` and a
  directory is explicitly configured.
- **Interface validation on load:** reject anything not subclassing
  `Processor` or missing required attributes.
- **Isolation (stretch):** consider running plugins in a restricted context;
  full sandboxing of in-process Python is limited, so the realistic control is
  review + signing + least-privilege, documented honestly.
- **Audit:** log every plugin loaded (name, file, hash) at startup.

**Deliverable:** admin drops a reviewed processor (loose file *or* pip package
via entry point), adds its name to `pipeline:`, and it runs — with discovery,
collision refusal, signing/review gate, and audit logging. Add a
`docs/WRITING_A_PROCESSOR.md` with the submission + review workflow.

## Suggested sequencing
Phases 0→1→2→3 are the critical path for multi-user (they unblock everything).
Phase 4 pairs with 1. Phase 5 is operational scale once state is externalized.
Phase 6 is independent and can be done any time after Phase 0 — but ship it
only with the security gate.

**Smallest first increment that proves the model:** Phase 1 (Postgres store) +
Phase 2 (`user_id`) + Phase 3 (per-user terms table + dashboard page). That
alone delivers "central config, users add their own strings."
