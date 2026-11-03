"""Streaming (SSE) response restoration for Anthropic.

Upstream streams token deltas. A restore token (⟦PERSON_1⟧) may be split
across SSE `text_delta` events, so restoration cannot be done per-chunk.

`Restorer` keeps a rolling per-content-block buffer:
  - replace every COMPLETE ⟦TYPE_N⟧ with its stored original,
  - emit only the prefix that cannot contain a partial token (everything
    before the last lone ⟦),
  - hold the tail until more text arrives, flush it at block end.

`stream_anthropic` parses the upstream SSE event blocks, rewrites the text
inside `content_block_delta` / `text_delta` events, flushes the held tail
on `content_block_stop`, and passes every other event through verbatim.

Only `text_delta` is restored. `input_json_delta` (tool-call argument
fragments) is passed through untouched — restoring partial JSON is unsafe;
if you anonymize tool arguments, buffer and restore them whole instead.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Callable

from aiproxy.processors.tokens import TOKEN_RE

_OPEN = "⟦"  # ⟦


class Restorer:
    """Incremental, sentinel-safe token->original restorer for one block."""

    def __init__(self, session_id: str, store):
        self.sid = session_id
        self.store = store
        self.buf = ""

    def _repl(self, m: "re.Match") -> str:  # noqa: F821
        orig = self.store.original_for(self.sid, m.group(0))
        return orig if orig is not None else m.group(0)

    def feed(self, text: str) -> str:
        self.buf += text
        self.buf = TOKEN_RE.sub(self._repl, self.buf)
        # Everything after the last lone ⟦ might be a partial token: hold it.
        idx = self.buf.rfind(_OPEN)
        if idx == -1:
            out, self.buf = self.buf, ""
            return out
        out, self.buf = self.buf[:idx], self.buf[idx:]
        return out

    def flush(self) -> str:
        self.buf = TOKEN_RE.sub(self._repl, self.buf)
        out, self.buf = self.buf, ""
        return out


def _sse(event: str | None, data: dict) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n"


async def stream_anthropic(
    lines: AsyncIterator[str],
    make_restorer: Callable[[], Restorer],
) -> AsyncIterator[str]:
    restorers: dict[int, Restorer] = {}
    cur_event: str | None = None
    cur_data: str | None = None

    async for line in lines:
        if line == "":  # end of one SSE event block
            if cur_data is None:
                continue
            try:
                data = json.loads(cur_data)
            except json.JSONDecodeError:
                # Pass through anything we can't parse, unchanged.
                yield _raw(cur_event, cur_data)
                cur_event = cur_data = None
                continue

            dtype = data.get("type")
            if dtype == "content_block_delta" and \
                    data.get("delta", {}).get("type") == "text_delta":
                idx = data.get("index", 0)
                r = restorers.setdefault(idx, make_restorer())
                data["delta"]["text"] = r.feed(data["delta"].get("text", ""))
                yield _sse(cur_event, data)
            elif dtype == "content_block_stop":
                idx = data.get("index", 0)
                r = restorers.get(idx)
                if r:
                    tail = r.flush()
                    if tail:
                        yield _sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "text_delta", "text": tail},
                        })
                yield _sse(cur_event, data)
            else:
                yield _sse(cur_event, data)

            cur_event = cur_data = None
            continue

        if line.startswith("event:"):
            cur_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            cur_data = line[len("data:"):].strip()
        # Other SSE fields (id:, retry:, comments) are ignored.


def _raw(event: str | None, data: str) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {data}\n\n"


async def stream_openai(
    lines: AsyncIterator[str],
    make_restorer: Callable[[], Restorer],
) -> AsyncIterator[str]:
    """OpenAI Chat Completions SSE.

    No `event:` lines; each chunk is `data: {json}`; `delta.content` holds
    text; stream ends with `data: [DONE]`. One restorer per `choices` index
    (`n>1` gives parallel choices). On `[DONE]` any held tail is flushed as
    a final synthetic chunk per choice so no partial token is lost.
    """
    restorers: dict[int, Restorer] = {}
    last_chunk: dict | None = None

    async for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()

        if payload == "[DONE]":
            for idx, r in restorers.items():
                tail = r.flush()
                if tail and last_chunk is not None:
                    yield _data(_mk_chunk(last_chunk, idx, tail))
            yield "data: [DONE]\n\n"
            return

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            yield f"data: {payload}\n\n"
            continue

        last_chunk = chunk
        for choice in chunk.get("choices", []):
            idx = choice.get("index", 0)
            delta = choice.get("delta", {})
            if isinstance(delta.get("content"), str):
                r = restorers.setdefault(idx, make_restorer())
                delta["content"] = r.feed(delta["content"])
        yield _data(chunk)


def _data(chunk: dict) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def _mk_chunk(template: dict, idx: int, text: str) -> dict:
    """A minimal trailing chunk carrying flushed tail text for one choice."""
    return {
        "id": template.get("id"),
        "object": template.get("object", "chat.completion.chunk"),
        "model": template.get("model"),
        "choices": [{"index": idx, "delta": {"content": text}, "finish_reason": None}],
    }
