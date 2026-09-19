"""API de Command Emission: la cola de comandos que consume `local_node.py`.

    GET  /api/commands/pending      → comandos pendientes (y quedan como entregados)
    POST /api/commands/{id}/ack     → el nodo confirma done / failed
    GET  /api/commands              → historial reciente (HUD, depuración)
    GET  /api/commands/{id}         → un comando con su payload
    POST /api/commands              → encolar a mano (probar el nodo sin el LLM)

Seguridad: con `JARVIS_NODE_TOKEN` configurado, todas las rutas exigen el token en
`X-Jarvis-Node-Token` o `Authorization: Bearer …`. Sin token configurado la API
queda abierta (útil en local), pero en Render cualquiera con la URL podría
encolar archivos hacia el PC del usuario: el nodo avisa de ello al arrancar.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from jarvis.commands import (
    CommandRecord,
    SystemCommand,
    ack_command,
    commands_summary,
    enqueue_command,
    get_command,
    list_commands,
    pending_commands,
)
from jarvis.config import get_settings

router = APIRouter(prefix="/api/commands", tags=["commands"])

TOKEN_HEADER = "X-Jarvis-Node-Token"


def require_node_token(
    x_jarvis_node_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> None:
    expected = (get_settings().jarvis_node_token or "").strip()
    if not expected:
        return
    provided = (x_jarvis_node_token or "").strip()
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Token del nodo local inválido o ausente.")


class AckRequest(BaseModel):
    ok: bool = True
    detail: str = Field(default="", max_length=4000)
    node: str = Field(default="", max_length=64)


class PendingResponse(BaseModel):
    commands: list[dict] = Field(default_factory=list)
    count: int = 0
    poll_seconds: int = 2


class CommandsListResponse(BaseModel):
    commands: list[CommandRecord] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    token_required: bool = False


@router.get("/pending", response_model=PendingResponse)
def commands_pending(
    node: str = Query(default="", max_length=64, description="Nombre del nodo local que sondea."),
    claim: bool = Query(default=True, description="Marcar como entregados (false = solo mirar)."),
    limit: int = Query(default=20, ge=1, le=100),
    x_jarvis_node_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> PendingResponse:
    require_node_token(x_jarvis_node_token, authorization)
    items = pending_commands(node=node, claim=claim, limit=limit)
    return PendingResponse(commands=items, count=len(items), poll_seconds=2)


@router.get("", response_model=CommandsListResponse)
def commands_list(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, pattern="^(pending|delivered|done|failed)$"),
    x_jarvis_node_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> CommandsListResponse:
    require_node_token(x_jarvis_node_token, authorization)
    return CommandsListResponse(
        commands=list_commands(limit=limit, status=status),
        summary=commands_summary(),
        token_required=bool((get_settings().jarvis_node_token or "").strip()),
    )


@router.post("", response_model=CommandRecord, status_code=201)
def commands_create(
    payload: SystemCommand,
    x_jarvis_node_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> CommandRecord:
    require_node_token(x_jarvis_node_token, authorization)
    return enqueue_command(payload, origin="api")


@router.get("/{command_id}", response_model=CommandRecord)
def commands_get(
    command_id: str,
    x_jarvis_node_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> CommandRecord:
    require_node_token(x_jarvis_node_token, authorization)
    record = get_command(command_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Comando no encontrado.")
    return record


@router.post("/{command_id}/ack", response_model=CommandRecord)
def commands_ack(
    command_id: str,
    payload: AckRequest,
    x_jarvis_node_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None),
) -> CommandRecord:
    require_node_token(x_jarvis_node_token, authorization)
    record = ack_command(command_id, ok=payload.ok, detail=payload.detail, node=payload.node)
    if record is None:
        raise HTTPException(status_code=404, detail="Comando no encontrado.")
    return record
