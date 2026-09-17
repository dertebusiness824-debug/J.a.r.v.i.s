"""Wrappers de WhatsApp Cloud API + parsing de webhooks."""

from __future__ import annotations

import json
from typing import Any

import httpx

from jarvis.config import get_settings


class WhatsAppClient:
    """WhatsApp Cloud API. Sin token opera en modo demo."""

    GRAPH_BASE = "https://graph.facebook.com/v21.0"

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.whatsapp_access_token and self.settings.whatsapp_phone_number_id)

    def send_text(self, to: str, body: str) -> str:
        if not self.configured:
            return json.dumps(
                {"mode": "demo", "channel": "whatsapp", "to": to, "body": body, "status": "queued"},
                ensure_ascii=False,
            )
        url = f"{self.GRAPH_BASE}/{self.settings.whatsapp_phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "text",
            "text": {"body": body},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return json.dumps(response.json(), ensure_ascii=False)

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
