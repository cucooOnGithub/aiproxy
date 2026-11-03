"""Streaming restore: tokens split across SSE chunks must still restore."""

from __future__ import annotations

import json
import tempfile

import pytest

from aiproxy.proxy.stream import Restorer, stream_anthropic
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _store_with(sid, token, original):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    s = SqliteStore(db, Cipher.null())
    s.put(sid, token, original, "PERSON")
    return s


def test_restorer_split_token():
    store = _store_with("s", "⟦PERSON_1⟧", "Alice")
    r = Restorer("s", store)
    # Token arrives in pieces across feeds.
    out = r.feed("hi ⟦PER")
    out += r.feed("SON_1")
    out += r.feed("⟧ there")
    out += r.flush()
    assert out == "hi Alice there"


def test_restorer_holds_partial_until_flush():
    store = _store_with("s", "⟦PERSON_1⟧", "Bob")
    r = Restorer("s", store)
    assert r.feed("x ⟦PERSON_1") == "x "   # tail held
    assert r.feed("⟧") == "Bob"


async def _lines(blocks):
    for b in blocks:
        for line in b:
            yield line


@pytest.mark.asyncio
async def test_stream_anthropic_restores_delta():
    store = _store_with("s", "⟦ORG_1⟧", "Acme")
    blocks = [
        ["event: content_block_delta",
         'data: ' + json.dumps({
             "type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "work at ⟦ORG_1"}}),
         ""],
        ["event: content_block_delta",
         'data: ' + json.dumps({
             "type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "⟧ inc"}}),
         ""],
        ["event: content_block_stop",
         'data: ' + json.dumps({"type": "content_block_stop", "index": 0}),
         ""],
    ]
    make = lambda: Restorer("s", store)  # noqa: E731
    out = "".join([c async for c in stream_anthropic(_lines(blocks), make)])
    assert "Acme" in out
    assert "⟦ORG_1⟧" not in out
