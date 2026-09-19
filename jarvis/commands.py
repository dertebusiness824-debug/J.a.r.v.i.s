"""Command Emission: comandos de sistema que el agente emite y un nodo local ejecuta.

Jarvis vive en Render y no puede crear archivos en el ordenador del usuario. En su
lugar, el agente estructura un comando estandarizado (`CREATE_PROJECT`), el backend
lo apila y `local_node.py`, corriendo en el PC, lo recoge por polling, crea el
proyecto y abre el IDE.

Ciclo de vida de un comando:

    pending ──GET /api/commands/pending──▶ delivered ──POST …/ack──▶ done | failed

Entregar marca el comando como `delivered`, así dos sondeos seguidos (o dos nodos)
no crean el mismo proyecto dos veces. La cola se guarda en la misma SQLite que la
bandeja de entrada: sobrevive a reinicios del proceso y a varios workers.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from jarvis.db import session_scope
from jarvis.db.models import ComandoSistema

CommandAction = Literal["CREATE_PROJECT"]
CommandStatus = Literal["pending", "delivered", "done", "failed"]
OpenWith = Literal["cursor", "code", "none"]

MAX_FILES = 60
MAX_TOTAL_BYTES = 1_500_000
MAX_PATH_PARTS = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_relative_path(raw: str, *, what: str = "path") -> str:
    """Devuelve una ruta POSIX relativa, sin `..`, sin raíz y sin unidades de Windows.

    El nodo local resuelve siempre bajo su carpeta raíz: cualquier intento de
    escaparse (`../`, `/etc`, `C:\\`) se rechaza aquí, antes de encolar nada.
    """
    text = str(raw or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if not text or text in {".", "/"}:
        raise ValueError(f"{what} vacío")
    if text.startswith("/") or text.startswith("~") or (len(text) > 1 and text[1] == ":"):
        raise ValueError(f"{what} debe ser relativo, no absoluto: {raw!r}")
    parts = [part for part in PurePosixPath(text).parts if part not in {"", "."}]
    if not parts:
        raise ValueError(f"{what} vacío")
    if any(part == ".." for part in parts):
        raise ValueError(f"{what} no puede salir de la carpeta del proyecto: {raw!r}")
    if any("\x00" in part for part in parts):
        raise ValueError(f"{what} contiene caracteres no válidos")
    if len(parts) > MAX_PATH_PARTS:
        raise ValueError(f"{what} demasiado profundo ({len(parts)} niveles)")
    return "/".join(parts)


class ProjectFile(BaseModel):
    name: str = Field(description="Ruta relativa dentro del proyecto, p.ej. 'index.html' o 'css/style.css'.")
    content: str = Field(default="", description="Contenido completo del archivo.")

    @field_validator("name")
    @classmethod
    def _relative_name(cls, value: str) -> str:
        return normalize_relative_path(value, what="name")


class SystemCommand(BaseModel):
    """El JSON estandarizado que viaja del agente al nodo local."""

    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    action: CommandAction = "CREATE_PROJECT"
    path: str = Field(description="Carpeta del proyecto, relativa a la raíz del nodo local, p.ej. './taller-web'.")
    files: list[ProjectFile] = Field(default_factory=list)
    open_with: OpenWith = "cursor"
    description: str = Field(default="", description="Qué es el proyecto, en una frase (para el log del nodo).")

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return "./" + normalize_relative_path(value)

    @model_validator(mode="after")
    def _limits(self) -> "SystemCommand":
        if len(self.files) > MAX_FILES:
            raise ValueError(f"Demasiados archivos ({len(self.files)} > {MAX_FILES})")
        seen: set[str] = set()
        for item in self.files:
            if item.name in seen:
                raise ValueError(f"Archivo repetido: {item.name}")
            seen.add(item.name)
        total = sum(len(item.content.encode("utf-8")) for item in self.files)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"Proyecto demasiado grande ({total} bytes > {MAX_TOTAL_BYTES})")
        return self

    def wire(self) -> dict[str, Any]:
        """Lo que se entrega al nodo: exactamente el contrato del patrón."""
        return {
            "id": self.id,
            "action": self.action,
            "path": self.path,
            "files": [{"name": f.name, "content": f.content} for f in self.files],
            "open_with": self.open_with,
            "description": self.description,
        }


class CommandRecord(BaseModel):
    """Un comando con su estado en la cola (lo que ven el HUD y la API de historial)."""

    id: str
    action: str
    status: str
    origin: str = ""
    node: str = ""
    result: str = ""
    created_at: str | None = None
    delivered_at: str | None = None
    finished_at: str | None = None
    command: dict[str, Any] = Field(default_factory=dict)


def _record(row: ComandoSistema, *, with_payload: bool = True) -> CommandRecord:
    try:
        payload = json.loads(row.payload or "{}")
    except ValueError:
        payload = {}
    return CommandRecord(
        id=row.id,
        action=row.action,
        status=row.status,
        origin=row.origin or "",
        node=row.node or "",
        result=row.result or "",
        created_at=_iso(row.created_at),
        delivered_at=_iso(row.delivered_at),
        finished_at=_iso(row.finished_at),
        command=payload if with_payload else {},
    )


def enqueue_command(command: SystemCommand, *, origin: str = "agent") -> CommandRecord:
    with session_scope() as session:
        row = ComandoSistema(
            id=command.id,
            action=command.action,
            payload=json.dumps(command.wire(), ensure_ascii=False),
            status="pending",
            origin=origin[:64],
        )
        session.add(row)
        session.flush()
        return _record(row)


def pending_commands(*, node: str = "", claim: bool = True, limit: int = 20) -> list[dict[str, Any]]:
    """Comandos pendientes, más antiguos primero. Con `claim` quedan como entregados."""
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(ComandoSistema)
                .where(ComandoSistema.status == "pending")
                .order_by(ComandoSistema.created_at.asc())
                .limit(max(1, min(limit, 100)))
            ).all()
        )
        delivered: list[dict[str, Any]] = []
        now = _utc_now()
        for row in rows:
            if claim:
                row.status = "delivered"
                row.delivered_at = now
                row.node = (node or "")[:64]
            delivered.append(_record(row).command)
        return delivered


def ack_command(command_id: str, *, ok: bool, detail: str = "", node: str = "") -> CommandRecord | None:
    with session_scope() as session:
        row = session.get(ComandoSistema, command_id)
        if row is None:
            return None
        row.status = "done" if ok else "failed"
        row.result = (detail or "")[:4000]
        row.finished_at = _utc_now()
        if node:
            row.node = node[:64]
        return _record(row)


def list_commands(*, limit: int = 50, status: str | None = None, with_payload: bool = False) -> list[CommandRecord]:
    with session_scope() as session:
        query = select(ComandoSistema).order_by(ComandoSistema.created_at.desc()).limit(max(1, min(limit, 200)))
        if status:
            query = query.where(ComandoSistema.status == status)
        return [_record(row, with_payload=with_payload) for row in session.scalars(query).all()]


def get_command(command_id: str) -> CommandRecord | None:
    with session_scope() as session:
        row = session.get(ComandoSistema, command_id)
        return _record(row) if row is not None else None


def commands_summary() -> dict[str, int]:
    records = list_commands(limit=200)
    counts = {"pending": 0, "delivered": 0, "done": 0, "failed": 0}
    for item in records:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts
