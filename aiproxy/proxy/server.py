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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from aiproxy.metrics import METRICS
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

    @app.get("/metrics")
    async def metrics():
        """JSON counters: runtime (since start) + durable store totals."""
        snap = METRICS.snapshot()
        snap["store"] = store.stats()
        return JSONResponse(snap)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(_DASHBOARD_HTML)

    async def _handle(req: Request, provider: str, path: str) -> Response:
        body = await req.json()
        adapter = ADAPTERS[provider]
        ctx = SessionContext(_session_id(req, body), store, cfg)
        METRICS.inc("requests")
        METRICS.inc(f"req:{provider}")

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


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aiproxy dashboard</title>
<style>
 :root{color-scheme:light dark}
 body{font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:860px}
 h1{font-size:20px;margin:0 0 4px} .sub{opacity:.6;margin:0 0 20px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
 .card{border:1px solid #8884;border-radius:10px;padding:14px}
 .card .n{font-size:26px;font-weight:600} .card .l{opacity:.6;font-size:12px}
 table{border-collapse:collapse;width:100%;margin-bottom:24px}
 th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #8883}
 td.r,th.r{text-align:right} h2{font-size:14px;opacity:.7;margin:0 0 6px}
</style></head><body>
<h1>aiproxy</h1><p class="sub">anonymization metrics · refreshing every 3s</p>
<div class="grid" id="cards"></div>
<h2>Entities anonymized by type</h2><table id="ents"></table>
<h2>Documents by kind</h2><table id="docs"></table>
<h2>Store (durable)</h2><table id="store"></table>
<script>
function row(k,v){return `<tr><td>${k}</td><td class="r">${v}</td></tr>`}
function card(n,l){return `<div class="card"><div class="n">${n}</div><div class="l">${l}</div></div>`}
function fmtUptime(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+'h '+m+'m'}
async function tick(){
 try{
  const m=await (await fetch('metrics')).json();
  document.getElementById('cards').innerHTML=
    card(m.requests_total,'requests')+
    card(m.text_parts_redacted,'text parts redacted')+
    card(m.tokens_created,'tokens created')+
    card((m.store&&m.store.total_mappings)||0,'stored mappings')+
    card(m.cache_hits,'cache hits')+
    card(m.ner_blocks,'NER blocks')+
    card((m.store&&m.store.sessions)||0,'sessions')+
    card(fmtUptime(m.uptime_seconds),'uptime');
  const ents=m.entities_by_type||{};
  document.getElementById('ents').innerHTML=
    '<tr><th>type</th><th class=r>count</th></tr>'+
    (Object.keys(ents).length?Object.entries(ents).map(([k,v])=>row(k,v)).join(''):'<tr><td colspan=2>none yet</td></tr>');
  const docs=m.documents_by_kind||{};
  document.getElementById('docs').innerHTML=
    '<tr><th>kind</th><th class=r>count</th></tr>'+
    (Object.keys(docs).length?Object.entries(docs).map(([k,v])=>row(k,v)).join(''):'<tr><td colspan=2>none yet</td></tr>');
  const s=m.store||{},bt=s.mappings_by_type||{};
  document.getElementById('store').innerHTML=
    row('total mappings',s.total_mappings||0)+row('sessions',s.sessions||0)+
    row('cached documents',s.cached_documents||0)+
    Object.entries(bt).map(([k,v])=>row('mappings · '+k,v)).join('');
 }catch(e){}
}
tick();setInterval(tick,3000);
</script></body></html>"""
