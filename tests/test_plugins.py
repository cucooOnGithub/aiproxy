"""Phase 6: drop-in processor plugin discovery + security gates."""

from __future__ import annotations

import pytest

from aiproxy.pipeline.plugins import PluginError, discover_plugins
from aiproxy.pipeline.registry import BUILTINS, build_registry

_GOOD = '''
from aiproxy.pipeline.base import Processor
class MyProc(Processor):
    name = "myproc"
    def process_request(self, doc, ctx):
        return doc
    def process_response(self, doc, ctx):
        return doc
'''

_SHADOW = '''
from aiproxy.pipeline.base import Processor
class Shadow(Processor):
    name = "anonymizer"
    def process_request(self, doc, ctx):
        return doc
    def process_response(self, doc, ctx):
        return doc
'''

_BADNAME = '''
from aiproxy.pipeline.base import Processor
class NoName(Processor):
    def process_request(self, doc, ctx):
        return doc
    def process_response(self, doc, ctx):
        return doc
'''


def _plugdir(tmp_path, **files):
    for fname, content in files.items():
        (tmp_path / fname).write_text(content)
    return str(tmp_path)


def test_disabled_by_default(tmp_path):
    d = _plugdir(tmp_path, **{"good.py": _GOOD})
    assert discover_plugins({"dir": d}, set(BUILTINS)) == {}  # enabled not set


def test_discovers_valid_plugin(tmp_path):
    d = _plugdir(tmp_path, **{"good.py": _GOOD})
    found = discover_plugins({"enabled": True, "dir": d}, set(BUILTINS))
    assert "myproc" in found


def test_refuses_builtin_shadow(tmp_path):
    d = _plugdir(tmp_path, **{"bad.py": _SHADOW})
    with pytest.raises(PluginError):
        discover_plugins({"enabled": True, "dir": d}, set(BUILTINS))


def test_rejects_nameless(tmp_path):
    d = _plugdir(tmp_path, **{"bad.py": _BADNAME})
    with pytest.raises(PluginError):
        discover_plugins({"enabled": True, "dir": d}, set(BUILTINS))


def test_allowlist_filters(tmp_path):
    d = _plugdir(tmp_path, **{"good.py": _GOOD})
    found = discover_plugins(
        {"enabled": True, "dir": d, "allow": ["other"]}, set(BUILTINS))
    assert found == {}  # myproc not in allow


def test_build_registry_composes(tmp_path):
    d = _plugdir(tmp_path, **{"good.py": _GOOD})
    reg = build_registry({"plugins": {"enabled": True, "dir": d}})
    assert "myproc" in reg and "anonymizer" in reg


def test_build_registry_builtins_only_when_disabled():
    reg = build_registry({})
    assert set(reg) == set(BUILTINS)
