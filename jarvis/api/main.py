"""Punto de entrada ASGI: `uvicorn jarvis.api.main:app --host 0.0.0.0 --port 8000`.

CORS (allow_origins/methods/headers = *) se aplica en `create_app()` sobre esta instancia.
Patrón secretaria: terceros → notificación al jefe; MASTER_NUMBER → Supervisor LangGraph.
"""

from fastapi import BackgroundTasks, Request
import requests

from jarvis.api.app import app
from jarvis.api.vapi_routes import router as vapi_router
from jarvis.api.whatsapp_local import (
    MASTER_NUMBER,
    attach_whatsapp_local_webhook,
    notificar_al_jefe,
    process_whatsapp_message,
)
from jarvis.supervisor import run_jarvis

# Garantiza el webhook en la instancia que carga uvicorn (`jarvis.api.main:app`).
attach_whatsapp_local_webhook(app)

__all__ = [
    "app",
    "vapi_router",
    "Request",
    "BackgroundTasks",
    "requests",
    "MASTER_NUMBER",
    "notificar_al_jefe",
    "run_jarvis",
    "process_whatsapp_message",
]
