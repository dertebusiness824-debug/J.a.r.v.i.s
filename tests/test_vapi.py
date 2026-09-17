import asyncio
import json
import logging
import time

from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.api.vapi_routes import ERROR_REPLY, SLOW_REPLY


def _slow_supervisor(delay: float):
    """Supervisor lento: bloquea su hilo, como el LangGraph real (LLM + requests)."""

    async def _run(text, session_id=None):
        await asyncio.to_thread(time.sleep, delay)
        return {"final_answer": f"Listo tras {delay} segundos."}

    return _run


def _broken_supervisor(error: BaseException):
    async def _run(text, session_id=None):
        raise error

    return _run


def _sse_content(line: str) -> str:
    """Texto del delta de un chunk OpenAI; '' para keep-alives y para [DONE]."""
    if not line.startswith("data: ") or line.strip() == "data: [DONE]":
        return ""
    chunk = json.loads(line[len("data: ") :])
    assert chunk["object"] == "chat.completion.chunk"
    return str(chunk["choices"][0]["delta"].get("content") or "")


def test_vapi_empty_payload_ready():
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={})
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "Sistema listo."
    probe = client.get("/webhooks/vapi-llm")
    assert probe.status_code == 200
    assert probe.json()["status"] == "ok"


def test_vapi_custom_llm_calculator_is_spoken():
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/vapi-llm",
        json={
            "call": {"id": "voice-calc"},
            "messages": [
                {"role": "system", "content": "vapi"},
                {"role": "user", "content": "¿Cuánto es 17 * 24?"},
            ],
        },
    )
    assert res.status_code == 200
    spoken = res.json()["choices"][0]["message"]["content"]
    assert "408" in spoken
    assert "*" not in spoken
    assert "```" not in spoken


def test_vapi_server_url_shopify_payload():
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/vapi-llm",
        json={
            "message": {
                "messages": [{"role": "user", "content": "Lista los productos de Shopify"}]
            },
            "call": {"id": "voice-shop"},
        },
    )
    assert res.status_code == 200
    spoken = res.json()["choices"][0]["message"]["content"]
    assert "Inventario revisado" in spoken
    assert "{" not in spoken


def test_vapi_chat_completions_sse_openai_path():
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={
            "model": "jarvis-supervisor",
            "stream": True,
            "messages": [
                {"role": "system", "content": "vapi"},
                {"role": "user", "content": "¿Cuánto es 17 * 24?"},
            ],
        },
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())
    assert "text/event-stream" in res.headers["content-type"]
    assert "chat.completion.chunk" in body
    assert '"delta"' in body
    assert "408" in body
    assert '"finish_reason": "stop"' in body or '"finish_reason":"stop"' in body
    assert "data: [DONE]" in body
    assert body.strip().endswith("data: [DONE]")
    assert '"role": "assistant"' in body or '"role":"assistant"' in body
    assert res.headers.get("x-accel-buffering") == "no"


def test_vapi_openai_path_aliases_stream():
    client = TestClient(create_app())
    payload = {"stream": True, "messages": [{"role": "user", "content": "¿Cuánto es 2 + 2?"}]}
    for path in (
        "/v1/chat/completions",
        "/v1/chat/completions/",
        "/chat/completions",
        "/webhooks/vapi-llm/v1/chat/completions",
        "/webhooks/vapi-llm/chat/completions/",
    ):
        with client.stream("POST", path, json=payload) as res:
            assert res.status_code == 200, path
            body = "".join(res.iter_text())
        assert "4" in body, path
        assert "data: [DONE]" in body, path


def test_vapi_sse_sends_keepalive_while_supervisor_thinks(monkeypatch):
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.05")
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr("jarvis.api.vapi_routes.arun_jarvis", _slow_supervisor(0.3))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hola"}]},
    ) as res:
        assert res.status_code == 200
        lines = [line for line in res.iter_lines() if line.strip()]
    assert ": keep-alive" in lines
    assert "".join(_sse_content(line) for line in lines) == "Listo tras 0.3 segundos."
    assert lines[-1] == "data: [DONE]"
    get_settings.cache_clear()


def test_vapi_sse_answers_before_vapi_times_out(monkeypatch):
    """El stream cierra al vencer el presupuesto, no cuando el Supervisor acaba."""
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.2")
    monkeypatch.setattr("jarvis.api.vapi_routes.arun_jarvis", _slow_supervisor(4))
    from jarvis.api.vapi_routes import _sse_supervisor_reply
    from jarvis.config import get_settings

    get_settings.cache_clear()

    async def collect() -> list[tuple[float, str]]:
        started = time.monotonic()
        events = []
        async for chunk in _sse_supervisor_reply("hola", "test", "chatcmpl-test"):
            events.append((time.monotonic() - started, chunk))
        return events

    events = asyncio.run(collect())
    lines = [line for _at, chunk in events for line in chunk.splitlines() if line.strip()]
    spoken = "".join(_sse_content(line) for line in lines)
    assert events[0][0] < 0.5, "el primer chunk debe salir de inmediato"
    assert events[-1][0] < 2.5, "el stream no debe esperar a que el Supervisor termine"
    assert spoken == SLOW_REPLY
    assert ": keep-alive" in lines
    assert '"finish_reason": "stop"' in " ".join(lines)
    assert lines[-1] == "data: [DONE]"
    get_settings.cache_clear()


def test_vapi_json_path_also_answers_on_timeout(monkeypatch):
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr("jarvis.api.vapi_routes.arun_jarvis", _slow_supervisor(3))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/vapi-llm",
        json={"messages": [{"role": "user", "content": "hola"}]},
    )
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == SLOW_REPLY
    get_settings.cache_clear()


def test_vapi_sse_speaks_the_error_when_the_supervisor_explodes(monkeypatch, caplog):
    """Un fallo interno se dice en voz alta: nunca un stream cortado sin [DONE]."""
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.arun_jarvis",
        _broken_supervisor(RuntimeError("LangGraph roto")),
    )
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hola"}]},
    ) as res:
        assert res.status_code == 200
        lines = [line for line in res.iter_lines() if line.strip()]
    assert "".join(_sse_content(line) for line in lines) == ERROR_REPLY
    assert '"finish_reason": "stop"' in " ".join(lines)
    assert lines[-1] == "data: [DONE]"
    errors = [rec for rec in caplog.records if "[VAPI ERROR]" in rec.getMessage()]
    assert errors and errors[0].exc_info, "la traza del fallo debe quedar en el log"


def test_vapi_sse_does_not_mistake_a_tool_timeout_for_slowness(monkeypatch):
    """`TimeoutError` del Supervisor es un error, no el heartbeat venciendo."""
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.arun_jarvis",
        _broken_supervisor(TimeoutError("una herramienta HTTP expiró")),
    )
    from jarvis.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hola"}]},
    ) as res:
        assert res.status_code == 200
        lines = [line for line in res.iter_lines() if line.strip()]
    assert "".join(_sse_content(line) for line in lines) == ERROR_REPLY
    assert lines[-1] == "data: [DONE]"
    get_settings.cache_clear()


def test_vapi_json_path_speaks_the_error(monkeypatch):
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.arun_jarvis",
        _broken_supervisor(RuntimeError("LangGraph roto")),
    )
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={"messages": [{"role": "user", "content": "hola"}]})
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == ERROR_REPLY


def test_vapi_logs_the_incoming_message_and_the_spoken_reply(caplog):
    """Sin estas dos líneas en Render no se puede saber si Vapi llegó al backend."""
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={
            "stream": True,
            "call": {"id": "voice-logs"},
            "messages": [{"role": "user", "content": "¿Cuánto es 17 * 24?"}],
        },
    ) as res:
        assert res.status_code == 200
        "".join(res.iter_text())
    incoming = [rec.getMessage() for rec in caplog.records if "[VAPI INCOMING]" in rec.getMessage()]
    outgoing = [rec.getMessage() for rec in caplog.records if "[VAPI OUTGOING]" in rec.getMessage()]
    supervisor = [rec.getMessage() for rec in caplog.records if "[VAPI SUPERVISOR]" in rec.getMessage()]
    assert incoming and "¿Cuánto es 17 * 24?" in incoming[0]
    assert "voice-logs" in incoming[0]
    assert outgoing and "408" in outgoing[0]
    assert supervisor and "general" in supervisor[0]


def test_vapi_logs_a_payload_without_user_message(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={"call": {"id": "voice-empty"}})
    assert res.status_code == 200
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "[VAPI INCOMING]" in rec.getMessage()
    ]
    assert warnings and "sin mensaje de usuario" in warnings[0]
    assert "voice-empty" in warnings[0]


def test_vapi_logs_a_body_that_is_not_json(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/vapi-llm",
        content=b"esto no es json",
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "Sistema listo."
    assert any("cuerpo no-JSON" in rec.getMessage() for rec in caplog.records)


def test_vapi_logs_the_timeout_before_answering(monkeypatch, caplog):
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr("jarvis.api.vapi_routes.arun_jarvis", _slow_supervisor(3))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={"messages": [{"role": "user", "content": "hola"}]})
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == SLOW_REPLY
    assert any("[VAPI TIMEOUT]" in rec.getMessage() for rec in caplog.records)
    get_settings.cache_clear()


def test_vapi_sse_deltas_rebuild_the_sentence_verbatim():
    from jarvis.api.vapi_routes import _text_fragments

    text = "Sigo procesando la petición, señor. Dame unos segundos y pregúntame de nuevo."
    fragments = _text_fragments(text)
    assert len(fragments) > 1
    assert "".join(fragments) == text


def test_vapi_reads_the_last_message_even_without_role():
    """Payload OpenAI mínimo: `messages[-1].content` sin rol declarado."""
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={"messages": [{"content": "¿Cuánto es 2 + 2?"}]})
    assert res.status_code == 200
    assert "4" in res.json()["choices"][0]["message"]["content"]


def test_vapi_chat_completions_get_probe():
    client = TestClient(create_app())
    for path in ("/chat/completions", "/v1/chat/completions", "/webhooks/vapi-llm/chat/completions"):
        probe = client.get(path)
        assert probe.status_code == 200, path
        assert probe.json()["status"] == "ok"


def test_vapi_extracts_call_messages():
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/vapi-llm",
        json={"call": {"id": "voice-call-msgs", "messages": [{"role": "user", "content": "¿Cuánto es 2 + 2?"}]}},
    )
    assert res.status_code == 200
    assert "4" in res.json()["choices"][0]["message"]["content"]


def test_vapi_stream_sse():
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm",
        json={"stream": True, "messages": [{"role": "user", "content": "¿Cuánto es 2 + 2?"}]},
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())
    assert "text/event-stream" in res.headers["content-type"]
    assert "data:" in body
    assert "[DONE]" in body
    assert "4" in body


def test_voice_config_and_assistant_blueprint():
    client = TestClient(create_app())
    cfg = client.get("/voice/config")
    assert cfg.status_code == 200
    body = cfg.json()
    assert body["custom_llm_path"] == "/webhooks/vapi-llm"
    assert body["custom_llm_url"].endswith("/webhooks/vapi-llm")
    assert body["custom_llm_model"]["provider"] == "custom-llm"
    assert body["custom_llm_model"]["url"].endswith("/webhooks/vapi-llm")
    assert body["custom_llm_model"]["metadataSendMode"] == "off"
    assert body["custom_llm_model"]["timeoutSeconds"] == 90
    assert "vapi_public_key" in body
    assert "talk_enabled" in body
    blueprint = client.get("/voice/vapi-assistant")
    assert blueprint.status_code == 200
    data = blueprint.json()
    assert data["model"]["provider"] == "custom-llm"
    assert data["voice"]["provider"] == "cartesia"
    assert "/webhooks/vapi-llm" in data["model"]["url"]
    assert data["model"]["metadataSendMode"] == "off"


def test_voice_tts_without_cartesia():
    client = TestClient(create_app())
    res = client.post("/voice/tts", json={"text": "Hola"})
    assert res.status_code == 503


def test_voice_config_exposes_public_key_for_hud(monkeypatch):
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk-hud-test")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst-hud-test")
    from jarvis.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    body = client.get("/voice/config").json()
    assert body["vapi_public_key"] == "pk-hud-test"
    assert body["vapi_assistant_id"] == "asst-hud-test"
    assert body["talk_enabled"] is True
    get_settings.cache_clear()


def test_vapi_rejects_bad_secret(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "s3cret")
    from jarvis.config import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())
    denied = client.post("/webhooks/vapi-llm", json={"messages": [{"role": "user", "content": "hola"}]})
    assert denied.status_code == 401
    ok = client.post(
        "/webhooks/vapi-llm",
        headers={"Authorization": "Bearer s3cret"},
        json={"messages": [{"role": "user", "content": "¿Cuánto es 2+2?"}]},
    )
    assert ok.status_code == 200
    get_settings.cache_clear()
