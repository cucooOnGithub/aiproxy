"""Phase 4: at-rest key sourcing (env/file/command)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from aiproxy.store.crypto import Cipher


def test_env_source(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("AIPROXY_STORE_KEY", k)
    c = Cipher.from_config({"encrypt": True, "key_source": "env"})
    assert c.decrypt(c.encrypt(b"hi")) == b"hi"


def test_file_source(tmp_path):
    k = Fernet.generate_key().decode()
    f = tmp_path / "key"
    f.write_text(k + "\n")
    c = Cipher.from_config({"key_source": "file", "key_path": str(f)})
    assert c.decrypt(c.encrypt(b"hi")) == b"hi"


def test_command_source(tmp_path):
    k = Fernet.generate_key().decode()
    f = tmp_path / "key"
    f.write_text(k)
    c = Cipher.from_config({"key_source": "command",
                            "key_command": f"cat {f}"})
    assert c.decrypt(c.encrypt(b"hi")) == b"hi"


def test_file_source_requires_path():
    with pytest.raises(RuntimeError):
        Cipher.from_config({"key_source": "file"})


def test_unknown_source():
    with pytest.raises(RuntimeError):
        Cipher.from_config({"key_source": "smoke-signals"})
