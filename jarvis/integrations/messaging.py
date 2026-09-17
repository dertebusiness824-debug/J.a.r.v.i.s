"""Puente WhatsApp Web (whatsapp-web.js) + parsing de webhooks locales/Meta."""

from __future__ import annotations

import json
from typing import Any

import httpx

from jarvis.config import get_settings


def to_whatsapp_id(to: str) -> str:
    """Normaliza E.164 o dígitos a JID de WhatsApp Web (`numero@c.us`)."""
    value = (to or "").strip()
    if not value:
        return value
    if "@" in value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"{digits}@c.us" if digits else value


class WhatsAppClient:
    """Envío vía puente local whatsapp-web.js. Si el puente no responde, modo demo."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.whatsapp_bridge_url)

    def send_text(self, to: str, body: str) -> str:
        chat_id = to_whatsapp_id(to)
        url = f"{self.settings.whatsapp_bridge_url.rstrip('/')}/send"
        payload = {"to": chat_id, "message": body}
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    data = {"raw": response.text}
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text if exc.response is not None else str(exc)
            return json.dumps(
                {"ok": False, "channel": "whatsapp-web", "to": chat_id, "error": detail},
                ensure_ascii=False,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
            return json.dumps(
                {
                    "mode": "demo",
                    "channel": "whatsapp-web",
                    "to": chat_id,
                    "body": body,
                    "status": "queued",
                    "error": f"puente no disponible: {exc}",
                },
                ensure_ascii=False,
            )
        if isinstance(data, dict):
            data.setdefault("channel", "whatsapp-web")
            data.setdefault("to", chat_id)
            return json.dumps(data, ensure_ascii=False)
        return json.dumps({"channel": "whatsapp-web", "to": chat_id, "result": data}, ensure_ascii=False)

    def verify_webhook(self, mode: str, token: str, challenge: str) -> str | None:
        expected = self.settings.whatsapp_verify_token
        if mode == "subscribe" and expected and token == expected:
            return challenge
        if mode == "subscribe" and not expected:
            return challenge
        return None

    @staticmethod
    def extract_inbound(payload: dict[str, Any]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    text = (msg.get("text") or {}).get("body") or ""
                    if text:
                        messages.append({"from": msg.get("from", ""), "body": text, "id": msg.get("id", "")})
        return messages
