from fastapi.testclient import TestClient

from jarvis.api.app import create_app


def test_cors_allows_vapi_and_webhooks():
    client = TestClient(create_app())
    preflight = client.options(
        "/webhooks/vapi-llm",
        headers={
            "Origin": "https://api.vapi.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert preflight.headers.get("access-control-allow-origin") == "*"
    assert "POST" in (preflight.headers.get("access-control-allow-methods") or "").upper()
    health = client.get("/health", headers={"Origin": "https://dashboard.vapi.ai"})
    assert health.status_code == 200
    assert health.headers.get("access-control-allow-origin") == "*"


def test_health_and_docs():
    client = TestClient(create_app())
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert "code_agent" in body["agents"]
    assert client.get("/docs").status_code == 200
    assert client.get("/").status_code == 200


def test_invoke_calculator():
    client = TestClient(create_app())
    res = client.post("/invoke", json={"message": "¿Cuánto es 17 * 24?", "session_id": "api-1"})
    assert res.status_code == 200
    data = res.json()
    assert "408" in data["answer"]
    assert data["agent"] == "general"


def test_whatsapp_verify_and_inbound():
    client = TestClient(create_app())
    verify = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "challenge-42"},
    )
    assert verify.status_code == 200
    assert verify.text == "challenge-42"

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "id": "wamid.1",
                                    "text": {"body": "¿Cuánto es 2 + 2?"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    inbound = client.post("/webhooks/whatsapp", json=payload)
    assert inbound.status_code == 200
    body = inbound.json()
    assert body["processed"] == 1
    assert "4" in body["replies"][0]["answer"]


def test_twilio_webhook_removed():
    client = TestClient(create_app())
    res = client.post("/webhooks/twilio", data={"From": "+15550001111", "Body": "¿Cuánto es 3*3?"})
    assert res.status_code == 404


def test_zadarma_echo_and_inbound_call():
    client = TestClient(create_app())
    echo = client.get("/webhooks/zadarma", params={"zd_echo": "echo-42"})
    assert echo.status_code == 200
    assert echo.text == "echo-42"

    inbound = client.post(
        "/webhooks/zadarma",
        data={
            "event": "NOTIFY_START",
            "caller_id": "+34911000000",
            "called_did": "+34911999888",
            "call_start": "2026-09-17 10:00:00",
            "pbx_call_id": "in-1",
        },
    )
    assert inbound.status_code == 200
    body = inbound.json()
    assert body["ok"] is True
    assert body["recorded"] is True
    assert body["event"]["event"] == "NOTIFY_START"
    assert body["event"]["caller_id"] == "+34911000000"
