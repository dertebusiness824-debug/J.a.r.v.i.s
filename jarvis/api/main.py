"""Punto de entrada ASGI: `uvicorn jarvis.api.main:app --reload`.

El router de Vapi (`/webhooks/vapi-llm`) se registra en `create_app()`.
"""

from jarvis.api.app import app
from jarvis.api.vapi_routes import router as vapi_router

__all__ = ["app", "vapi_router"]
