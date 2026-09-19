"""Autenticación de webhooks Vapi: Bearer, X-Vapi-Secret y Standard Webhooks.

`VAPI_WEBHOOK_SECRET` cubre dos usos distintos:

- Secreto plano: el Custom LLM y el Server URL envían `Authorization: Bearer …`
  o `X-Vapi-Secret`. Sin esa cabecera, 401.
- Secreto `whsec_…` (Standard Webhooks / Svix): Vapi lo usa para firmar el
  Server URL (`/api/jarvis/vapi-events`). El Custom LLM no manda esa firma, así
  que no se exige Bearer solo por tener el secreto de firma configurado.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import Request

from jarvis.config import get_settings

SIGNING_PREFIX = "whsec_"
MAX_SKEW_SECONDS = 300


def configured_webhook_secret() -> str | None:
    secret = (get_settings().vapi_webhook_secret or "").strip()
    return secret or None


def is_signing_secret(secret: str) -> bool:
    return secret.startswith(SIGNING_PREFIX)


def signing_key(secret: str) -> bytes:
    raw = secret[len(SIGNING_PREFIX) :] if secret.startswith(SIGNING_PREFIX) else secret
    try:
        return base64.b64decode(raw)
    except Exception:
        return raw.encode("utf-8")


def standard_webhook_signature(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    payload = body.decode("utf-8")
    signed = f"{msg_id}.{timestamp}.{payload}".encode("utf-8")
    digest = hmac.new(signing_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_standard_webhooks(
    secret: str,
    *,
    msg_id: str,
    timestamp: str,
    signatures: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    if not (secret and msg_id and timestamp and signatures):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    clock = time.time() if now is None else now
    if abs(clock - ts) > MAX_SKEW_SECONDS:
        return False
    try:
        expected = standard_webhook_signature(secret, msg_id, timestamp, body)
    except UnicodeDecodeError:
        return False
    expected_sig = expected.partition(",")[2]
    for part in signatures.split():
        version, sep, sig = part.partition(",")
        if sep and version == "v1" and hmac.compare_digest(sig, expected_sig):
            return True
    return False


def _header_matches(request: Request, secret: str) -> bool:
    auth = (request.headers.get("authorization") or "").strip()
    if auth and (
        hmac.compare_digest(auth, f"Bearer {secret}") or hmac.compare_digest(auth, secret)
    ):
        return True
    offered = (request.headers.get("x-vapi-secret") or "").strip()
    return bool(offered) and hmac.compare_digest(offered, secret)


def _offered_shared_secret(request: Request) -> bool:
    auth = (request.headers.get("authorization") or "").strip()
    header = (request.headers.get("x-vapi-secret") or "").strip()
    return bool(auth or header)


def _standard_headers(request: Request) -> tuple[str, str, str]:
    return (
        (request.headers.get("webhook-id") or "").strip(),
        (request.headers.get("webhook-timestamp") or "").strip(),
        (request.headers.get("webhook-signature") or "").strip(),
    )


def authorized_vapi_request(
    request: Request,
    *,
    body: bytes = b"",
    strict: bool = False,
) -> bool:
    """True si la petición puede pasar.

    `strict=True` (Server URL): con secreto configurado hace falta una prueba
    válida (Bearer, X-Vapi-Secret o HMAC Standard Webhooks).

    `strict=False` (Custom LLM): un `whsec_` sin cabeceras de auth no 401,
    porque Vapi no firma esas llamadas. Un Bearer / HMAC incorrecto sí se rechaza.
    """
    secret = configured_webhook_secret()
    if not secret:
        return True
    if _header_matches(request, secret):
        return True
    msg_id, timestamp, signatures = _standard_headers(request)
    if msg_id or timestamp or signatures:
        return verify_standard_webhooks(
            secret,
            msg_id=msg_id,
            timestamp=timestamp,
            signatures=signatures,
            body=body,
        )
    if _offered_shared_secret(request):
        return False
    if strict:
        return False
    return is_signing_secret(secret)
