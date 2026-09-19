"""Modelo SQLAlchemy de la bandeja de entrada universal."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class MensajeEntrante(Base):
    __tablename__ = "mensajes_entrantes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plataforma: Mapped[str] = mapped_column(String(32), index=True)
    remitente: Mapped[str] = mapped_column(String(255), default="")
    contenido: Mapped[str] = mapped_column(Text, default="")
    leido: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fecha_recepcion: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)


class ComandoSistema(Base):
    """Comando emitido por el agente para que lo ejecute un nodo local (Command Emission).

    El backend en Render no toca el disco del usuario: apila aquí el JSON del comando
    y `local_node.py` lo recoge por polling, lo ejecuta y confirma el resultado.
    """

    __tablename__ = "comandos_sistema"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    origin: Mapped[str] = mapped_column(String(64), default="")
    node: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
