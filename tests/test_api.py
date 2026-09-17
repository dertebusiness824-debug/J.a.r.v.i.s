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
    assert "research_agent" in body["agents"]
    assert client.get("/docs").status_code == 200
    assert client.get("/").status_code == 200
    hud = client.get("/")
    assert "commandInput" in hud.text
    assert "NEURAL CORE" in hud.text
    assert "HABLAR" in hud.text
    assert 'id="talkBtn"' in hud.text
    assert "Delegación a Code, Comms, Shop o General" not in hud.text
    assert hud.headers.get("cache-control", "").startswith("no-store")
    assert "cdn.tailwindcss.com" in hud.text
    assert "backdrop-blur-md" in hud.text
    assert "font-mono" in hud.text
    assert "JSON.stringify(error, null, 2)" in hud.text
    assert "max-h-[40vh]" in hud.text
    assert "flex-1" in hud.text
    assert "from-orange-500" in hud.text
    assert "animate-pulse" in hud.text
    assert 'id="netLayer"' in hud.text
    assert "absolute inset-0 -z-10" in hud.text
    assert "object-cover" in hud.text
    assert "opacity-10" in hud.text
    assert "opacity-60" in hud.text
    assert "transition-opacity duration-1000 ease-in-out" in hud.text
    assert "animate-breath" in hud.text
    assert "speech-start" in hud.text
    assert 'id="netVeil"' in hud.text
    assert "bg-black/40" in hud.text
    assert "isolate" in hud.text


def test_directive_and_inbox_status():
    client = TestClient(create_app())
    empty = client.get("/api/jarvis/inbox-status")
    assert empty.status_code == 200
    assert empty.json()["whatsapp"] == 0
    assert empty.json()["correo"] == 0

    client.post("/webhooks/whatsapp-local", json={"from": "34911@c.us", "body": "cita"})
    from jarvis.db import guardar_mensaje

    guardar_mensaje("gmail", "ana@x.com", "factura")
    counts = client.get("/api/jarvis/inbox-status").json()
    assert counts["whatsapp"] == 1
    assert counts["correo"] == 1
    assert counts["total"] == 2

    res = client.post("/api/jarvis/directive", json={"command": "¿Cuánto es 17 * 24?", "session_id": "hud-1"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "408" in body["answer"]
    assert body["lines"][0].startswith(">")
    assert "408" in body["lines"][1]


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


def test_whatsapp_local_bridge_inbound():
    client = TestClient(create_app())
    ping = client.get("/webhooks/whatsapp-local")
    assert ping.status_code == 200
    assert ping.json()["ok"] is True

    inbound = client.post(
        "/webhooks/whatsapp-local",
        json={"from": "34600000000@c.us", "body": "Hola, ¿tienen cita?"},
    )
    assert inbound.status_code == 200
    assert inbound.json()["status"] == "saved_to_inbox"
    assert inbound.json()["saved"] is True

    master = client.post(
        "/webhooks/whatsapp-local",
        json={"from": "34605686509@c.us", "body": "¿Cuánto es 2 + 2?"},
    )
    assert master.status_code == 200
    assert master.json()["status"] == "saved_to_inbox"


def test_whatsapp_local_on_uvicorn_app():
    from jarvis.api.main import MASTER_NUMBER, app as uvicorn_app

    assert MASTER_NUMBER == "34605686509"
    client = TestClient(uvicorn_app)
    res = client.post(
        "/webhooks/whatsapp-local",
        json={"from": "34605686509", "body": "ping"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "saved_to_inbox"


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
