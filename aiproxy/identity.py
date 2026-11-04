"""Identity resolution at request ingress (Phase 2).

Produces a stable `user_id` for every request so a shared proxy can namespace
mappings per user: one user can never reverse another's tokens and sequence
numbers never collide across users. The namespacing itself is done by
prefixing the session id with the user id (see `scoped_session`), which keeps
the store backend user-agnostic for its mapping methods.

Sources (config `identity.source`):
  anonymous  (default) - single shared namespace; standalone/single-box.
  api_key              - MVP: the proxy maps a per-user API key (the inbound
                         x-api-key / Authorization bearer) to a user_id via
                         `identity.keys: {<key>: <user_id>}`.
  header               - an auth gateway (OIDC/SSO) in front terminates auth
                         and injects a TRUSTED signed identity header
                         (`identity.header`, e.g. x-auth-user). The proxy
                         trusts it; the gateway must strip any client-supplied
                         copy. This is the target production model.
"""

from __future__ import annotations

import hashlib

ANONYMOUS = "anon"


def _bearer(req_headers) -> str:
    key = req_headers.get("x-api-key") or req_headers.get("authorization", "")
    return key.removeprefix("Bearer ").removeprefix("bearer ").strip()


def resolve_user_id(req_headers, cfg: dict) -> str:
    """Resolve a stable user_id from the request headers per config."""
    ident = cfg.get("identity") or {}
    source = (ident.get("source") or "anonymous").lower()

    if source == "anonymous":
        return ANONYMOUS

    if source == "api_key":
        key = _bearer(req_headers)
        keys = ident.get("keys") or {}
        if key and key in keys:
            return str(keys[key])
        # Unknown/absent key: fall back to a stable opaque id derived from the
        # key so an unmapped caller still gets its OWN isolated namespace
        # (never shares with another). Never the raw key (it's a secret).
        if key:
            return "k_" + hashlib.sha256(key.encode()).hexdigest()[:16]
        return ANONYMOUS

    if source == "header":
        hdr = ident.get("header", "x-auth-user")
        val = (req_headers.get(hdr) or "").strip()
        return val or ANONYMOUS

    return ANONYMOUS


def scoped_session(user_id: str, raw_session: str) -> str:
    """Namespace a session id by user so stores stay per-user isolated."""
    return f"{user_id}/{raw_session}"
