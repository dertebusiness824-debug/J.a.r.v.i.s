"""Webhook local WhatsApp Web: patrón secretaria (Human-in-the-Loop)."""

from __future__ import annotations

import logging

import requests
from fastapi import BackgroundTasks, FastAPI, Request

from jarvis.agent_core import extract_answer
from jarvis.integrations.messaging import WhatsAppClient, to_whatsapp_id
from jarvis.supervisor import run_jarvis

logger = logging.getLogger(__name__)

MASTER_NUMBER = "34605686509"
BRIDGE_SEND_URL = "http://127.0.0.1:3000/send"


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def is_master_number(remitente: str) -> bool:
    """True si el JID/E.164 coincide con MASTER_NUMBER (34605686509 o …@c.us)."""
    incoming = _digits(remitente)
    return bool(incoming) and incoming == _digits(MASTER_NUMBER)


def notificar_al_jefe(remitente: str, mensaje: str) -> None:
    """Alerta al número maestro. No responde al tercero ni llama a LangGraph."""
    alerta = (
        "📬 Secretaria Jarvis\n"
        "Un tercero te ha escrito. No he contestado.\n\n"
        f"De: {remitente}\n"
        f"Mensaje: {mensaje}\n\n"
        "Si quieres que actúe, respóndeme aquí con la orden."
    )
    payload = {"to": to_whatsapp_id(MASTER_NUMBER), "message": alerta}
    try:
        response = requests.post(BRIDGE_SEND_URL, json=payload, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("No se pudo notificar al jefe: %s", exc)


def process_whatsapp_message(sender: str, body: str) -> None:
    """Orden del jefe: Supervisor LangGraph y respuesta al MASTER_NUMBER."""
    result = run_jarvis(body, session_id=f"wa:{sender or 'unknown'}")
    answer = extract_answer(result)
    if answer:
        WhatsAppClient().send_text(to=sender or MASTER_NUMBER, body=answer)


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
        mensaje = str(data.get("body") or "")

        if not is_master_number(remitente):
            background_tasks.add_task(notificar_al_jefe, remitente, mensaje)
            return {"status": "intercepted_and_notified"}

        if not mensaje.strip():
            return {"status": "processing_master_command", "queued": False}
        background_tasks.add_task(process_whatsapp_message, remitente, mensaje)
        return {"status": "processing_master_command"}
