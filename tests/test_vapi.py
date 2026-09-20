import asyncio
import json
import logging
import time

from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.api.vapi_routes import ERROR_REPLY, FILLER_PHRASES, SLOW_REPLY


def _instant_supervisor(answer: str):
    async def _run(text, session_id=None):
        return {"final_answer": answer}

    return _run


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


def _instant_stream(answer: str):
    async def _stream(text, session_id=None):
        yield {"kind": "state", "state": {"final_answer": answer}}

    return _stream


def _slow_stream(delay: float, *, events: list[dict] | None = None):
    """Grafo lento: bloquea su hilo, como el LangGraph real (LLM + requests)."""

    async def _stream(text, session_id=None):
        for event in events or []:
            yield event
        await asyncio.to_thread(time.sleep, delay)
        yield {"kind": "state", "state": {"final_answer": f"Listo tras {delay} segundos."}}

    return _stream


def _broken_stream(error: BaseException):
    async def _stream(text, session_id=None):
        raise error
        yield {}  # pragma: no cover - marca la función como generadora

    return _stream


def _collect_sse(user_text: str = "hola", session: str = "call-test") -> list[tuple[float, str]]:
    """Chunks del generador SSE con el instante en que los suelta."""
    from jarvis.api.vapi_routes import _sse_supervisor_reply

    async def _run() -> list[tuple[float, str]]:
        started = time.monotonic()
        events = []
        async for chunk in _sse_supervisor_reply(user_text, session, "chatcmpl-test"):
            events.append((time.monotonic() - started, chunk))
        return events

    return asyncio.run(_run())


def _said(events: list[tuple[float, str]]) -> list[tuple[float, str]]:
    deltas = [(at, _sse_content(line)) for at, chunk in events for line in chunk.splitlines()]
    return [(at, text) for at, text in deltas if text.strip()]


def _spoken(events: list[tuple[float, str]]) -> str:
    return "".join(_sse_content(line) for _at, chunk in events for line in chunk.splitlines())


def _sse_content(line: str) -> str:
    """Texto del delta de un chunk OpenAI; '' para keep-alives y para [DONE]."""
    if not line.startswith("data: ") or line.strip() == "data: [DONE]":
        return ""
    chunk = json.loads(line[len("data: ") :])
    assert chunk["object"] == "chat.completion.chunk"
    return str(chunk["choices"][0]["delta"].get("content") or "")


def _post_sse(path: str, payload: dict, *, headers: dict | None = None) -> tuple[int, str, str]:
    """POST al Custom LLM: body SSE y content-type. Los eventos de ruido no llegan aquí."""
    client = TestClient(create_app())
    with client.stream("POST", path, json=payload, headers=headers or {}) as res:
        body = "".join(res.iter_text())
        return res.status_code, body, res.headers.get("content-type") or ""


def _spoken_sse(body: str) -> str:
    return "".join(_sse_content(line) for line in body.splitlines())


def test_vapi_empty_payload_ready():
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={})
    assert res.status_code == 200
    assert res.json() == {}
    probe = client.get("/webhooks/vapi-llm")
    assert probe.status_code == 200
    assert probe.json()["status"] == "ok"


def test_vapi_control_events_are_acked_without_langgraph(monkeypatch, caplog):
    """speech-update / status-update no son un turno: 200 vacío, sin Supervisor."""

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("LangGraph no debe correr en un evento de control")

    async def _must_not_stream(*_args, **_kwargs):
        raise AssertionError("LangGraph no debe correr en un evento de control")
        yield {}  # pragma: no cover

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _must_not_stream)
    monkeypatch.setattr("jarvis.api.vapi_routes.arun_jarvis", _must_not_run)
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    for payload in (
        {"message": {"type": "speech-update", "status": "started", "role": "assistant"}},
        {"message": {"type": "status-update", "status": "in-progress"}},
        {"message": {"type": "transcript", "role": "user", "transcript": "hola", "transcriptType": "partial"}},
        {"message": {"type": "metadata", "metadata": {"foo": 1}}},
        {"type": "conversation-update"},
    ):
        res = client.post("/webhooks/vapi-llm", json=payload)
        assert res.status_code == 200, payload
        assert res.json() == {}
    assert not any("[VAPI INCOMING]" in rec.getMessage() for rec in caplog.records)
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_vapi_custom_llm_calculator_is_spoken():
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {
            "call": {"id": "voice-calc"},
            "messages": [
                {"role": "system", "content": "vapi"},
                {"role": "user", "content": "¿Cuánto es 17 * 24?"},
            ],
        },
    )
    assert status == 200
    assert "text/event-stream" in ctype
    spoken = _spoken_sse(body)
    assert "408" in spoken
    assert "*" not in spoken
    assert "```" not in spoken
    assert "data: [DONE]" in body


def test_vapi_server_url_shopify_payload():
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {
            "message": {
                "messages": [{"role": "user", "content": "Lista los productos de Shopify"}]
            },
            "call": {"id": "voice-shop"},
        },
    )
    assert status == 200
    assert "text/event-stream" in ctype
    spoken = _spoken_sse(body)
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
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(0.3))
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
    spoken = "".join(_sse_content(line) for line in lines)
    assert spoken.endswith("Listo tras 0.3 segundos.")
    assert spoken.split(" Listo")[0] in FILLER_PHRASES
    assert lines[-1] == "data: [DONE]"
    get_settings.cache_clear()


def test_vapi_sse_speaks_a_filler_before_langgraph_answers(monkeypatch):
    """Fase 1: la voz arranca en milisegundos aunque el grafo tarde segundos."""
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.2")
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("VAPI_FILLER_DELAY_SECONDS", "0.1")
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(1.5))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    events = _collect_sse(session="call-filler")
    said = _said(events)
    assert said, "el stream debe llevar texto hablado"
    first_at, first_text = said[0]
    assert first_text in FILLER_PHRASES
    assert first_at < 0.5, f"la frase puente llegó tarde ({first_at:.2f} s)"
    assert first_at < events[-1][0] / 2, "la frase puente debe adelantarse mucho a la respuesta"
    # El separador cuenta: sin él el TTS diría "directiva…Listo".
    assert _spoken(events) == f"{first_text} Listo tras 1.5 segundos."
    get_settings.cache_clear()


def test_vapi_sse_skips_the_filler_when_the_answer_is_instant(monkeypatch):
    """Sin silencio que tapar no se añade paja: la respuesta va sola."""
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _instant_stream("Son 408."))
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "¿Cuánto es 17 * 24?"}]},
    ) as res:
        assert res.status_code == 200
        lines = [line for line in res.iter_lines() if line.strip()]
    assert "".join(_sse_content(line) for line in lines) == "Son 408."
    assert lines[-1] == "data: [DONE]"


def test_vapi_filler_does_not_repeat_itself_in_the_same_call():
    from jarvis.api.vapi_routes import pick_filler

    previous = ""
    for _turn in range(12):
        phrase = pick_filler("call-rotation")
        assert phrase in FILLER_PHRASES
        assert phrase != previous, "dos turnos seguidos con la misma frase suenan a bucle"
        previous = phrase


def test_vapi_logs_the_filler_it_spoke(monkeypatch, caplog):
    monkeypatch.setenv("VAPI_FILLER_DELAY_SECONDS", "0.05")
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(0.4))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={"stream": True, "call": {"id": "voice-filler"}, "messages": [{"role": "user", "content": "hola"}]},
    ) as res:
        assert res.status_code == 200
        "".join(res.iter_text())
    fillers = [rec.getMessage() for rec in caplog.records if "[VAPI FILLER]" in rec.getMessage()]
    outgoing = [rec.getMessage() for rec in caplog.records if "[VAPI OUTGOING]" in rec.getMessage()]
    assert fillers and "voice-filler" in fillers[0]
    assert outgoing and "puente=" in outgoing[0]
    get_settings.cache_clear()


def test_vapi_narrates_the_tool_it_is_waiting_for(monkeypatch):
    """Tavily tarda segundos: la voz cuenta lo que pasa en vez de callarse."""
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.1")
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.astream_jarvis",
        _slow_stream(1.2, events=[{"kind": "tool", "tool": "web_search"}]),
    )
    from jarvis.config import get_settings

    get_settings.cache_clear()
    events = _collect_sse(user_text="investiga a quien sea", session="call-narra")
    said = [text for _at, text in _said(events)]
    assert "Accediendo a la red global, maestro." in said
    narration_at = next(at for at, text in _said(events) if "red global" in text)
    answer_at = next(at for at, text in _said(events) if "Listo tras" in text)
    assert narration_at < answer_at, "la narración va mientras la herramienta trabaja"
    assert _spoken(events).endswith("Listo tras 1.2 segundos.")
    get_settings.cache_clear()


def test_vapi_does_not_narrate_a_tool_that_answers_at_once(monkeypatch):
    """Anunciar una herramienta instantánea solo alarga la respuesta."""
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.astream_jarvis",
        _slow_stream(0.05, events=[{"kind": "tool", "tool": "shopify_inventory_summary"}]),
    )
    spoken = _spoken(_collect_sse(session="call-fast-tool"))
    assert spoken == "Listo tras 0.05 segundos."
    assert "Revisando" not in spoken


def test_vapi_does_not_narrate_the_same_tool_twice(monkeypatch):
    """El research_agent repite web_search: decirlo cada vez suena a disco rayado."""
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")

    async def _stream(text, session_id=None):
        for _round in range(3):
            yield {"kind": "tool", "tool": "web_search"}
            yield {"kind": "tool", "tool": "calculate_expression"}
            await asyncio.to_thread(time.sleep, 0.8)
        yield {"kind": "state", "state": {"final_answer": "Información pública recopilada."}}

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    from jarvis.config import get_settings

    get_settings.cache_clear()
    spoken = _spoken(_collect_sse(session="call-repeat"))
    assert spoken.count("Accediendo a la red global") == 1
    # La calculadora es instantánea: narrarla solo añadiría ruido.
    assert "calculate_expression" not in spoken
    assert spoken.endswith("Información pública recopilada.")
    get_settings.cache_clear()


def test_vapi_speaks_again_when_the_graph_goes_quiet(monkeypatch):
    """Un silencio largo cuelga la llamada: cada pocos segundos se dice algo."""
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("VAPI_FILLER_DELAY_SECONDS", "0.05")
    monkeypatch.setenv("VAPI_IDLE_SPEECH_SECONDS", "0.5")
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(1.4))
    from jarvis.api.vapi_routes import WAIT_PHRASES
    from jarvis.config import get_settings

    get_settings.cache_clear()
    said = [text for _at, text in _said(_collect_sse(session="call-idle"))]
    waits = [text for text in said if text in WAIT_PHRASES]
    assert said[0] in FILLER_PHRASES
    assert len(waits) >= 2, f"con 1.4 s de silencio deben salir varias esperas: {said}"
    assert waits[0] != waits[1], "dos esperas seguidas iguales suenan a bucle"
    get_settings.cache_clear()


def test_vapi_streams_the_executor_tokens_as_they_arrive(monkeypatch):
    """Fase 3 real: la voz empieza la respuesta antes de que el grafo cierre."""
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")

    async def _stream(text, session_id=None):
        for token in ("La temperatura", " es de 22", " grados, maestro."):
            await asyncio.sleep(0.05)
            yield {"kind": "token", "text": token}
        await asyncio.sleep(0.05)
        yield {"kind": "state", "state": {"final_answer": "La temperatura es de 22 grados, maestro."}}

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    from jarvis.config import get_settings

    get_settings.cache_clear()
    events = _collect_sse(session="call-tokens")
    said = _said(events)
    assert said[0][1].startswith("La temperatura")
    assert said[0][0] < events[-1][0], "el primer token no espera al final del grafo"
    # La frase final es el borrador que ya se dijo: repetirla sonaría a tartamudeo.
    assert _spoken(events) == "La temperatura es de 22 grados, maestro."
    get_settings.cache_clear()


def test_vapi_completes_the_answer_the_tokens_left_half_said(monkeypatch):
    """Si `to_spoken` añade algo al borrador, solo se pronuncia lo que falta."""

    async def _stream(text, session_id=None):
        yield {"kind": "token", "text": "Inventario revisado."}
        yield {
            "kind": "state",
            "state": {"final_answer": "Inventario revisado. Tienes 42 en stock."},
        }

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    assert _spoken(_collect_sse(session="call-tail")) == "Inventario revisado. Tienes 42 en stock."


def test_vapi_never_speaks_raw_tool_output(monkeypatch):
    """Un volcado JSON no se pronuncia: habla la frase de `to_spoken`."""

    async def _stream(text, session_id=None):
        yield {"kind": "token", "text": '{"products": [{"title"'}
        yield {"kind": "token", "text": ': "Auriculares"}]}'}
        yield {"kind": "state", "state": {"final_answer": "Inventario revisado."}}

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    spoken = _spoken(_collect_sse(session="call-json"))
    assert spoken == "Inventario revisado."
    assert "{" not in spoken


def test_vapi_drops_the_graph_when_the_call_hangs_up(monkeypatch):
    """Si Vapi cuelga a mitad del stream, el turno se suelta en vez de quedar colgado."""
    marks: list[str] = []

    async def _stream(text, session_id=None):
        try:
            await asyncio.sleep(5)
            yield {"kind": "state", "state": {"final_answer": "tarde"}}
        except asyncio.CancelledError:
            marks.append("cancelado")
            raise

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    from jarvis.api.vapi_routes import _sse_supervisor_reply

    async def scenario() -> None:
        stream = _sse_supervisor_reply("hola", "call-hangup", "chatcmpl-test")
        assert '"role": "assistant"' in await stream.__anext__()
        # Un segundo chunk (la frase puente) obliga al turno a estar ya en marcha.
        assert _sse_content(await stream.__anext__()) in FILLER_PHRASES
        await stream.aclose()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert marks == ["cancelado"]


def test_vapi_keeps_the_socket_during_a_pause_between_tokens(monkeypatch):
    """Tras una tool el LLM se calla un instante: keep-alive, no [DONE] a medias."""
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.1")
    monkeypatch.setenv("VAPI_IDLE_SPEECH_SECONDS", "0.5")
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "10")

    async def _stream(text, session_id=None):
        yield {"kind": "token", "text": "La temperatura es "}
        await asyncio.sleep(0.45)
        yield {"kind": "token", "text": "de 22 grados, maestro."}
        yield {"kind": "state", "state": {"final_answer": "La temperatura es de 22 grados, maestro."}}

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    from jarvis.config import get_settings

    get_settings.cache_clear()
    events = _collect_sse(session="call-pause")
    lines = [line for _at, chunk in events for line in chunk.splitlines() if line.strip()]
    spoken = _spoken(events)
    assert spoken == "La temperatura es de 22 grados, maestro."
    assert ": keep-alive" in lines
    assert lines[-1] == "data: [DONE]"
    done_at = next(i for i, line in enumerate(lines) if line == "data: [DONE]")
    keep_at = next(i for i, line in enumerate(lines) if line == ": keep-alive")
    assert keep_at < done_at, "el keep-alive no puede ir después del cierre"
    get_settings.cache_clear()


def test_vapi_logs_when_vapi_cancels_the_stream(monkeypatch, caplog):
    """GeneratorExit / CancelledError: se anota que cortó Vapi, no el generador."""
    monkeypatch.setenv("VAPI_FILLER_DELAY_SECONDS", "0.05")

    async def _stream(text, session_id=None):
        await asyncio.sleep(5)
        yield {"kind": "state", "state": {"final_answer": "tarde"}}

    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _stream)
    from jarvis.api.vapi_routes import _sse_supervisor_reply
    from jarvis.config import get_settings

    get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="jarvis.api.vapi_routes")

    async def scenario() -> None:
        stream = _sse_supervisor_reply("hola", "call-cancel-log", "chatcmpl-test")
        await stream.__anext__()
        await stream.__anext__()
        await stream.aclose()

    asyncio.run(scenario())
    disconnects = [rec.getMessage() for rec in caplog.records if "[VAPI DISCONNECT]" in rec.getMessage()]
    assert disconnects, "hay que saber si cortó Vapi o el generador"
    assert "call-cancel-log" in disconnects[0]
    get_settings.cache_clear()


def test_unsaid_tail_only_returns_what_is_missing():
    from jarvis.api.vapi_routes import _unsaid_tail

    assert _unsaid_tail("Inventario revisado.", "Inventario revisado.") == ""
    assert _unsaid_tail("Inventario revisado", "Inventario revisado. Hay 3 productos.") == (
        "Hay 3 productos."
    )
    assert _unsaid_tail("", "Listo.") == "Listo."
    assert _unsaid_tail("Hola", "Archivo actualizado.") == "Archivo actualizado."


def test_vapi_webhook_path_always_streams(monkeypatch):
    """El Custom LLM ya no tiene camino JSON: Vapi solo traga SSE."""
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _instant_stream("Listo."))
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {"messages": [{"role": "user", "content": "hola"}]},
    )
    assert status == 200
    assert "text/event-stream" in ctype
    assert _spoken_sse(body) == "Listo."
    assert "data: [DONE]" in body


def test_vapi_sse_answers_before_vapi_times_out(monkeypatch):
    """El stream cierra al vencer el presupuesto, no cuando el Supervisor acaba."""
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.2")
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(4))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    events = _collect_sse()
    lines = [line for _at, chunk in events for line in chunk.splitlines() if line.strip()]
    spoken = _spoken(events)
    assert events[0][0] < 0.5, "el primer chunk debe salir de inmediato"
    assert events[-1][0] < 2.5, "el stream no debe esperar a que el Supervisor termine"
    assert spoken.endswith(SLOW_REPLY)
    assert spoken[: -len(SLOW_REPLY)].strip() in FILLER_PHRASES
    assert ": keep-alive" in lines
    assert '"finish_reason": "stop"' in " ".join(lines)
    assert lines[-1] == "data: [DONE]"
    get_settings.cache_clear()


def test_vapi_webhook_path_also_answers_on_timeout(monkeypatch):
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.2")
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(3))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {"messages": [{"role": "user", "content": "hola"}]},
    )
    assert status == 200
    assert "text/event-stream" in ctype
    assert _spoken_sse(body).endswith(SLOW_REPLY)
    assert "data: [DONE]" in body
    get_settings.cache_clear()


def test_vapi_sse_speaks_the_error_when_the_supervisor_explodes(monkeypatch, caplog):
    """Un fallo interno se dice en voz alta: nunca un stream cortado sin [DONE]."""
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.astream_jarvis",
        _broken_stream(RuntimeError("LangGraph roto")),
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
        "jarvis.api.vapi_routes.astream_jarvis",
        _broken_stream(TimeoutError("una herramienta HTTP expiró")),
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


def test_vapi_webhook_path_speaks_the_error(monkeypatch):
    monkeypatch.setattr(
        "jarvis.api.vapi_routes.astream_jarvis",
        _broken_stream(RuntimeError("LangGraph roto")),
    )
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {"messages": [{"role": "user", "content": "hola"}]},
    )
    assert status == 200
    assert "text/event-stream" in ctype
    assert _spoken_sse(body) == ERROR_REPLY
    assert "data: [DONE]" in body


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


def test_vapi_payload_without_user_message_is_acked_silently(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    res = client.post("/webhooks/vapi-llm", json={"call": {"id": "voice-empty"}})
    assert res.status_code == 200
    assert res.json() == {}
    assert not any("[VAPI INCOMING]" in rec.getMessage() for rec in caplog.records)
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_vapi_logs_a_body_that_is_not_json(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    client = TestClient(create_app())
    res = client.post(
        "/webhooks/vapi-llm",
        content=b"esto no es json",
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 200
    assert res.json() == {}
    assert any("cuerpo no-JSON" in rec.getMessage() for rec in caplog.records)


def test_vapi_logs_the_timeout_before_answering(monkeypatch, caplog):
    monkeypatch.setenv("VAPI_RESPONSE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("VAPI_KEEPALIVE_SECONDS", "0.2")
    monkeypatch.setattr("jarvis.api.vapi_routes.astream_jarvis", _slow_stream(3))
    from jarvis.config import get_settings

    get_settings.cache_clear()
    caplog.set_level(logging.INFO, logger="jarvis.api.vapi_routes")
    status, body, _ctype = _post_sse(
        "/webhooks/vapi-llm",
        {"messages": [{"role": "user", "content": "hola"}]},
    )
    assert status == 200
    assert _spoken_sse(body).endswith(SLOW_REPLY)
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
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {"messages": [{"content": "¿Cuánto es 2 + 2?"}]},
    )
    assert status == 200
    assert "text/event-stream" in ctype
    assert "4" in _spoken_sse(body)


def test_vapi_chat_completions_get_probe():
    client = TestClient(create_app())
    for path in ("/chat/completions", "/v1/chat/completions", "/webhooks/vapi-llm/chat/completions"):
        probe = client.get(path)
        assert probe.status_code == 200, path
        assert probe.json()["status"] == "ok"


def test_vapi_extracts_call_messages():
    status, body, ctype = _post_sse(
        "/webhooks/vapi-llm",
        {"call": {"id": "voice-call-msgs", "messages": [{"role": "user", "content": "¿Cuánto es 2 + 2?"}]}},
    )
    assert status == 200
    assert "text/event-stream" in ctype
    assert "4" in _spoken_sse(body)


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
    header = client.post(
        "/webhooks/vapi-llm",
        headers={"X-Vapi-Secret": "s3cret"},
        json={"messages": [{"role": "user", "content": "¿Cuánto es 2+2?"}]},
    )
    assert header.status_code == 200
    get_settings.cache_clear()


def test_vapi_sse_speaks_the_neural_error_when_a_specialist_crashes(monkeypatch):
    """Con el grafo real: el especialista revienta y Vapi oye la frase amable, con [DONE]."""
    from jarvis.prompts import NEURAL_ERROR_REPLY

    class _Exploding:
        def invoke(self, _payload, _config=None):
            raise RuntimeError("parseo roto dentro del research_agent")

    monkeypatch.setattr("jarvis.agents.base.core_subgraph", lambda: _Exploding())
    client = TestClient(create_app())
    with client.stream(
        "POST",
        "/webhooks/vapi-llm/chat/completions",
        json={
            "stream": True,
            "call": {"id": "voice-crash"},
            "messages": [{"role": "user", "content": "Investiga a Ada Lovelace y recopila información pública"}],
        },
    ) as res:
        assert res.status_code == 200
        lines = [line for line in res.iter_lines() if line.strip()]
    spoken = "".join(_sse_content(line) for line in lines)
    assert spoken.endswith(NEURAL_ERROR_REPLY)
    assert ERROR_REPLY not in spoken, "el fallo se absorbe en el grafo, no en el endpoint"
    assert '"finish_reason": "stop"' in " ".join(lines)
    assert lines[-1] == "data: [DONE]"
