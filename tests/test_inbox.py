import email

from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.db import guardar_mensaje, listar_no_leidos
from jarvis.tools.email_reader import plataforma_desde_host, resumen_correo, revisar_correos_nuevos

VAPI_LLM_URL = "https://j-a-r-v-i-s-yghr.onrender.com/webhooks/vapi-llm"


def test_vapi_events_empty_inbox():
    client = TestClient(create_app())
    res = client.post(
        "/api/jarvis/vapi-events",
        json={"message": {"type": "assistant-request"}},
    )
    assert res.status_code == 200
    body = res.json()
    spoken = body["assistant"]["firstMessage"]
    assert spoken == "Sistemas en línea. No hay mensajes pendientes, señor."
    assert body["assistant"]["model"]["provider"] == "custom-llm"
    assert body["assistant"]["model"]["url"] == VAPI_LLM_URL


def test_vapi_events_briefing_marks_read():
    client = TestClient(create_app())
    client.post("/webhooks/whatsapp-local", json={"from": "34911000000@c.us", "body": "Necesito cita"})
    client.post("/webhooks/whatsapp-local", json={"from": "34600000001@c.us", "body": "Presupuesto"})
    guardar_mensaje("gmail", "ana@example.com", "Asunto: factura")

    first = client.post(
        "/api/jarvis/vapi-events",
        json={"message": {"type": "assistant-request"}},
    )
    assert first.status_code == 200
    spoken = first.json()["assistant"]["firstMessage"]
    assert "Sistemas en línea" in spoken
    assert "3 mensajes" in spoken
    assert "WhatsApp" in spoken
    assert "Gmail" in spoken
    assert first.json()["assistant"]["model"]["url"] == VAPI_LLM_URL

    second = client.post(
        "/api/jarvis/vapi-events",
        json={"message": {"type": "assistant-request"}},
    )
    assert "No hay mensajes pendientes" in second.json()["assistant"]["firstMessage"]
    assert listar_no_leidos() == []


def test_vapi_events_other_types_empty_200():
    client = TestClient(create_app())
    res = client.post(
        "/api/jarvis/vapi-events",
        json={"message": {"type": "status-update", "status": "in-progress"}},
    )
    assert res.status_code == 200
    assert res.content == b""


def test_plataforma_imap_host():
    assert plataforma_desde_host("imap.gmail.com") == "gmail"
    assert plataforma_desde_host("imap.ionos.es") == "webmail"


def test_resumen_correo_plain():
    raw = (
        b"From: Ana <ana@example.com>\r\n"
        b"Subject: Hola taller\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Necesito una cita el lunes.\r\n"
    )
    msg = email.message_from_bytes(raw)
    remitente, contenido = resumen_correo(msg)
    assert "Ana" in remitente
    assert "Hola taller" in contenido
    assert "cita" in contenido


def test_revisar_correos_sin_credenciales():
    assert revisar_correos_nuevos() == []
