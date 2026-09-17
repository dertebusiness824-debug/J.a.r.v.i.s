"""Webhook local del puente whatsapp-web.js → Supervisor LangGraph."""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Request

from jarvis.agent_core import extract_answer
from jarvis.integrations.messaging import WhatsAppClient
from jarvis.supervisor import run_jarvis

logger = logging.getLogger(__name__)


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
    async def whatsapp_local_inbound(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict:
        payload = await request.json()
        sender = str(payload.get("from") or "")
        body = str(payload.get("body") or "")
        if not body.strip():
            return {"ok": True, "queued": False, "processed": 0, "channel": "whatsapp-web"}
        background_tasks.add_task(process_whatsapp_message, sender, body)
        return {
            "ok": True,
            "queued": True,
            "channel": "whatsapp-web",
            "from": sender,
        }
