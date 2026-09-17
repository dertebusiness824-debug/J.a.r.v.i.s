"""Webhook local del puente whatsapp-web.js → Supervisor LangGraph."""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Request

from jarvis.agent_core import extract_answer
from jarvis.config import get_settings
from jarvis.integrations.messaging import WhatsAppClient
from jarvis.supervisor import run_jarvis

logger = logging.getLogger(__name__)

# Número propio (internacional, sin +). WhatsApp Web manda `34605686509@c.us`.
MI_NUMERO = "34605686509"


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def is_owner_number(remitente: str) -> bool:
    allowed = _digits(get_settings().whatsapp_allowed_number or MI_NUMERO)
    incoming = _digits(remitente)
    return bool(allowed and incoming) and incoming == allowed


def process_whatsapp_message(sender: str, body: str) -> None:
    """Invoca al Supervisor (run_jarvis → graph.invoke) y responde por el puente :3000."""
    result = run_jarvis(body, session_id=f"wa:{sender or 'unknown'}")
    answer = extract_answer(result)
    if sender and answer:
        WhatsAppClient().send_text(to=sender, body=answer)


def _already_registered(app: FastAPI) -> bool:
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path == "/webhooks/whatsapp-local" and "POST" in methods:
            return True
    return False


def attach_whatsapp_local_webhook(app: FastAPI) -> None:
    """Idempotente: uvicorn carga `jarvis.api.main:app`; los tests usan `create_app()`."""
    if _already_registered(app):
        return

    @app.get("/webhooks/whatsapp-local", tags=["webhooks"])
    async def whatsapp_local_ping() -> dict:
        return {"ok": True, "endpoint": "whatsapp-local", "method": "POST"}

    @app.post("/webhooks/whatsapp-local", tags=["webhooks"])
    async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
        data = await request.json()
        remitente = str(data.get("from") or "")
        if not is_owner_number(remitente):
            logger.info("Mensaje ignorado del número: %s", remitente)
            return {"status": "ignored"}

        mensaje = str(data.get("body") or "")
        if not mensaje.strip():
            return {"status": "received", "queued": False}
        background_tasks.add_task(process_whatsapp_message, remitente, mensaje)
        return {"status": "received", "queued": True, "from": remitente}
