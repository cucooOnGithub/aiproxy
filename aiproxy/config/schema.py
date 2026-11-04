"""Config schema validation (Phase 0).

Runs on every load AND every hot-reload. On invalid config it raises
`ConfigError`; the caller refuses to start or to apply the reload, so a bad
central change can never take a running proxy into an unsafe state
(fail-closed for config, mirroring fail-closed for redaction).

Kept deliberately dependency-free (no jsonschema/pydantic) and permissive
about unknown keys — it validates the structure and the safety-critical
invariants, not every optional processor knob.
"""

from __future__ import annotations

_VALID_MODES = {"fail_closed", "fail_open", "observe"}
_VALID_ACTIONS = {"anonymize", "redact", "ignore"}
_VALID_BACKENDS = {"sqlite", "postgres"}
_VALID_KEY_SOURCES = {"env", "file", "command"}


class ConfigError(ValueError):
    """Raised when config fails validation. Fail closed: do not start/reload."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def validate_config(cfg: dict) -> dict:
    """Validate a loaded config dict in place; return it. Raises ConfigError."""
    _require(isinstance(cfg, dict), "config root must be a mapping")

    # --- upstreams ---
    ups = cfg.get("upstreams")
    _require(isinstance(ups, dict) and ups,
             "upstreams: must be a non-empty mapping of provider -> {base_url}")
    for name, u in ups.items():
        _require(isinstance(u, dict) and isinstance(u.get("base_url"), str)
                 and u["base_url"].strip(),
                 f"upstreams.{name}.base_url must be a non-empty string")

    # --- pipeline ---
    pipe = cfg.get("pipeline")
    _require(isinstance(pipe, list) and pipe,
             "pipeline: must be a non-empty list of processor names")
    _require(all(isinstance(n, str) for n in pipe),
             "pipeline: every entry must be a processor name (string)")

    # --- mode ---
    mode = cfg.get("mode", "fail_closed")
    _require(mode in _VALID_MODES,
             f"mode: '{mode}' invalid; one of {sorted(_VALID_MODES)}")

    # --- store ---
    store = cfg.get("store")
    _require(isinstance(store, dict), "store: must be a mapping")
    backend = (store.get("backend") or "sqlite").lower()
    _require(backend in _VALID_BACKENDS,
             f"store.backend: '{backend}' invalid; one of {sorted(_VALID_BACKENDS)}")
    if backend == "postgres":
        _require(bool(store.get("dsn") or store.get("url")),
                 "store.backend: postgres requires store.dsn")
    if store.get("encrypt"):
        ks = (store.get("key_source") or "env").lower()
        _require(ks in _VALID_KEY_SOURCES,
                 f"store.key_source: '{ks}' invalid; one of {sorted(_VALID_KEY_SOURCES)}")
        if ks == "file":
            _require(bool(store.get("key_path")),
                     "store.key_source: file requires store.key_path")
        if ks == "command":
            _require(bool(store.get("key_command")),
                     "store.key_source: command requires store.key_command")

    # --- anonymizer entity actions ---
    anon = cfg.get("anonymizer") or {}
    _require(isinstance(anon, dict), "anonymizer: must be a mapping")
    ents = anon.get("entities") or {}
    _require(isinstance(ents, dict), "anonymizer.entities: must be a mapping")
    for etype, action in ents.items():
        _require(action in _VALID_ACTIONS,
                 f"anonymizer.entities.{etype}: '{action}' invalid; "
                 f"one of {sorted(_VALID_ACTIONS)}")

    # --- identity (Phase 2), optional ---
    ident = cfg.get("identity")
    if ident is not None:
        _require(isinstance(ident, dict), "identity: must be a mapping")
        src = (ident.get("source") or "anonymous").lower()
        _require(src in {"anonymous", "api_key", "header"},
                 "identity.source must be one of anonymous, api_key, header")
        if src == "api_key":
            keys = ident.get("keys") or {}
            _require(isinstance(keys, dict),
                     "identity.keys must be a mapping of api_key -> user_id")
        if src == "header":
            _require(bool(ident.get("header")),
                     "identity.source: header requires identity.header")

    # --- plugins (Phase 6), optional ---
    plugins = cfg.get("plugins")
    if plugins is not None:
        _require(isinstance(plugins, dict), "plugins: must be a mapping")
        if plugins.get("enabled"):
            _require(bool(plugins.get("dir")) or bool(plugins.get("entry_points")),
                     "plugins.enabled requires plugins.dir and/or "
                     "plugins.entry_points: true")

    return cfg
