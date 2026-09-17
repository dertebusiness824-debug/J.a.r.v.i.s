"""Punto de entrada ASGI: `uvicorn jarvis.api.main:app --reload`."""

from jarvis.api.app import app

__all__ = ["app"]
