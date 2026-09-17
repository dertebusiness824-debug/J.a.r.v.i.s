"""Aplicación FastAPI de Jarvis v2.0."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from jarvis import __version__
from jarvis.agent_core import extract_answer
from jarvis.api.schemas import HealthResponse, InvokeRequest, InvokeResponse, TaskOut, ToolResultOut
from jarvis.config import get_settings
from jarvis.api.vapi_routes import router as vapi_router
from jarvis.integrations.messaging import TwilioClient, WhatsAppClient
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

    @app.post("/webhooks/twilio", tags=["webhooks"])
    async def twilio_inbound(request: Request) -> Response:
        form = await request.form()
        body = str(form.get("Body") or "")
        sender = str(form.get("From") or "")
        if not body:
            return Response(content="<Response></Response>", media_type="application/xml")
        result = run_jarvis(body, session_id=f"twilio:{sender or 'unknown'}")
        answer = extract_answer(result)
        if sender:
            TwilioClient().send_sms(to=sender, body=answer)
        xml = f"<Response><Message>{_xml_escape(answer)}</Message></Response>"
        return Response(content=xml, media_type="application/xml")

    return app


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


app = create_app()
