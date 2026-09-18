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
    alias_preflight = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "https://api.vapi.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert alias_preflight.headers.get("access-control-allow-origin") == "*"
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
    assert "J.A.R.V.I.S" in hud.text
    assert 'id="talkBtn"' in hud.text
    assert "Delegación a Code, Comms, Shop o General" not in hud.text
    assert hud.headers.get("cache-control", "").startswith("no-store")
    assert "cdn.tailwindcss.com" in hud.text
    assert "font-mono" in hud.text
    assert "/static/format-error.js" in hud.text
    assert "/static/hud.js" in hud.text
    assert "terminalMessages" in hud.text
    assert "console.error" in hud.text
    # Layout móvil: todo cabe en la pantalla, sin scroll accidental; el dock inferior
    # es lo único que desplaza, y por dentro.
    assert "flex h-screen w-full items-center justify-center overflow-hidden" in hud.text
    assert "100dvh" in hud.text
    assert "bg-gradient-to-b from-slate-900 via-blue-950 to-slate-900" in hud.text
    assert "max-h-[22vh]" in hud.text
    assert "flex-1" in hud.text
    assert "env(safe-area-inset-bottom)" in hud.text
    # Pistas de circuito en dos esquinas: la misma figura, una girada 180°.
    assert hud.text.count("text-cyan-500/20") == 2
    assert "left-0 top-0" in hud.text
    assert "bottom-0 right-0" in hud.text and "rotate-180" in hud.text
    assert hud.text.count('stroke-dasharray="5 4"') == 2
    # Núcleo holográfico: texto ancho con brillo y al menos tres anillos concéntricos,
    # discontinuos y sólidos, en cian y azul.
    assert 'id="holoCore"' in hud.text
    assert 'data-voice="idle"' in hud.text and 'data-researching="0"' in hud.text
    assert "h-72 w-72 md:h-96 md:w-96" in hud.text
    assert 'id="coreLabel"' in hud.text
    assert "font-hud text-xl font-bold tracking-widest text-white drop-shadow-[0_0_10px_rgba(6,182,212,0.8)]" in hud.text
    assert "border-2 border-dashed border-cyan-500/50" in hud.text
    assert "border border-dashed border-blue-500/30" in hud.text
    assert hud.text.count("rounded-full border border-cyan-500/50") >= 1
    assert hud.text.count("rounded-full border border-blue-500/30") >= 1
    assert hud.text.count("rounded-full border") >= 4
    assert 'data-hud="globe"' in hud.text and 'data-hud="net"' in hud.text
    assert 'data-hud="ring-outer"' in hud.text and 'data-hud="ring-mid"' in hud.text
    assert 'id="corePing"' in hud.text
    assert "active:scale-95" in hud.text
    assert "orange" not in hud.text
    assert "emerald" not in hud.text
    # Los anillos giran en sentidos opuestos, y solo cuando Jarvis habla o escucha.
    assert "@keyframes spin" in hud.text
    assert "animate-[spin_14s_linear_infinite]" in hud.text
    assert "animate-[spin_20s_linear_infinite] [animation-direction:reverse]" in hud.text
    assert ".ring-spin { animation-play-state: paused; }" in hud.text
    assert '[data-voice="speaking"] .ring-spin { animation-play-state: running; }' in hud.text
    assert "prefers-reduced-motion" in hud.text
    # Modo investigación: los anillos se apagan y entra la red neuronal en su sitio.
    assert '[data-researching="1"] [data-hud="rings"] { opacity: 0; transform: scale(0.9); }' in hud.text
    assert '[data-researching="0"] [data-hud="net"] { opacity: 0;' in hud.text
    assert "isResearching" in hud.text
    assert "setResearching" in hud.text
    assert "hud.setResearching(isResearching())" in hud.text
    assert "ACCEDIENDO A LA RED GLOBAL" in hud.text
    assert 'id="researchBanner"' in hud.text
    # Voz: los estados y el volumen de Vapi llegan al núcleo.
    assert "createHoloCore(document.getElementById(\"holoCore\"))" in hud.text
    assert "hud.setVoice(callStatus)" in hud.text
    assert 'vapi.on("volume-level"' in hud.text
    assert "hud.setVolume(level)" in hud.text
    assert "speech-start" in hud.text
    assert "isolate" in hud.text
    assert "metadataSendMode" in hud.text
    assert "@vapi-ai/web@2.6.3" in hud.text
    assert "vapi.start(assistant)" in hud.text
    assert "if (!call" in hud.text
    assert "await vapi.stop()" in hud.text
    assert "lastVapiError" in hud.text
    assert "getUserMedia" in hud.text
    assert "INICIAR SISTEMA" in hud.text
    assert "Requiere interacción manual para desbloquear canales de audio" in hud.text
    assert "startVapiCall" in hud.text
    assert "auto: true" in hud.text
    # El texto del núcleo va en su propio span (los anillos no se pisan al cambiar de estado).
    assert "talkBtn.textContent" not in hud.text
    # Prompt de consola con cursor parpadeante justo detrás del log.
    assert 'id="terminalCaret"' in hud.text
    assert "animate-caret inline-block h-4 w-2 bg-cyan-400" in hud.text
    assert "@keyframes caret" in hud.text
    assert "supervisor@jarvis:~$" in hud.text
    assert 'id="terminalText"' in hud.text
    assert "terminalText.textContent = terminalMessages.join" in hud.text
    assert "terminal.textContent" not in hud.text
    # Contadores de la bandeja en cajas de panel de mandos.
    assert hud.text.count("rounded-sm border border-cyan-500/30 bg-cyan-900/20") == 3
    assert hud.text.count("font-mono text-base font-black text-cyan-300") == 3


def test_holo_core_module_is_served_and_self_contained():
    client = TestClient(create_app())
    res = client.get("/static/hud.js")
    assert res.status_code == 200
    js = res.text
    assert "createHoloCore" in js
    # Una sola API hacia la página: voz, volumen e investigación.
    for method in ("setVoice", "setVolume", "setResearching"):
        assert f"function {method}(" in js
    # El módulo no habla con el backend ni con Vapi: recibe estado y pinta.
    assert "vapi.on(" not in js and "import(" not in js
    assert "fetch(" not in js
    # Rendimiento en móvil: un rAF, DPR limitado, sin shadowBlur, parada con la pestaña oculta.
    assert "requestAnimationFrame" in js
    assert "DPR_CAP = 2" in js
    assert "shadowBlur =" not in js
    assert "visibilitychange" in js
    assert "prefers-reduced-motion" in js
    # El volumen modula escala y opacidad del anillo exterior, y el brillo del texto.
    assert "ringOuter.style.transform" in js
    assert "ringOuter.style.opacity" in js
    assert "label.style.textShadow" in js
    # Sin datos de volumen (voz del navegador) el núcleo sigue latiendo al hablar.
    assert "state.voice === \"speaking\"" in js


def test_directive_and_inbox_status():
    client = TestClient(create_app())
    empty = client.get("/api/jarvis/inbox-status")
    assert empty.status_code == 200
    assert empty.json()["whatsapp"] == 0
    assert empty.json()["correo"] == 0
    assert empty.json()["researching"] is False

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
