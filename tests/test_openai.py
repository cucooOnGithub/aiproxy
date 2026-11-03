"""OpenAI adapter + OpenAI SSE streaming restore."""

from __future__ import annotations

import base64
import json
import tempfile

from aiproxy.document import PartKind
from aiproxy.proxy.adapters import OpenAIAdapter
from aiproxy.proxy.stream import Restorer, stream_openai
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _store_with(sid, token, original, etype="ORG"):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    s = SqliteStore(db, Cipher.null())
    s.put(sid, token, original, etype)
    return s


def test_request_roundtrip_string_and_parts():
    a = OpenAIAdapter()
    png = base64.b64encode(b"\x89PNG").decode()
    body = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": [
                {"type": "text", "text": "see this"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{png}"}},
                {"type": "image_url",
                 "image_url": {"url": "https://x.com/a.png"}},
            ]},
        ],
    }
    doc = a.to_document(body)
    texts = [p for p in doc.parts if p.kind is PartKind.TEXT and not p.origin.get("inert")]
    images = doc.media_parts()
    assert {p.text for p in texts} == {"hi", "see this"}
    assert len(images) == 1 and images[0].data == b"\x89PNG"

    # Mutate and write back.
    texts[0].text = "HI"
    images[0].data = b"XX"
    out = a.from_document(doc)
    assert out["messages"][0]["content"] == "HI"
    url = out["messages"][1]["content"][1]["image_url"]["url"]
    assert url == f"data:image/png;base64,{base64.b64encode(b'XX').decode()}"
    # Remote URL untouched.
    assert out["messages"][1]["content"][2]["image_url"]["url"] == "https://x.com/a.png"


def test_response_roundtrip():
    a = OpenAIAdapter()
    body = {"choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "x"}}]}
    doc = a.response_to_document(body)
    doc.parts[0].text = "restored"
    assert a.response_from_document(doc)["choices"][0]["message"]["content"] == "restored"


async def _lines(items):
    for i in items:
        yield i


async def test_stream_openai_restores_split_token():
    store = _store_with("s", "⟦ORG_1⟧", "Acme")
    def chunk(text):
        return "data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk", "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
    items = [chunk("at ⟦ORG"), "", chunk("_1⟧ inc"), "", "data: [DONE]"]
    make = lambda: Restorer("s", store)  # noqa: E731
    out = "".join([c async for c in stream_openai(_lines(items), make)])
    assert "Acme" in out
    assert "⟦ORG_1⟧" not in out
    assert out.rstrip().endswith("[DONE]")
