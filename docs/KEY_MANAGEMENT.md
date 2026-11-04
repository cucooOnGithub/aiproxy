# Key & secret management (Phase 4)

The mapping store is the **de-anonymization key**: anyone with the store file
*and* the at-rest key can reverse every redaction. This doc covers where the
at-rest key comes from and how to rotate it.

## The at-rest key

Originals (and cached redacted blobs) are encrypted with Fernet
(AES-128-CBC + HMAC). Generate a key:

```bash
python -m aiproxy genkey        # prints a Fernet key
```

## Sourcing the key (no long-lived secret on disk)

`store.key_source` selects where the key is read from at startup/reload:

| source    | config                       | use                                  |
|-----------|------------------------------|--------------------------------------|
| `env`     | `key_env` (default `AIPROXY_STORE_KEY`) | single box, systemd `EnvironmentFile` with 0400 |
| `file`    | `key_path`                   | a mounted secret (Kubernetes/Nomad secret, `/run/secrets/...`) |
| `command` | `key_command`                | shell out to a secrets manager / KMS CLI; stdout is the key |

```yaml
store:
  encrypt: true
  key_source: command
  key_command: "vault kv get -field=key secret/aiproxy/store"
```

The `command` source keeps aiproxy agnostic to the secrets manager — point it
at Vault, AWS/GCP Secrets Manager, Azure Key Vault, `sops`, etc. The proxy
never writes the key anywhere; it holds it only in memory for the life of the
process.

## Per-tenant keys (optional, recommended at scale)

A single store key means one compromise exposes every user. To contain blast
radius, key by tenant/user and route at encryption time. aiproxy's `Cipher`
is resolved once today; to adopt per-tenant keys, wrap the store so the
`Cipher` is selected per `user_id`/tenant from the secrets manager. This is a
documented extension point, not yet wired by default — track it alongside the
Phase 2 `user_id` plumbing.

## Rotation

Two strategies:

### 1. Bulk re-encrypt (simple, has a maintenance window)
1. Stand up with the old key still readable.
2. For every row, `decrypt(old)` then `encrypt(new)` and write back.
3. Flip `key_source` to the new key; restart/reload.

A re-encrypt helper is the natural home for this (`aiproxy rekey`), reading
old+new keys and rewriting `mappings` and `redaction_cache`. Until it ships,
rotate by exporting/decrypting with the old key and re-importing with the new.

### 2. Envelope encryption (no bulk re-encrypt)
Encrypt each row with a per-row data key; wrap the data key with a master key
(KEK) held in the KMS. Rotating the KEK re-wraps only the data keys, not the
data. This avoids touching every row at rotation time and is the preferred
model for large stores. It is a schema change (store the wrapped data key per
row) — plan it with the Postgres backend rollout.

## Operational hardening (Phase 5)

- Turn **off** `debug`, `log_all` and especially `mode: observe` in
  production — `observe` forwards real data upstream in the clear. Gate any
  diagnostic behind an explicit, audited admin flag.
- Lock down and set retention on the redaction log; centralize it.
- Treat the store file / database with the same controls as the key: restricted
  access, encrypted backups, audited reads.
