"""FastAPI app: anonymizing reverse proxy.

Request flow:
  caller -> adapter.to_document -> pipeline.run_request (redact + store)
         -> adapter.from_document -> upstream
  upstream -> adapter.response_to_document -> pipeline.run_response (restore)
           -> adapter.response_from_document -> caller

When the caller requests stream=true the restored reply is streamed back
token-by-token (see stream.py); otherwise the JSON path below is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from aiproxy.pipeline.base import SessionContext
from aiproxy.pipeline.registry import build_pipeline
from aiproxy.proxy.adapters import ADAPTERS
from aiproxy.proxy.stream import Restorer, stream_anthropic, stream_openai

_STREAMERS = {"anthropic": stream_anthropic, "openai": stream_openai}
_PATHS = {"anthropic": "/v1/messages", "openai": "/v1/chat/completions"}
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _build_store(cfg: dict) -> SqliteStore:
    s = cfg["store"]
    cipher = Cipher.from_env() if s.get("encrypt") else Cipher.null()
    return SqliteStore(s.get("path", "./map.db"), cipher)


def _session_id(req: Request, body: dict) -> str:
    """Derive a stable per-conversation id.

    Prefer an explicit header the caller can set; else hash the API key +
    the first user message so the same conversation reuses the same tokens.
    """
    explicit = req.headers.get("x-aiproxy-session")
    if explicit:
        return explicit
    key = req.headers.get("x-api-key") or req.headers.get("authorization", "")
    seed = key + str(body.get("messages", [{}])[0])[:200]
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


async def _prewarm(pipeline, cfg: dict) -> None:
    """Load each processor's local model before serving, so the first
    request doesn't pay the cold-load cost against the per-request timeout.
    Controlled by `prewarm: true` (default). A warm failure is logged, not
    fatal — the model may just not be up yet."""
    if not cfg.get("prewarm", True):
        return
    seen = set()
    for p in pipeline.processors:
        ner = getattr(p, "ner", None)
        if ner is None or id(ner) in seen:
            continue
        seen.add(id(ner))
        try:
            await asyncio.to_thread(ner.warm)
            logging.getLogger("aiproxy").info("prewarmed %s", ner.model)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("aiproxy").warning(
                "prewarm of %s failed: %s", ner.model, e)


def create_app(cfg: dict) -> FastAPI:
    store = _build_store(cfg)
    pipeline = build_pipeline(cfg["pipeline"], cfg)
    upstreams = cfg["upstreams"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await _prewarm(pipeline, cfg)
        yield

    app = FastAPI(title="aiproxy", lifespan=lifespan)

    @app.post("/v1/messages")
    async def messages(req: Request):
        return await _handle(req, "anthropic", "/v1/messages")

    @app.post("/v1/chat/completions")
    async def chat_completions(req: Request):
        return await _handle(req, "openai", "/v1/chat/completions")

    @app.post("/dryrun")
    async def dryrun(req: Request, provider: str = "anthropic"):
        """Show what WOULD be redacted without forwarding anything.

        Pick the schema with ?provider=anthropic (default) or openai.
        """
        body = await req.json()
        adapter = ADAPTERS[provider]
        ctx = SessionContext(_session_id(req, body), store, cfg)
        doc = pipeline.run_request(adapter.to_document(body), ctx)
        return JSONResponse(adapter.from_document(doc))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    async def _handle(req: Request, provider: str, path: str) -> Response:
        body = await req.json()
        adapter = ADAPTERS[provider]
        ctx = SessionContext(_session_id(req, body), store, cfg)

        # Redact outbound.
        out_doc = pipeline.run_request(adapter.to_document(body), ctx)
        redacted = adapter.from_document(out_doc)

        up = upstreams[provider]["base_url"].rstrip("/")
        fwd_headers = {
            k: v for k, v in req.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        wants_stream = bool(body.get("stream"))

        if wants_stream:
            return _stream(ctx, provider, up, path, redacted, fwd_headers)

        # Non-streaming JSON path.
        redacted["stream"] = False
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{up}{path}", json=redacted, headers=fwd_headers)

        if r.status_code >= 400:
            return Response(r.content, status_code=r.status_code,
                            media_type="application/json")

        # Restore inbound.
        in_doc = pipeline.run_response(adapter.response_to_document(r.json()), ctx)
        return JSONResponse(adapter.response_from_document(in_doc))

    def _stream(ctx, provider, up, path, redacted, fwd_headers) -> StreamingResponse:
        """Forward with stream=true and restore tokens in the SSE reply."""
        redacted["stream"] = True
        streamer = _STREAMERS[provider]

        async def gen():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", f"{up}{path}", json=redacted, headers=fwd_headers
                ) as r:
                    if r.status_code >= 400:
                        body_bytes = await r.aread()
                        yield body_bytes.decode(errors="replace")
                        return
                    restorer = lambda: Restorer(ctx.session_id, ctx.store)  # noqa: E731
                    async for chunk in streamer(r.aiter_lines(), restorer):
                        yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
