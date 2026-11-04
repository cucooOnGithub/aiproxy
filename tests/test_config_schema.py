"""Phase 0: config schema validation fails closed on bad config."""

from __future__ import annotations

import pytest

from aiproxy.config.schema import ConfigError, validate_config


def _base():
    return {
        "upstreams": {"anthropic": {"base_url": "https://api.anthropic.com"}},
        "pipeline": ["anonymizer"],
        "store": {"backend": "sqlite", "path": "./map.db"},
        "mode": "fail_closed",
        "anonymizer": {"entities": {"PERSON": "anonymize"}},
    }


def test_valid_config_passes():
    assert validate_config(_base()) is not None


def test_missing_upstreams_rejected():
    cfg = _base()
    cfg["upstreams"] = {}
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_bad_mode_rejected():
    cfg = _base()
    cfg["mode"] = "yolo"
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_bad_entity_action_rejected():
    cfg = _base()
    cfg["anonymizer"]["entities"]["PERSON"] = "nuke"
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_postgres_requires_dsn():
    cfg = _base()
    cfg["store"] = {"backend": "postgres"}
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_unknown_backend_rejected():
    cfg = _base()
    cfg["store"] = {"backend": "mongo"}
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_key_source_file_requires_path():
    cfg = _base()
    cfg["store"] = {"backend": "sqlite", "encrypt": True, "key_source": "file"}
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_identity_api_key_shape():
    cfg = _base()
    cfg["identity"] = {"source": "api_key", "keys": {"k1": "alice"}}
    assert validate_config(cfg) is not None
    cfg["identity"] = {"source": "header"}  # missing header name
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_plugins_enabled_requires_source():
    cfg = _base()
    cfg["plugins"] = {"enabled": True}
    with pytest.raises(ConfigError):
        validate_config(cfg)
