"""Maps processor names (from config) to Processor classes.

Built-ins are fixed; plugins (Phase 6) extend the registry at startup/reload
when `plugins.enabled` is set. `build_pipeline` resolves every name in
`pipeline:` against the composed registry, so enabling a reviewed plugin is a
one-line config change, never a code change.
"""

from __future__ import annotations

from aiproxy.pipeline.base import Pipeline, Processor
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.processors.image_redactor import ImageRedactor
from aiproxy.processors.logger import Logger
from aiproxy.processors.secrets_regex import SecretsRegex

BUILTINS: dict[str, type[Processor]] = {
    Logger.name: Logger,
    SecretsRegex.name: SecretsRegex,
    Anonymizer.name: Anonymizer,
    ImageRedactor.name: ImageRedactor,
}

# Back-compat alias: older code/tests referenced REGISTRY directly.
REGISTRY = BUILTINS


def build_registry(config: dict) -> dict[str, type[Processor]]:
    """Built-ins plus any enabled, reviewed, non-colliding plugins."""
    from aiproxy.pipeline.plugins import discover_plugins
    reg = dict(BUILTINS)
    plugins = discover_plugins(config.get("plugins") or {}, set(BUILTINS))
    reg.update(plugins)  # collisions already refused in discover_plugins
    return reg


def build_pipeline(names: list[str], config: dict) -> Pipeline:
    registry = build_registry(config)
    procs: list[Processor] = []
    for n in names:
        if n not in registry:
            raise KeyError(f"unknown processor '{n}'; known: {sorted(registry)}")
        procs.append(registry[n](config.get(n, {})))
    return Pipeline(procs)
