"""Eventos de servidor Vapi: assistant-request → briefing de bandeja."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response

from jarvis.config import get_settings
from jarvis.db import briefing_inicial

DEFAULT_PUBLIC_URL = "https://j-a-r-v-i-s-yghr.onrender.com"


def custom_llm_url() -> str:
    settings = get_settings()
    base = (settings.jarvis_public_url or DEFAULT_PUBLIC_URL).rstrip("/")
    return f"{base}/webhooks/vapi-llm"


def custom_llm_model(url: str | None = None) -> dict[str, Any]:
    """Custom LLM que Vapi debe usar: base URL (sin /chat/completions) + SSE estable."""
    return {
        "provider": "custom-llm",
        "url": url or custom_llm_url(),
        "model": "jarvis-supervisor",
        "metadataSendMode": "off",
        "timeoutSeconds": 90,
    }


def assistant_request_payload(spoken: str) -> dict[str, Any]:
    return {
        "assistant": {
            "firstMessage": spoken,
            "model": custom_llm_model(),
        }
    }


def _already_registered(app: FastAPI) -> bool:
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path == "/api/jarvis/vapi-events" and "POST" in methods:
            return True
    return False


def attach_vapi_events(app: FastAPI) -> None:
    if _already_registered(app):
        return

    @app.post("/api/jarvis/vapi-events", tags=["vapi"], response_model=None)
    async def vapi_server_events(request: Request):
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=200)
        if not isinstance(data, dict):
            return Response(status_code=200)
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        tipo_mensaje = message.get("type") or data.get("type")
        if tipo_mensaje != "assistant-request":
            return Response(status_code=200)
        briefing = briefing_inicial(marcar=True)
        spoken = str(briefing.get("firstMessage") or briefing.get("spoken") or "")
        return assistant_request_payload(spoken)
