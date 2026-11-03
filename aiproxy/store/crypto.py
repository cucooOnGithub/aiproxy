"""At-rest encryption for stored originals.

Uses Fernet (AES-128-CBC + HMAC) from `cryptography`. Key comes from the
OS keyring or an env var — never the config file. `Cipher.null()` is a
no-op passthrough for when encryption is disabled (dev only).
"""

from __future__ import annotations

import os


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

    def encrypt(self, data: bytes) -> bytes:
        return self._f.encrypt(data) if self._f else data

    def decrypt(self, data: bytes) -> bytes:
        return self._f.decrypt(data) if self._f else data


if __name__ == "__main__":  # key generator helper
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
