"""Webhook local WhatsApp Web: guarda en la bandeja, sin push ni LangGraph."""

from __future__ import annotations

from fastapi import FastAPI, Request

from jarvis.db import guardar_mensaje

MASTER_NUMBER = "34605686509"


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
    async def whatsapp_webhook(request: Request) -> dict:
        data = await request.json()
        remitente = str(data.get("from") or "")
        mensaje = str(data.get("body") or "")
        if not mensaje.strip():
            return {"status": "saved_to_inbox", "saved": False}
        row = guardar_mensaje("whatsapp", remitente, mensaje)
        return {
            "status": "saved_to_inbox",
            "saved": True,
            "id": row.id,
            "plataforma": "whatsapp",
        }
