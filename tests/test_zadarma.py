import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.config import get_settings
from jarvis.integrations.zadarma import (
    ZadarmaClient,
    api_signature,
    authorization_header,
    webhook_signature,
)
from jarvis.tools import send_zadarma_sms

# Vectores independientes (hashlib/hmac) de la firma oficial Zadarma.
_API_SIGN = "YzJlOThjODc1ZGMxZWZmZjRjMWYzMmY1YTdkNTdjZDQ4OWE5NWE1Mw=="
_WEBHOOK_SIGN = "ZGJjNTdiYzI1ZmJlNTZhY2I4OGVmMTZmZDcyNzMwOTNlNDM5MTM4Mw=="


def test_api_signature_matches_official_algorithm():
    params = {"format": "json", "message": "hola", "number": "+15551234567"}
    assert api_signature("/v1/sms/send/", params, "test-secret") == _API_SIGN
    assert authorization_header("test-key", "test-secret", "/v1/sms/send/", params) == f"test-key:{_API_SIGN}"


def test_webhook_signature_matches_pbx_docs():
    assert (
        webhook_signature("+34911000000", "+34911999888", "2026-09-17 10:00:00", "test-secret")
        == _WEBHOOK_SIGN
    )


def test_send_zadarma_sms_demo_without_keys():
    out = json.loads(send_zadarma_sms.invoke({"to": "+15550001111", "body": "cita taller"}))
    assert out["mode"] == "demo"
    assert out["channel"] == "zadarma_sms"
    assert out["to"] == "+15550001111"
    assert out["status"] == "queued"


def test_send_sms_posts_signed_request(monkeypatch):
    monkeypatch.setenv("ZADARMA_KEY", "test-key")
    monkeypatch.setenv("ZADARMA_SECRET", "test-secret")
    monkeypatch.setenv("ZADARMA_PBX_ID", "pbx-1")
    get_settings.cache_clear()

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = json.dumps({"status": "success", "messages": 1})

    with patch("jarvis.integrations.zadarma.httpx.Client") as client_cls:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        instance.request.return_value = response
        client_cls.return_value = instance

        payload = json.loads(
            send_zadarma_sms.invoke({"to": "+15551234567", "body": "hola", "sender": "TALLER"})
        )
        assert payload["status"] == "success"
        assert payload["channel"] == "zadarma_sms"
        assert payload["pbx_id"] == "pbx-1"

        args, kwargs = instance.request.call_args
        assert args[0] == "POST"
        assert args[1] == "https://api.zadarma.com/v1/sms/send/"
        assert kwargs["data"]["number"] == "+15551234567"
        assert kwargs["data"]["message"] == "hola"
        assert kwargs["data"]["caller_id"] == "TALLER"
        assert kwargs["headers"]["Authorization"].startswith("test-key:")


def test_zadarma_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("ZADARMA_KEY", "test-key")
    monkeypatch.setenv("ZADARMA_SECRET", "test-secret")
    get_settings.cache_clear()
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/zadarma",
        data={
            "event": "NOTIFY_START",
            "caller_id": "+34911000000",
            "called_did": "+34911999888",
            "call_start": "2026-09-17 10:00:00",
        },
        headers={"Signature": "invalid"},
    )
    assert res.status_code == 403


def test_zadarma_webhook_accepts_valid_signature(monkeypatch):
    monkeypatch.setenv("ZADARMA_KEY", "test-key")
    monkeypatch.setenv("ZADARMA_SECRET", "test-secret")
    get_settings.cache_clear()
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/zadarma",
        data={
            "event": "NOTIFY_START",
            "caller_id": "+34911000000",
            "called_did": "+34911999888",
            "call_start": "2026-09-17 10:00:00",
            "pbx_call_id": "in-signed",
        },
        headers={"Signature": _WEBHOOK_SIGN},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["recorded"] is True
    assert ZadarmaClient.inbound_calls[-1]["pbx_call_id"] == "in-signed"
