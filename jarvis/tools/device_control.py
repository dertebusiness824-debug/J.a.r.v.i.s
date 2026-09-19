"""Tool del Supervisor: emite una orden JSON por el WebSocket al PC del usuario."""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from jarvis.device_bridge import manager

TOOL_NAME = "execute_local_command"
ALLOWED_COMMANDS = frozenset({"OPEN_APP", "SYSTEM_ALERT"})
MAX_PAYLOAD_KEYS = 16
MAX_VALUE_CHARS = 500


class ExecuteLocalCommandInput(BaseModel):
    command_type: str = Field(
        description="Tipo de orden para el PC: OPEN_APP (calculadora o bloc de notas) o SYSTEM_ALERT."
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Datos de la orden. OPEN_APP: {app: calculator|notepad}. SYSTEM_ALERT: {message: texto}.",
    )


def _sanitize_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    clean: dict[str, Any] = {}
    for index, (key, value) in enumerate(payload.items()):
        if index >= MAX_PAYLOAD_KEYS:
            break
        name = str(key)[:64]
        if isinstance(value, (int, float, bool)) or value is None:
            clean[name] = value
        else:
            clean[name] = str(value)[:MAX_VALUE_CHARS]
    return clean


def _normalize_command(command_type: str) -> str:
    return "".join(ch for ch in (command_type or "").strip().upper() if ch.isalnum() or ch == "_")


@tool(TOOL_NAME, args_schema=ExecuteLocalCommandInput)
def execute_local_command(command_type: str, payload: dict | None = None) -> str:
    """Envía una orden en tiempo real al PC del usuario por WebSocket (local_client.py).

    No ejecuta nada en Render. El cliente local tiene que estar conectado a
    /ws/device-control. Tipos iniciales: OPEN_APP y SYSTEM_ALERT.
    """
    kind = _normalize_command(command_type)
    if kind not in ALLOWED_COMMANDS:
        return (
            f"Tipo de orden no soportado: {command_type!r}. "
            "Usa OPEN_APP o SYSTEM_ALERT."
        )
    body = _sanitize_payload(payload)
    if kind == "OPEN_APP" and not str(body.get("app") or "").strip():
        return "OPEN_APP necesita payload.app (calculator o notepad)."
    message = {
        "command_type": kind,
        "payload": body,
        "id": uuid.uuid4().hex[:12],
    }
    sent = manager.emit(message)
    if sent <= 0:
        return (
            "No hay ningún ordenador conectado al puente WebSocket. "
            "Arranca local_client.py en su PC."
        )
    return f"Orden {kind} enviada a su equipo ({sent} conexión/es)."
