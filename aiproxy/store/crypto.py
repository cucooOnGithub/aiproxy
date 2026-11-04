"""At-rest encryption for stored originals.

Uses Fernet (AES-128-CBC + HMAC) from `cryptography`. Key comes from the
OS keyring or an env var — never the config file. `Cipher.null()` is a
no-op passthrough for when encryption is disabled (dev only).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class Cipher:
    def __init__(self, fernet):
        self._f = fernet

    @classmethod
    def null(cls) -> "Cipher":
        return cls(None)

    @classmethod
    def from_key(cls, key: bytes) -> "Cipher":
        from cryptography.fernet import Fernet

        return cls(Fernet(key))

    @classmethod
    def from_env(cls, var: str = "AIPROXY_STORE_KEY") -> "Cipher":
        key = os.environ.get(var)
        if not key:
            raise RuntimeError(
                f"store encryption enabled but {var} not set; "
                f"generate one with: python -m aiproxy.store.crypto"
            )
        return cls.from_key(key.encode())

    @classmethod
    def from_config(cls, store_cfg: dict) -> "Cipher":
        """Resolve the at-rest key from the configured source (Phase 4).

        `store.key_source` selects where AIPROXY_STORE_KEY comes from so no
        long-lived secret need sit in the config file or a dotfile:

            key_source: env       # default; AIPROXY_STORE_KEY in the env
            key_source: file      # read key_path (0400), e.g. a mounted secret
            key_source: command   # run key_command; its stdout is the key
                                  # (shell out to `vault kv get ...`, a KMS
                                  # CLI, `gcloud secrets ...`, etc.)

        This keeps the proxy agnostic to the secrets manager: point
        key_command at whatever your org runs. Key rotation: re-point the
        source at the new key and re-encrypt (see docs/KEY_MANAGEMENT.md).
        """
        src = (store_cfg.get("key_source") or "env").lower()
        if src == "env":
            return cls.from_env(store_cfg.get("key_env", "AIPROXY_STORE_KEY"))
        if src == "file":
            path = store_cfg.get("key_path")
            if not path:
                raise RuntimeError("store.key_source: file requires store.key_path")
            key = Path(path).read_text().strip()
            if not key:
                raise RuntimeError(f"store key file {path} is empty")
            return cls.from_key(key.encode())
        if src == "command":
            cmd = store_cfg.get("key_command")
            if not cmd:
                raise RuntimeError(
                    "store.key_source: command requires store.key_command")
            out = subprocess.run(cmd, shell=True, capture_output=True,
                                 text=True, check=True)
            key = out.stdout.strip()
            if not key:
                raise RuntimeError(
                    "store.key_command produced no key on stdout")
            return cls.from_key(key.encode())
        raise RuntimeError(
            f"unknown store.key_source '{src}'; known: env, file, command")

    def encrypt(self, data: bytes) -> bytes:
        return self._f.encrypt(data) if self._f else data

    def decrypt(self, data: bytes) -> bytes:
        return self._f.decrypt(data) if self._f else data


if __name__ == "__main__":  # key generator helper
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
