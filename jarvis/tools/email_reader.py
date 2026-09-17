"""Lectura IMAP de correos no leídos → bandeja MensajeEntrante."""

from __future__ import annotations

import email
import imaplib
import logging
from email.header import decode_header, make_header
from email.message import Message

from jarvis.config import get_settings
from jarvis.db import guardar_mensaje

logger = logging.getLogger(__name__)


def plataforma_desde_host(host: str) -> str:
    lowered = (host or "").lower()
    if "gmail" in lowered or "google" in lowered:
        return "gmail"
    return "webmail"


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def extraer_cuerpo(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition") or ""
            ).lower():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace").strip()
                except Exception:
                    return payload.decode("utf-8", errors="replace").strip()
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace").strip()
    except Exception:
        return payload.decode("utf-8", errors="replace").strip()


def resumen_correo(msg: Message) -> tuple[str, str]:
    remitente = _decode_header(msg.get("From"))
    asunto = _decode_header(msg.get("Subject"))
    cuerpo = extraer_cuerpo(msg)
    contenido = f"{asunto}\n{cuerpo}".strip() if asunto else cuerpo
    return remitente, contenido[:4000]


def revisar_correos_nuevos() -> list[int]:
    """Conecta por IMAP, lee UNSEEN y los guarda en la bandeja. Sin credenciales no hace nada."""
    settings = get_settings()
    host = (settings.email_host or "").strip()
    user = (settings.email_user or "").strip()
    password = settings.email_pass or ""
    if not host or not user or not password:
        logger.info("IMAP omitido: faltan EMAIL_HOST / EMAIL_USER / EMAIL_PASS.")
        return []

    plataforma = plataforma_desde_host(host)
    folder = settings.email_folder or "INBOX"
    saved_ids: list[int] = []
    client: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
    try:
        if settings.email_use_ssl:
            client = imaplib.IMAP4_SSL(host, settings.email_port)
        else:
            client = imaplib.IMAP4(host, settings.email_port)
        client.login(user, password)
        client.select(folder, readonly=False)
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            return []
        for raw_id in (data[0] or b"").split():
            fetch_status, fetched = client.fetch(raw_id, "(RFC822)")
            if fetch_status != "OK" or not fetched or not fetched[0]:
                continue
            blob = fetched[0][1]
            if not isinstance(blob, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(blob)
            remitente, contenido = resumen_correo(msg)
            if not contenido.strip():
                continue
            row = guardar_mensaje(plataforma, remitente, contenido)
            saved_ids.append(int(row.id))
            client.store(raw_id, "+FLAGS", "\\Seen")
    except Exception:
        logger.exception("Error al revisar correos IMAP.")
        return saved_ids
    finally:
        try:
            if client is not None:
                client.logout()
        except Exception:
            pass
    return saved_ids
