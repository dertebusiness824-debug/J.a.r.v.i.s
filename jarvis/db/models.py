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
