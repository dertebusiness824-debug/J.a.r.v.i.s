import base64
import time

from fastapi.testclient import TestClient
from starlette.requests import Request

from jarvis.api.app import create_app
from jarvis.api.vapi_auth import (
    authorized_vapi_request,
    is_signing_secret,
    standard_webhook_signature,
    verify_standard_webhooks,
)
from jarvis.config import get_settings

TEST_WHSEC = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-ok!").decode("ascii")
JSON_BODY = b'{"message":{"type":"status-update"}}'


def _request(headers: dict[str, str], body: bytes = JSON_BODY) -> Request:
    header_list = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/jarvis/vapi-events",
            "headers": header_list,
            "query_string": b"",
        }
    )


def _signed_headers(secret: str, body: bytes = JSON_BODY, *, now: int | None = None) -> dict[str, str]:
    ts = str(int(time.time() if now is None else now))
    return {
        "webhook-id": "msg_test_1",
        "webhook-timestamp": ts,
        "webhook-signature": standard_webhook_signature(secret, "msg_test_1", ts, body),
        "content-type": "application/json",
    }


def test_whsec_is_a_signing_secret():
    assert is_signing_secret(TEST_WHSEC)
    assert not is_signing_secret("s3cret")


def test_standard_webhooks_accepts_valid_hmac():
    headers = _signed_headers(TEST_WHSEC)
    assert verify_standard_webhooks(
        TEST_WHSEC,
        msg_id=headers["webhook-id"],
        timestamp=headers["webhook-timestamp"],
        signatures=headers["webhook-signature"],
        body=JSON_BODY,
    )


def test_standard_webhooks_rejects_bad_hmac_and_old_timestamp():
    headers = _signed_headers(TEST_WHSEC)
    assert not verify_standard_webhooks(
        TEST_WHSEC,
        msg_id=headers["webhook-id"],
        timestamp=headers["webhook-timestamp"],
        signatures="v1,dG9vLWJhZC1zaWduYXR1cmU=",
        body=JSON_BODY,
    )
    old = str(int(time.time()) - 301)
    assert not verify_standard_webhooks(
        TEST_WHSEC,
        msg_id="msg_old",
        timestamp=old,
        signatures=standard_webhook_signature(TEST_WHSEC, "msg_old", old, JSON_BODY),
        body=JSON_BODY,
    )


def test_signing_secret_allows_unsigned_custom_llm(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", TEST_WHSEC)
    get_settings.cache_clear()
    assert authorized_vapi_request(_request({}), body=JSON_BODY, strict=False) is True
    assert authorized_vapi_request(_request({}), body=JSON_BODY, strict=True) is False


def test_signing_secret_rejects_wrong_bearer(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", TEST_WHSEC)
    get_settings.cache_clear()
    req = _request({"authorization": "Bearer wrong"})
    assert authorized_vapi_request(req, body=JSON_BODY, strict=False) is False


def test_plain_secret_still_requires_bearer(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "s3cret")
    get_settings.cache_clear()
    assert authorized_vapi_request(_request({}), strict=False) is False
    ok = _request({"authorization": "Bearer s3cret"})
    assert authorized_vapi_request(ok, strict=False) is True
    header = _request({"x-vapi-secret": "s3cret"})
    assert authorized_vapi_request(header, strict=False) is True


def test_vapi_events_require_proof_when_whsec_set(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", TEST_WHSEC)
    get_settings.cache_clear()
    client = TestClient(create_app())
    denied = client.post(
        "/api/jarvis/vapi-events",
        content=JSON_BODY,
        headers={"content-type": "application/json"},
    )
    assert denied.status_code == 401
    ok = client.post(
        "/api/jarvis/vapi-events",
        content=JSON_BODY,
        headers=_signed_headers(TEST_WHSEC),
    )
    assert ok.status_code == 200


def test_vapi_events_accept_x_vapi_secret(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", TEST_WHSEC)
    get_settings.cache_clear()
    client = TestClient(create_app())
    res = client.post(
        "/api/jarvis/vapi-events",
        json={"message": {"type": "status-update"}},
        headers={"X-Vapi-Secret": TEST_WHSEC},
    )
    assert res.status_code == 200


def test_custom_llm_stays_open_with_whsec(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", TEST_WHSEC)
    get_settings.cache_clear()
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={"messages": [{"role": "user", "content": "hola"}]})
    assert res.status_code == 200
    denied = client.post(
        "/webhooks/vapi-llm",
        headers={"Authorization": "Bearer wrong"},
        json={"messages": [{"role": "user", "content": "hola"}]},
    )
    assert denied.status_code == 401
    signed = client.post(
        "/webhooks/vapi-llm",
        content=b'{"messages":[{"role":"user","content":"hola"}]}',
        headers=_signed_headers(TEST_WHSEC, b'{"messages":[{"role":"user","content":"hola"}]}'),
    )
    assert signed.status_code == 200
