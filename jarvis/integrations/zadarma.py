"""Cliente REST de Zadarma PBX: autenticación HMAC (Key + Secret) y webhooks de centralita."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections import deque
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvis.config import get_settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.zadarma.com"
SMS_METHOD = "/v1/sms/send/"
INBOUND_EVENTS = frozenset(
    {
        "NOTIFY_START",
        "NOTIFY_INTERNAL",
        "NOTIFY_ANSWER",
        "NOTIFY_END",
        "NOTIFY_OUT_START",
        "NOTIFY_OUT_END",
        "NOTIFY_RECORD",
    }
)


def encode_params(params: dict[str, Any]) -> str:
    """Query string RFC1738 (equivalente a PHP http_build_query + PHP_QUERY_RFC1738)."""
    items = sorted((str(k), "" if v is None else str(v)) for k, v in params.items())
    return urlencode(items, doseq=True)


def api_signature(method: str, params: dict[str, Any], secret: str) -> str:
    """Firma oficial: base64(hmac_sha1_hex(method + paramsStr + md5(paramsStr), secret))."""
    params_str = encode_params(params)
    payload = f"{method}{params_str}{hashlib.md5(params_str.encode('utf-8')).hexdigest()}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).hexdigest()
    return base64.b64encode(digest.encode("utf-8")).decode("ascii")


def authorization_header(key: str, secret: str, method: str, params: dict[str, Any]) -> str:
    return f"{key}:{api_signature(method, params, secret)}"


def webhook_signature(caller_id: str, called_did: str, call_start: str, secret: str) -> str:
    """Firma de webhooks PBX: base64(hmac_sha1_hex(caller_id + called_did + call_start, secret))."""
    payload = f"{caller_id}{called_did}{call_start}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).hexdigest()
    return base64.b64encode(digest.encode("utf-8")).decode("ascii")


class ZadarmaClient:
    """SMS y eventos de centralita. Sin Key/Secret opera en modo demo."""

    inbound_calls: deque[dict[str, Any]] = deque(maxlen=200)

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.zadarma_key and self.settings.zadarma_secret)

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"format": "json"}
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None and v != ""})
        return params

    def call(self, method: str, params: dict[str, Any] | None = None, http_method: str = "GET") -> str:
        if not self.configured:
            raise RuntimeError("Zadarma no está configurado (faltan ZADARMA_KEY / ZADARMA_SECRET).")
        payload = self._params(params)
        verb = http_method.upper()
        headers = {
            "Authorization": authorization_header(
                self.settings.zadarma_key or "",
                self.settings.zadarma_secret or "",
                method,
                payload,
            )
        }
        url = f"{API_BASE}{method}"
        with httpx.Client(timeout=20.0) as client:
            if verb == "GET":
                response = client.get(url, params=payload, headers=headers)
            else:
                response = client.request(
                    verb,
                    url,
                    data=payload,
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
            response.raise_for_status()
            return response.text

    def send_sms(self, to: str, body: str, sender: str | None = None) -> str:
        if not self.configured:
            return json.dumps(
                {
                    "mode": "demo",
                    "channel": "zadarma_sms",
                    "to": to,
                    "body": body,
                    "status": "queued",
                    "pbx_id": self.settings.zadarma_pbx_id,
                },
                ensure_ascii=False,
            )
        params: dict[str, Any] = {"number": to, "message": body}
        if sender:
            params["caller_id"] = sender
        raw = self.call(SMS_METHOD, params, http_method="POST")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({"channel": "zadarma_sms", "raw": raw}, ensure_ascii=False)
        parsed.setdefault("channel", "zadarma_sms")
        if self.settings.zadarma_pbx_id:
            parsed.setdefault("pbx_id", self.settings.zadarma_pbx_id)
        return json.dumps(parsed, ensure_ascii=False)

    def verify_webhook_signature(self, payload: dict[str, Any], signature: str | None) -> bool:
        if not self.configured:
            return True
        if not signature:
            return False
        expected = webhook_signature(
            str(payload.get("caller_id") or ""),
            str(payload.get("called_did") or ""),
            str(payload.get("call_start") or ""),
            self.settings.zadarma_secret or "",
        )
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def extract_event(payload: dict[str, Any]) -> dict[str, Any]:
        event = str(payload.get("event") or payload.get("event_name") or "").upper()
        return {
            "event": event or "UNKNOWN",
            "caller_id": str(payload.get("caller_id") or payload.get("from") or ""),
            "called_did": str(payload.get("called_did") or payload.get("to") or ""),
            "destination": str(payload.get("destination") or ""),
            "call_start": str(payload.get("call_start") or ""),
            "pbx_call_id": str(payload.get("pbx_call_id") or payload.get("call_id") or ""),
            "internal": str(payload.get("internal") or ""),
            "duration": str(payload.get("duration") or ""),
            "disposition": str(payload.get("disposition") or ""),
            "is_recorded": str(payload.get("is_recorded") or ""),
        }

    def record_inbound(self, event: dict[str, Any]) -> dict[str, Any]:
        recorded = dict(event)
        recorded["pbx_id"] = self.settings.zadarma_pbx_id
        self.inbound_calls.append(recorded)
        try:
            from jarvis.memory import get_memory

            get_memory().remember(
                "Llamada entrante del taller (Zadarma PBX): "
                f"evento={recorded.get('event')} de {recorded.get('caller_id') or 'desconocido'} "
                f"a {recorded.get('called_did') or recorded.get('destination') or 'DID'} "
                f"inicio={recorded.get('call_start') or 'n/d'} "
                f"id={recorded.get('pbx_call_id') or 'n/d'}."
            )
        except Exception as exc:  # pragma: no cover - memoria opcional
            logger.warning("No se pudo persistir la llamada Zadarma en memoria: %s", exc)
        return recorded
