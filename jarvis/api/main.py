"""Punto de entrada ASGI: `uvicorn jarvis.api.main:app --host 0.0.0.0 --port 8000`.

CORS (allow_origins/methods/headers = *) se aplica en `create_app()` sobre esta instancia.
WhatsApp local: POST /webhooks/whatsapp-local guarda en la bandeja SQLite.
Vapi Server URL: POST /api/jarvis/vapi-events (evento assistant-request → firstMessage).
"""

from fastapi import Request

from jarvis.api.app import app
from jarvis.api.vapi_events import attach_vapi_events
from jarvis.api.vapi_routes import router as vapi_router
from jarvis.api.whatsapp_local import MASTER_NUMBER, attach_whatsapp_local_webhook
from jarvis.supervisor import run_jarvis

attach_whatsapp_local_webhook(app)
attach_vapi_events(app)

__all__ = [
    "app",
    "vapi_router",
    "Request",
    "MASTER_NUMBER",
    "run_jarvis",
]
