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
import signal
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from aiproxy.archive import build_archive, build_redaction_log
from aiproxy.identity import resolve_user_id, scoped_session
from aiproxy.metrics import METRICS
from aiproxy.pipeline.base import SessionContext
from aiproxy.pipeline.registry import build_pipeline
from aiproxy.proxy.adapters import ADAPTERS
from aiproxy.proxy.stream import Restorer, stream_anthropic, stream_openai
from aiproxy.store.base import USER_TERM_KINDS, build_store

_STREAMERS = {"anthropic": stream_anthropic, "openai": stream_openai}
_PATHS = {"anthropic": "/v1/messages", "openai": "/v1/chat/completions"}

_LOG = logging.getLogger("aiproxy")


def _build_store(cfg: dict):
    """Construct the configured store backend (sqlite|postgres) with the
    configured at-rest key source. Kept for doctor/CLI back-compat."""
    return build_store(cfg)


class _State:
    """Mutable holder so a SIGHUP hot-reload can swap config, pipeline and
    upstreams in place without restarting the process or dropping the store
    (the store IS the de-anonymization key; it must survive a reload)."""

    def __init__(self, cfg: dict, store):
        self.store = store
        self.apply(cfg)

    def apply(self, cfg: dict) -> None:
        self.cfg = cfg
        self.upstreams = cfg["upstreams"]
        self.archive = build_archive(cfg)
        self.redaction_log = build_redaction_log(cfg)
        self.pipeline = build_pipeline(cfg["pipeline"], cfg)
        self._warn_unsafe(cfg)

    @staticmethod
    def _warn_unsafe(cfg: dict) -> None:
        """Loudly warn on diagnostics that leak real data upstream / to disk,
        so they are never left on in production unnoticed (Phase 5)."""
        if cfg.get("mode") == "observe":
            _LOG.warning("mode: observe — requests are forwarded UN-REDACTED "
                         "(real data upstream in clear). Diagnostic only.")
        rl = cfg.get("redaction_log") or {}
        if rl.get("debug"):
            _LOG.warning("redaction_log.debug: true — ORIGINAL PII is written "
                         "to disk in plaintext. Do not use in production.")
        if rl.get("log_all"):
            _LOG.warning("redaction_log.log_all: true — every request block is "
                         "logged, not just redacted ones.")


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


def create_app(cfg: dict, config_path: str | None = None) -> FastAPI:
    # The store survives reloads (it holds the de-anonymization key), so build
    # it once. Everything else lives in _State and can be swapped on SIGHUP.
    store = _build_store(cfg)
    state = _State(cfg, store)

    def _reload() -> None:
        """Hot-reload central config: validate, then swap pipeline/upstreams
        in place. Fail closed — on invalid config the running config is kept
        and the error logged, never applied (Phase 0)."""
        if not config_path:
            _LOG.warning("SIGHUP ignored: no config path to reload from")
            return
        try:
            from aiproxy.config.loader import load_config
            new_cfg = load_config(config_path)  # validates; raises on bad
            state.apply(new_cfg)
            _LOG.info("config reloaded from %s", config_path)
        except Exception as e:  # noqa: BLE001
            _LOG.error("config reload REJECTED (keeping running config): %s", e)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, _reload)
        except (NotImplementedError, ValueError, RuntimeError, AttributeError):
            pass  # no SIGHUP on this platform / not in main thread (tests)
        await _prewarm(state.pipeline, state.cfg)
        yield

    app = FastAPI(title="aiproxy", lifespan=lifespan)

    def _ctx(req: Request, body: dict) -> SessionContext:
        """Resolve identity and build a per-user-isolated SessionContext."""
        user_id = resolve_user_id(req.headers, state.cfg)
        session = scoped_session(user_id, _session_id(req, body))
        return SessionContext(session, store, state.cfg, state.archive,
                              user_id=user_id)

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
        doc = state.pipeline.run_request(adapter.to_document(body),
                                         _ctx(req, body))
        return JSONResponse(adapter.from_document(doc))

    @app.get("/healthz")
    async def healthz():
        from aiproxy import __version__
        return {"ok": True, "version": __version__}

    @app.get("/readyz")
    async def readyz():
        """Readiness: store reachable + NER model(s) warm. Used by the load
        balancer so a replica with a cold/broken NER is pulled from rotation
        (Phase 5)."""
        checks = {}
        try:
            store.stats()
            checks["store"] = True
        except Exception as e:  # noqa: BLE001
            checks["store"] = False
            checks["store_error"] = str(e)
        ready = all(v is True for k, v in checks.items() if not k.endswith("_error"))
        return JSONResponse({"ready": ready, "checks": checks},
                            status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics():
        """JSON counters: runtime (since start) + durable store totals."""
        snap = METRICS.snapshot()
        snap["store"] = store.stats()
        return JSONResponse(snap)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/terms", response_class=HTMLResponse)
    async def terms_page():
        return HTMLResponse(_TERMS_HTML)

    # --- per-user term self-service (Phase 3) --------------------------
    # Authenticated as the CALLING user via the same identity resolution as
    # the proxy path. A user only ever sees/edits their OWN terms; they can
    # add (not remove) their own always_tokenize/denylist entries and can
    # never touch the org tier (which is config, not in this table).
    def _uid(req: Request) -> str:
        return resolve_user_id(req.headers, state.cfg)

    @app.get("/api/terms")
    async def get_terms(req: Request):
        return JSONResponse({"user_id": _uid(req),
                             "terms": store.user_terms(_uid(req))})

    @app.post("/api/terms")
    async def add_term(req: Request):
        body = await req.json()
        kind, value = body.get("kind"), body.get("value")
        if kind not in USER_TERM_KINDS:
            return JSONResponse(
                {"error": f"kind must be one of {list(USER_TERM_KINDS)}"},
                status_code=400)
        if not isinstance(value, str) or not value.strip():
            return JSONResponse({"error": "value must be a non-empty string"},
                                status_code=400)
        store.add_user_term(_uid(req), kind, value)
        return JSONResponse({"ok": True}, status_code=201)

    @app.delete("/api/terms")
    async def delete_term(req: Request):
        body = await req.json()
        kind, value = body.get("kind"), body.get("value")
        n = store.remove_user_term(_uid(req), kind, value)
        return JSONResponse({"ok": True, "removed": n})

    async def _handle(req: Request, provider: str, path: str) -> Response:
        body = await req.json()
        adapter = ADAPTERS[provider]
        ctx = _ctx(req, body)
        ctx.log = state.redaction_log
        METRICS.inc("requests")
        METRICS.inc(f"req:{provider}")
        METRICS.inc(f"user:{ctx.user_id}")  # per-user request count (Phase 5)

        # Redact outbound.
        out_doc = state.pipeline.run_request(adapter.to_document(body), ctx)
        redacted = adapter.from_document(out_doc)

        up = state.upstreams[provider]["base_url"].rstrip("/")
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
        in_doc = state.pipeline.run_response(
            adapter.response_to_document(r.json()), ctx)
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

    app.state.reload = _reload  # exposed for tests / admin
    app.state.runtime = state   # exposed for tests / admin introspection
    return app

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
    card(m.cache_hits,'text cache hits')+
    card(m.document_cache_hits,'document cache hits')+
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


# Per-user term self-service page (Phase 3). Calls /api/terms authenticated as
# the browser's caller. For an org, front this with your SSO gateway so the
# identity header is injected; in api_key mode the user pastes their key.
_TERMS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aiproxy — my terms</title>
<style>
 :root{color-scheme:light dark}
 body{font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:720px}
 h1{font-size:20px;margin:0 0 4px} .sub{opacity:.6;margin:0 0 20px}
 fieldset{border:1px solid #8884;border-radius:10px;margin:0 0 18px;padding:14px}
 legend{opacity:.7;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 input,select,button{font:inherit;padding:6px 10px;border-radius:8px;
   border:1px solid #8886;background:transparent;color:inherit}
 button{cursor:pointer} label{display:block;margin:4px 0}
 ul{list-style:none;padding:0} li{display:flex;justify-content:space-between;
   padding:6px 0;border-bottom:1px solid #8883}
 .k{opacity:.6;font-size:12px;margin-right:8px}
 .msg{min-height:20px;opacity:.8}
</style></head><body>
<h1>My redaction terms</h1>
<p class="sub">Strings masked on <b>your</b> requests only. You can add; the
org list is set by security and always applies on top.</p>
<fieldset><legend>Add a term</legend>
 <label>Value <input id="v" placeholder="e.g. Project Moonshot"></label>
 <label>Kind
  <select id="k">
   <option value="always_tokenize">always_tokenize (reversible)</option>
   <option value="denylist">denylist (irreversible [REDACTED])</option>
  </select></label>
 <button onclick="add()">Add</button>
 <span class="msg" id="msg"></span>
</fieldset>
<fieldset><legend>My terms</legend><ul id="list"></ul></fieldset>
<script>
async function load(){
 const r=await fetch('/api/terms'); const d=await r.json();
 const t=d.terms||{}; const items=[];
 for(const k of Object.keys(t)) for(const v of t[k])
   items.push(`<li><span><span class=k>${k}</span>${v}</span>`+
     `<button onclick="del('${k}',${JSON.stringify(v)})">remove</button></li>`);
 document.getElementById('list').innerHTML=
   items.join('')||'<li>(none yet)</li>';
}
async function add(){
 const value=document.getElementById('v').value.trim();
 const kind=document.getElementById('k').value;
 if(!value){return}
 const r=await fetch('/api/terms',{method:'POST',
   headers:{'content-type':'application/json'},
   body:JSON.stringify({kind,value})});
 const d=await r.json();
 document.getElementById('msg').textContent=r.ok?'added':(d.error||'error');
 if(r.ok){document.getElementById('v').value='';load()}
}
async function del(kind,value){
 await fetch('/api/terms',{method:'DELETE',
   headers:{'content-type':'application/json'},
   body:JSON.stringify({kind,value})});
 load();
}
load();
</script></body></html>"""
