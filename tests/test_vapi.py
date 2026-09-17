from fastapi.testclient import TestClient

from jarvis.api.app import create_app


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
