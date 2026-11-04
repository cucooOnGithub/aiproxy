"""Drop-in custom processor loading (Phase 6).

Goal: an admin drops a reviewed `Processor` into a directory (or installs it
as a pip package exposing an entry point), adds its `name` to `pipeline:` in
central config, and it runs — no edit to registry.py.

SECURITY (mandatory). Loading a plugin EXECUTES ARBITRARY CODE with full
proxy privileges: the store, the decryption key, and all traffic. Therefore:
  * This is NOT a user self-service path. Users SUBMIT processors; ADMINS
    review and place them. (Distinct from Phase 3 term self-service.)
  * Default OFF. Loading happens only when `plugins.enabled: true` and a
    directory and/or entry points are explicitly configured.
  * Interface validation on load: reject anything not subclassing `Processor`
    or missing required attributes.
  * Name-collision refusal: a plugin may never shadow a built-in or another
    plugin (fail closed).
  * Audit: every loaded plugin is logged with name, source file and sha256.
  * Optional allowlist (`plugins.allow: [names]`) and signature/manifest
    hooks are documented in docs/WRITING_A_PROCESSOR.md; full in-process
    sandboxing of Python is not achievable, so the real control is
    review + least privilege, stated honestly.

`discover_plugins` returns name->class WITHOUT mutating the global registry,
so it is pure and testable; `build_registry` composes built-ins + plugins.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
from pathlib import Path

from aiproxy.pipeline.base import Processor

_LOG = logging.getLogger("aiproxy.plugins")


class PluginError(RuntimeError):
    pass


def _validate(cls) -> None:
    if not (inspect.isclass(cls) and issubclass(cls, Processor)
            and cls is not Processor):
        raise PluginError(f"{cls!r} is not a Processor subclass")
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name or name == "unnamed":
        raise PluginError(f"{cls!r} must set a non-empty class attribute 'name'")
    for meth in ("process_request", "process_response"):
        if getattr(cls, meth, None) is getattr(Processor, meth, None):
            raise PluginError(f"{cls.__name__} does not implement {meth}")


def _processor_classes(module) -> list[type]:
    out = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        # Only classes DEFINED in this module (ignore imported Processor etc).
        if obj.__module__ == module.__name__ and issubclass(obj, Processor) \
                and obj is not Processor:
            out.append(obj)
    return out


def _load_file(path: Path) -> list[type]:
    spec = importlib.util.spec_from_file_location(
        f"aiproxy_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot load plugin module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # executes plugin code (reviewed only!)
    return _processor_classes(module)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_plugins(plugins_cfg: dict,
                     builtin_names: set[str]) -> dict[str, type]:
    """Return name->Processor class for every enabled, valid, non-colliding
    plugin. Does not touch the global registry. Raises PluginError on an
    invalid plugin or a name collision (fail closed)."""
    cfg = plugins_cfg or {}
    if not cfg.get("enabled"):
        return {}

    allow = set(cfg.get("allow") or [])
    found: dict[str, type] = {}

    def _register(cls, source: str, digest: str) -> None:
        _validate(cls)
        name = cls.name
        if allow and name not in allow:
            _LOG.warning("plugin '%s' from %s not in plugins.allow; skipping",
                         name, source)
            return
        if name in builtin_names:
            raise PluginError(
                f"plugin '{name}' ({source}) shadows a built-in processor")
        if name in found:
            raise PluginError(
                f"plugin name collision: '{name}' from {source} already "
                "loaded from another plugin")
        found[name] = cls
        _LOG.info("loaded plugin processor name=%s source=%s sha256=%s",
                  name, source, digest)

    # 1) Loose files in a directory.
    directory = cfg.get("dir")
    if directory:
        d = Path(directory)
        if not d.is_dir():
            raise PluginError(f"plugins.dir '{directory}' is not a directory")
        for py in sorted(d.glob("*.py")):
            if py.name.startswith("_"):
                continue
            digest = _sha256(py)
            for cls in _load_file(py):
                _register(cls, str(py), digest)

    # 2) Pip-installed packages via entry points (recommended for orgs:
    #    versioned, uninstallable, no loose code).
    if cfg.get("entry_points"):
        from importlib.metadata import entry_points
        group = cfg.get("entry_point_group", "aiproxy.processors")
        for ep in entry_points(group=group):
            cls = ep.load()
            _register(cls, f"entry_point:{ep.name}", f"dist:{ep.value}")

    return found
