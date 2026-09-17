"""Punto de entrada ASGI: `uvicorn jarvis.api.main:app --host 0.0.0.0 --port $PORT`.

CORS (allow_origins/methods/headers = *) se aplica en `create_app()` sobre esta instancia,
para que Vapi y los webhooks de telefonía no encuentren bloqueos de origen.

Webhooks de WhatsApp Web (puente Node en :3000): `POST /webhooks/whatsapp-local`.
"""

from jarvis.api.app import app
from jarvis.api.vapi_routes import router as vapi_router

__all__ = ["app", "vapi_router"]
