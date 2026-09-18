"""Sesión SQLite y bandeja de mensajes (WhatsApp, Gmail, webmail)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from jarvis.config import get_settings
from jarvis.db.models import Base, MensajeEntrante

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None

_PLATFORM_SPOKEN = {
    "whatsapp": "WhatsApp",
    "gmail": "Gmail",
    "webmail": "correo web",
}


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    init_db()
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def guardar_mensaje(plataforma: str, remitente: str, contenido: str) -> MensajeEntrante:
    with session_scope() as session:
        row = MensajeEntrante(
            plataforma=(plataforma or "whatsapp").strip().lower(),
            remitente=remitente or "",
            contenido=contenido or "",
            leido=False,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def listar_no_leidos() -> list[MensajeEntrante]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(MensajeEntrante)
                .where(MensajeEntrante.leido.is_(False))
                .order_by(MensajeEntrante.fecha_recepcion.asc())
            ).all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def marcar_leidos(ids: list[int]) -> None:
    if not ids:
        return
    with session_scope() as session:
        session.execute(
            update(MensajeEntrante).where(MensajeEntrante.id.in_(ids)).values(leido=True)
        )


def _join_es(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " y " + parts[-1]


def formatear_briefing(mensajes: list[MensajeEntrante]) -> str:
    if not mensajes:
        return "Sistemas en línea. No hay mensajes pendientes, maestro."
    total = len(mensajes)
    cantidad = "1 mensaje" if total == 1 else f"{total} mensajes"
    counts = Counter(m.plataforma for m in mensajes)
    detalle_parts: list[str] = []
    for plataforma, n in counts.items():
        label = _PLATFORM_SPOKEN.get(plataforma, plataforma)
        detalle_parts.append(f"{n} de {label}")
    if len(counts) == 1:
        label = _PLATFORM_SPOKEN.get(next(iter(counts)), next(iter(counts)))
        return (
            f"Sistemas en línea. Maestro, tenemos {cantidad} de {label} "
            "sin responder esperando sus órdenes."
        )
    detalle = _join_es(detalle_parts)
    return (
        f"Sistemas en línea. Maestro, tenemos {cantidad} ({detalle}) "
        "sin responder esperando sus órdenes."
    )


def inbox_status() -> dict[str, Any]:
    """Contadores de bandeja no leída para el HUD (no marca como leídos)."""
    pendientes = listar_no_leidos()
    counts = Counter(m.plataforma for m in pendientes)
    gmail = int(counts.get("gmail") or 0)
    webmail = int(counts.get("webmail") or 0)
    whatsapp = int(counts.get("whatsapp") or 0)
    correo = gmail + webmail
    from jarvis.hud_live import is_researching

    return {
        "whatsapp": whatsapp,
        "correo": correo,
        "gmail": gmail,
        "webmail": webmail,
        "total": len(pendientes),
        "pending": len(pendientes),
        "researching": is_researching(),
    }


def briefing_inicial(*, marcar: bool = True) -> dict[str, Any]:
    pendientes = listar_no_leidos()
    spoken = formatear_briefing(pendientes)
    ids = [int(m.id) for m in pendientes if m.id is not None]
    by_platform = dict(Counter(m.plataforma for m in pendientes))
    if marcar and ids:
        marcar_leidos(ids)
    return {
        "firstMessage": spoken,
        "message": spoken,
        "spoken": spoken,
        "pending": len(pendientes),
        "by_platform": by_platform,
        "ids": ids,
    }
