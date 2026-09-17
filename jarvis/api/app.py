"""Aplicación FastAPI de Jarvis v2.0."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from jarvis import __version__
from jarvis.agent_core import extract_answer
from jarvis.api.schemas import HealthResponse, InvokeRequest, InvokeResponse, TaskOut, ToolResultOut
from jarvis.config import get_settings
from jarvis.api.vapi_routes import router as vapi_router
from jarvis.integrations.messaging import WhatsAppClient
from jarvis.integrations.zadarma import INBOUND_EVENTS, ZadarmaClient
from jarvis.supervisor import compile_supervisor_graph, graph_mermaid, run_jarvis

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.graph = compile_supervisor_graph()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jarvis v2.0",
        description="Sistema multi-agente (Supervisor + especialistas) con LangGraph.",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(vapi_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="UI no encontrada")
        return FileResponse(page)

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        settings = get_settings()
        return HealthResponse(
            status="ok",
            version=__version__,
            offline=settings.offline,
            agents=["supervisor", "code_agent", "comms_agent", "shop_agent", "general"],
        )

    @app.get("/graph", tags=["ops"])
    def graph() -> PlainTextResponse:
        return PlainTextResponse(graph_mermaid(), media_type="text/plain")

    @app.post("/invoke", response_model=InvokeResponse, tags=["agent"])
    def invoke(payload: InvokeRequest) -> InvokeResponse:
        settings = get_settings()
        result = run_jarvis(payload.message, session_id=payload.session_id)
        return InvokeResponse(
            answer=extract_answer(result),
            agent=str(result.get("active_agent") or result.get("next_agent") or "supervisor"),
            visited_agents=list(result.get("visited_agents") or []),
            plan=[TaskOut(**t) for t in (result.get("plan") or [])],
            tool_results=[ToolResultOut(**r) for r in (result.get("tool_results") or [])],
            retrieved_context=result.get("retrieved_context") or "",
            error=result.get("error"),
            offline=settings.offline,
        )

    @app.get("/webhooks/whatsapp", tags=["webhooks"])
    def whatsapp_verify(
        hub_mode: str | None = Query(default=None, alias="hub.mode"),
        hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> Response:
        challenge = WhatsAppClient().verify_webhook(
            hub_mode or "", hub_verify_token or "", hub_challenge or ""
        )
        if challenge is None:
            raise HTTPException(status_code=403, detail="Verificación WhatsApp rechazada")
        return PlainTextResponse(challenge)

    @app.post("/webhooks/whatsapp", tags=["webhooks"])
    async def whatsapp_inbound(request: Request) -> dict:
        payload = await request.json()
        inbound = WhatsAppClient().extract_inbound(payload)
        replies = []
        for msg in inbound:
            result = run_jarvis(msg["body"], session_id=f"wa:{msg.get('from') or 'unknown'}")
            answer = extract_answer(result)
            if msg.get("from"):
                WhatsAppClient().send_text(to=msg["from"], body=answer)
            replies.append({"from": msg.get("from"), "answer": answer})
        return {"ok": True, "processed": len(replies), "replies": replies}

    @app.post("/webhooks/whatsapp-local", tags=["webhooks"])
    async def whatsapp_local_inbound(request: Request) -> dict:
        """Inbound del puente whatsapp-web.js: {from, body} → Supervisor LangGraph."""
        payload = await request.json()
        sender = str(payload.get("from") or "")
        body = str(payload.get("body") or "")
        if not body.strip():
            return {"ok": True, "processed": 0, "channel": "whatsapp-web", "replies": []}
        result = await asyncio.to_thread(run_jarvis, body, session_id=f"wa:{sender or 'unknown'}")
        answer = extract_answer(result)
        if sender:
            await asyncio.to_thread(WhatsAppClient().send_text, sender, answer)
        return {
            "ok": True,
            "processed": 1,
            "channel": "whatsapp-web",
            "from": sender,
            "answer": answer,
            "replies": [{"from": sender, "answer": answer}],
        }

    @app.get("/webhooks/zadarma", tags=["webhooks"])
    async def zadarma_verify(zd_echo: str | None = Query(default=None)) -> Response:
        """Verificación de webhook PBX: Zadarma envía GET ?zd_echo=... y espera el eco."""
        if zd_echo:
            return PlainTextResponse(zd_echo)
        return PlainTextResponse("ok")

    @app.post("/webhooks/zadarma", tags=["webhooks"])
    async def zadarma_inbound(request: Request) -> dict:
        """Placeholder de centralita: registra NOTIFY_* para el Supervisor (llamadas del taller)."""
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            raw = await request.json()
            payload = dict(raw) if isinstance(raw, dict) else {}
        else:
            form = await request.form()
            payload = {str(k): str(v) for k, v in form.items()}

        client = ZadarmaClient()
        signature = (
            request.headers.get("Signature")
            or request.headers.get("signature")
            or str(payload.get("signature") or "")
        )
        if not client.verify_webhook_signature(payload, signature or None):
            raise HTTPException(status_code=403, detail="Firma Zadarma inválida")

        event = client.extract_event(payload)
        recorded = False
        if event["event"] in INBOUND_EVENTS:
            client.record_inbound(event)
            recorded = True
        return {"ok": True, "provider": "zadarma", "recorded": recorded, "event": event}

    return app


app = create_app()
