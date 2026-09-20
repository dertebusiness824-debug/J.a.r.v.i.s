"""Custom LLM de Vapi (OpenAI-compatible) → Supervisor LangGraph + Cartesia TTS."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from jarvis.config import get_settings
from jarvis.api.vapi_auth import authorized_vapi_request
from jarvis.api.vapi_events import custom_llm_model
from jarvis.integrations.cartesia import CartesiaClient
from jarvis.supervisor import arun_jarvis, astream_jarvis
from jarvis.voice import narrate_tool, spoken_from_state, strip_markdown

logger = logging.getLogger(__name__)

router = APIRouter()

# Comentario SSE: el cliente lo ignora, pero mantiene viva la conexión mientras
# LangGraph trabaja, así Vapi no cierra la llamada con "Did Not Receive Response".
KEEPALIVE = ": keep-alive\n\n"
SLOW_REPLY = "Sigo procesando la petición, maestro. Deme unos segundos y pregúnteme de nuevo."
# Se dice en voz alta a propósito: si Vapi lo pronuncia, el endpoint sí respondió
# y el fallo está dentro del Supervisor (traza completa en el log del servidor).
ERROR_REPLY = "Mis sistemas han fallado, maestro. Revise los registros del servidor."
# Frases puente. Vapi habla en cuanto recibe el primer delta con texto, así que
# esto convierte el silencio de LangGraph en tiempo de proceso gratis.
FILLER_PHRASES = (
    "Un momento, por favor…",
    "Analizando la directiva…",
    "Accediendo a los sistemas…",
    "Procesando, maestro…",
    "Consultando la red neuronal…",
)
# Para cuando el grafo lleva un rato sin dar señales (una herramienta lenta que ya
# se narró, un LLM pensando). Callarse es lo único que Vapi no perdona.
WAIT_PHRASES = (
    "Sigo en ello, maestro…",
    "Un instante más, maestro…",
    "Casi lo tengo, maestro…",
    "Continúo con el análisis, maestro…",
)
# Dos narraciones seguidas suenan a atropello: el research_agent encadena varias
# herramientas en menos de un segundo.
NARRATION_MIN_GAP_SECONDS = 2.0
# La narración espera un poco antes de salir: una herramienta que termina en este
# tiempo no necesita presentación, y anunciarla alargaría la respuesta sin motivo.
NARRATION_DELAY_SECONDS = 0.5
# Los payloads de Vapi traen la conversación entera: en el log solo cabe un adelanto.
LOG_PREVIEW_CHARS = 800
# Última frase dicha por llamada y pozo: repetirla seguida suena a bucle.
# Es una caché, no un registro: se vacía sola para no crecer con cada llamada.
_LAST_PHRASE: dict[str, str] = {}
_PHRASE_MEMORY = 256


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


# Eventos de control / metadatos del Server URL. Si Vapi los manda al Custom LLM
# (mala URL o metadataSendMode) no son un turno: un "Sistema listo." síncrono
# rompe la llamada. Se contestan con 200 vacío y no se toca LangGraph.
_NOISE_EVENT_TYPES = frozenset(
    {
        "speech-update",
        "status-update",
        "transcript",
        "conversation-update",
        "hang",
        "end-of-call-report",
        "user-interrupted",
        "language-changed",
        "metadata",
        "model-output",
        "voice-input",
        "recording",
        "transfer-update",
    }
)
# Server URL que sí pide una respuesta hablada o de herramienta.
_TURN_EVENT_TYPES = frozenset({"assistant-request", "function-call", "tool-calls"})


def vapi_message_type(payload: dict[str, Any]) -> str:
    """`message.type` de Vapi; vacío en el Custom LLM al estilo OpenAI."""
    nested = payload.get("message")
    if isinstance(nested, dict) and nested.get("type"):
        return str(nested["type"])
    return str(payload.get("type") or "")


def is_vapi_noise_event(payload: dict[str, Any]) -> bool:
    """True si el JSON es un evento de control/metadatos, no un turno del usuario."""
    tipo = vapi_message_type(payload)
    if tipo in _NOISE_EVENT_TYPES:
        return True
    if tipo and tipo not in _TURN_EVENT_TYPES:
        return True
    return False


def extract_user_turn(payload: dict[str, Any]) -> tuple[str, str, bool]:
    """Soporta Custom LLM (messages[]) y server-URL Vapi (message.messages)."""
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    session = str(
        call.get("id")
        or payload.get("callId")
        or payload.get("session_id")
        or "vapi"
    )
    stream = bool(payload.get("stream"))
    messages = payload.get("messages")
    nested = payload.get("message")
    if not messages and isinstance(nested, dict):
        messages = nested.get("messages")
        stream = stream or bool(nested.get("stream"))
    if not messages and isinstance(call, dict):
        messages = call.get("messages")
    if not isinstance(messages, list) or not messages:
        return "", session, stream

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or msg.get("type") or "")
        content = msg.get("content")
        if role not in {"user", "human"}:
            continue
        text = _flatten_content(content)
        if text:
            return text, session, stream
    last = messages[-1]
    if isinstance(last, dict):
        return _flatten_content(last.get("content")), session, stream
    return "", session, stream


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                chunks.append(str(part["text"]))
            elif isinstance(part, str):
                chunks.append(part)
        return " ".join(chunks).strip()
    return str(content or "").strip()


def last_user_content(payload: dict[str, Any]) -> str:
    """Último turno de usuario al estilo OpenAI (`messages[-1].content`)."""
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            text = _flatten_content(last.get("content"))
            if text:
                return text
    text, _session, _stream = extract_user_turn(payload)
    return text


def openai_completion(content: str, *, completion_id: str | None = None) -> dict[str, Any]:
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "jarvis-supervisor",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _text_fragments(content: str) -> list[str]:
    text = (content or "").strip()
    if not text:
        return [" "]
    parts: list[str] = []
    buf = ""
    # El espacio viaja con la palabra siguiente: unir los deltas debe reproducir el texto.
    for index, word in enumerate(text.split(" ")):
        buf += word if index == 0 else f" {word}"
        if len(buf) >= 28:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return parts


def _openai_chunk(
    *,
    completion_id: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "jarvis-supervisor",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _sse_openai_chunks(
    content: str,
    *,
    completion_id: str,
    created: int | None = None,
    include_role: bool = True,
) -> AsyncIterator[str]:
    """SSE OpenAI: deltas + finish_reason stop + data: [DONE] (lo que Vapi consume)."""
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(created if created is not None else time.time())
    first = True
    for fragment in _text_fragments(content):
        delta: dict[str, Any] = {"content": fragment}
        if first and include_role:
            delta["role"] = "assistant"
            first = False
        elif first:
            first = False
        yield _openai_chunk(completion_id=cid, created=created, delta=delta, finish_reason=None)
    yield _openai_chunk(completion_id=cid, created=created, delta={}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _log_supervisor(session_id: str, state: dict[str, Any]) -> None:
    logger.info(
        "🧠 [VAPI SUPERVISOR] call=%s agente=%s herramientas=%s",
        session_id,
        state.get("active_agent") or "supervisor",
        [str(item.get("tool") or "") for item in (state.get("tool_results") or [])],
    )


async def _supervisor_spoken(user_text: str, session_id: str) -> str:
    """Supervisor LangGraph fuera del event loop, ya convertido a frase hablable."""
    if not user_text:
        return "Sistema listo."
    result = await arun_jarvis(user_text, session_id=f"vapi:{session_id}")
    _log_supervisor(session_id, result)
    return spoken_from_state(result)


def _preview(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(value)
    return raw[:LOG_PREVIEW_CHARS] + ("…" if len(raw) > LOG_PREVIEW_CHARS else "")


def _log_incoming(payload: dict[str, Any], user_text: str, session_id: str, *, stream: bool) -> None:
    """Primera traza del turno: si esto no sale en Render, la petición no llegó."""
    if user_text:
        logger.info(
            "🔥 [VAPI INCOMING] call=%s stream=%s mensaje=%r",
            session_id,
            stream,
            user_text,
        )
        return
    # Sin texto no hay turno que procesar: el payload completo dice qué mandó Vapi.
    # Un payload vacío ya se registró al leer el cuerpo, así que basta con INFO.
    level = logging.INFO if not payload else logging.WARNING
    logger.log(
        level,
        "🔥 [VAPI INCOMING] call=%s stream=%s sin mensaje de usuario; payload=%s",
        session_id,
        stream,
        _preview(payload),
    )


def _log_outgoing(
    session_id: str,
    spoken: str,
    started: float,
    *,
    stream: bool,
    bridge: str = "",
) -> None:
    logger.info(
        "✅ [VAPI OUTGOING] call=%s stream=%s puente=%r en %.2f s: %r",
        session_id,
        stream,
        bridge,
        time.monotonic() - started,
        spoken,
    )


def _budget() -> tuple[float, float]:
    settings = get_settings()
    return (
        max(float(settings.vapi_response_timeout_seconds), 1.0),
        max(float(settings.vapi_keepalive_seconds), 0.1),
    )


def _filler_delay() -> float:
    return max(float(get_settings().vapi_filler_delay_seconds), 0.0)


def _idle_speech_gap() -> float:
    return max(float(get_settings().vapi_idle_speech_seconds), 0.5)


def _heartbeat_gap() -> float:
    """Pausa máxima sin emitir nada: un comentario SSE mantiene el socket.

    El `VAPI_KEEPALIVE_SECONDS` de 5 s es demasiado para Vapi si el LLM se
    calla entre tokens (tras una tool). Se recorta a 0,5 s: no es voz, pero
    tampoco se manda `finish_reason`/`[DONE]` — eso solo sale al cerrar.
    """
    return min(max(float(get_settings().vapi_keepalive_seconds), 0.1), 0.5)


def _pick_phrase(pool: tuple[str, ...], key: str) -> str:
    previous = _LAST_PHRASE.get(key)
    options = [phrase for phrase in pool if phrase != previous]
    phrase = random.choice(options or list(pool))
    if len(_LAST_PHRASE) >= _PHRASE_MEMORY:
        _LAST_PHRASE.clear()
    _LAST_PHRASE[key] = phrase
    return phrase


def pick_filler(session_id: str) -> str:
    """Frase puente al azar, sin repetir la del turno anterior de esta llamada."""
    return _pick_phrase(FILLER_PHRASES, f"filler:{session_id}")


def pick_wait_phrase(session_id: str) -> str:
    """Frase de espera para cuando el grafo se queda mudo a mitad del turno."""
    return _pick_phrase(WAIT_PHRASES, f"wait:{session_id}")


class TokenGate:
    """Deja pasar los tokens del ejecutor solo si son prosa pronunciable.

    El ejecutor redacta la respuesta, pero también puede volcar la salida cruda de
    una herramienta (JSON, HTML, un bloque de código). Se retienen los primeros
    caracteres para decidirlo; si la cosa pinta a datos se descarta el turno entero
    y habla la frase final, que sí pasa por `to_spoken`.
    """

    LOOKAHEAD = 6
    BLOCKED_STARTS = ("{", "[", "```", "<")

    def __init__(self) -> None:
        self._buffer = ""
        self._open = False
        self._blocked = False
        self.spoken = ""

    def feed(self, token: str) -> str:
        """Texto que se puede hablar ya; cadena vacía mientras aún no se sabe."""
        if self._blocked or not token:
            return ""
        if self._open:
            self.spoken += token
            return token
        self._buffer += token
        if len(self._buffer.strip()) < self.LOOKAHEAD:
            return ""
        text = self._buffer
        self._buffer = ""
        if text.lstrip().startswith(self.BLOCKED_STARTS):
            self._blocked = True
            return ""
        self._open = True
        self.spoken = text
        return text


_WORD_EDGES = re.compile(r"[^0-9a-záéíóúüñ]+")


def _speech_words(text: str) -> list[tuple[str, str]]:
    """Palabras del texto emparejadas con su forma comparable (sin tildes de puntuación)."""
    pairs = []
    for raw in strip_markdown(text or "").split():
        norm = _WORD_EDGES.sub("", raw.lower())
        if norm:
            pairs.append((raw, norm))
    return pairs


def _unsaid_tail(streamed: str, final: str) -> str:
    """Lo que queda por decir de la frase final tras haber hablado token a token.

    El borrador del ejecutor suele ser ya la respuesta, así que repetirla entera
    sonaría a tartamudeo. Se busca el solapamiento entre el final de lo hablado y
    el principio de la frase definitiva, y solo se pronuncia el resto.
    """
    said = [norm for _raw, norm in _speech_words(streamed)]
    target = _speech_words(final)
    if not target:
        return ""
    if not said:
        return final
    for size in range(min(len(said), len(target)), 0, -1):
        if said[-size:] == [norm for _raw, norm in target[:size]]:
            return " ".join(raw for raw, _norm in target[size:])
    return final


def _detach(task: asyncio.Future[str]) -> None:
    """El hilo del Supervisor no se puede cancelar: consume su resultado tardío."""

    def _drain(done: asyncio.Future[str]) -> None:
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error("Vapi: el Supervisor falló tras el timeout", exc_info=error)
        else:
            logger.info("Vapi: el Supervisor respondió tarde; se descarta el texto.")

    task.add_done_callback(_drain)


def _abandon(task: asyncio.Future[None], session_id: str) -> None:
    """Deja de escuchar al grafo: su hilo seguirá, pero el turno ya se contestó."""

    def _note(done: asyncio.Future[None]) -> None:
        error = None if done.cancelled() else done.exception()
        if error is not None:
            logger.error("Vapi: call=%s el Supervisor falló tras el timeout", session_id, exc_info=error)
        else:
            logger.info("Vapi: call=%s se descarta el turno tardío del Supervisor.", session_id)

    task.add_done_callback(_note)
    task.cancel()


async def _spoken_within_budget(user_text: str, session_id: str) -> str:
    """Camino JSON: misma frase hablable, con el mismo tope de tiempo que el SSE."""
    started = time.monotonic()
    timeout, _beat = _budget()
    task = asyncio.ensure_future(_supervisor_spoken(user_text, session_id))
    try:
        spoken = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        if task.done():
            spoken = task.result()
        else:
            _detach(task)
            logger.error(
                "⚠️ [VAPI TIMEOUT] call=%s el Supervisor superó %.1f s; respondo para no colgar la llamada.",
                session_id,
                timeout,
            )
            spoken = SLOW_REPLY
    _log_outgoing(session_id, spoken, started, stream=False)
    return spoken


async def _speech_events(
    user_text: str,
    session_id: str,
    queue: asyncio.Queue[dict[str, Any] | None],
) -> None:
    """Pasa a la cola lo que el grafo va produciendo; cierra siempre con el centinela.

    Va en su propia tarea para que el generador SSE conserve el reloj: así puede
    hablar, mandar heartbeats o rendirse sin depender de cuándo conteste LangGraph.
    """
    try:
        if not user_text:
            queue.put_nowait({"kind": "answer", "text": "Sistema listo."})
            return
        async for event in astream_jarvis(user_text, session_id=f"vapi:{session_id}"):
            kind = str(event.get("kind") or "")
            if kind == "tool":
                queue.put_nowait({"kind": "tool", "tool": str(event.get("tool") or "")})
            elif kind == "token":
                queue.put_nowait({"kind": "token", "text": str(event.get("text") or "")})
            elif kind == "state":
                state = event.get("state") or {}
                _log_supervisor(session_id, state)
                queue.put_nowait({"kind": "answer", "text": spoken_from_state(state)})
    except asyncio.CancelledError:
        logger.warning(
            "🔌 [VAPI DISCONNECT] call=%s la bomba del grafo se canceló (Vapi colgó o el generador se cerró).",
            session_id,
        )
        raise
    except Exception:
        logger.exception(
            "💥 [VAPI ERROR] call=%s fallo interno: respondo con voz para no dejar la llamada muda",
            session_id,
        )
        queue.put_nowait({"kind": "error"})
    finally:
        queue.put_nowait(None)


def _needs_space(previous: str, following: str) -> bool:
    """¿Hace falta un separador? Sin él el TTS diría "directiva…Listo"."""
    if not previous or not following:
        return False
    return not previous[-1].isspace() and not following[0].isspace()


async def _sse_supervisor_reply(
    user_text: str,
    session_id: str,
    completion_id: str,
) -> AsyncIterator[str]:
    """Stream hablado en tiempo real mientras el grafo trabaja.

    Vapi habla en cuanto recibe un delta con texto, así que el turno se cuenta en
    voz alta a medida que ocurre: frase puente si el grafo no contesta en el primer
    parpadeo, narración cuando arranca una herramienta lenta, los tokens del
    ejecutor según los escribe y, al final, la frase de `to_spoken` (solo la parte
    que no se haya dicho ya). Si nada de eso llega, cada pocos segundos sale una
    frase de espera: un comentario SSE mantiene el socket, pero el silencio corta
    la llamada. Siempre se cierra con `finish_reason` y `data: [DONE]`, incluso si
    algo explota por dentro.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    pump = asyncio.ensure_future(_speech_events(user_text, session_id, queue))
    try:
        async for chunk in _sse_from_events(queue, session_id, completion_id):
            yield chunk
    except GeneratorExit:
        logger.warning(
            "🔌 [VAPI DISCONNECT] call=%s Vapi cerró el stream (GeneratorExit); no mando [DONE].",
            session_id,
        )
        raise
    except asyncio.CancelledError:
        logger.warning(
            "🔌 [VAPI DISCONNECT] call=%s el generador SSE se canceló (CancelledError); no mando [DONE].",
            session_id,
        )
        raise
    finally:
        # Vale tanto para el timeout como para una llamada que se cuelga a medias:
        # el hilo del grafo seguirá, pero aquí ya no queda nadie escuchando.
        if not pump.done():
            _abandon(pump, session_id)


async def _sse_from_events(
    queue: asyncio.Queue[dict[str, Any] | None],
    session_id: str,
    completion_id: str,
) -> AsyncIterator[str]:
    """Convierte la cola de eventos del grafo en chunks SSE, marcando el ritmo."""
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    started = time.monotonic()
    yield _openai_chunk(
        completion_id=cid,
        created=created,
        delta={"role": "assistant"},
        finish_reason=None,
    )

    def _say(text: str) -> str:
        return _openai_chunk(
            completion_id=cid,
            created=created,
            delta={"content": text},
            finish_reason=None,
        )

    timeout, _beat = _budget()
    filler_delay = _filler_delay()
    idle_gap = _idle_speech_gap()
    heartbeat = _heartbeat_gap()
    deadline = started + timeout
    gate = TokenGate()
    bridge: list[str] = []
    tail_char = ""
    last_speech = started
    # Arranca a cero, no en `started`: la primera herramienta suele dispararse en el
    # primer segundo y es justo lo que hay que contar.
    last_narration = 0.0
    # Narración en cola: (frase, cuándo toca decirla, herramienta).
    pending: tuple[str, float, str] | None = None
    final = ERROR_REPLY
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                logger.error(
                    "⚠️ [VAPI TIMEOUT] call=%s el Supervisor superó %.1f s; respondo para no colgar la llamada.",
                    session_id,
                    timeout,
                )
                final = SLOW_REPLY
                break
            # Antes de la primera palabra manda el margen de la frase puente;
            # después, el silencio máximo que una llamada tolera.
            target = (started + filler_delay) if not tail_char else last_speech + idle_gap
            if pending is not None:
                target = min(target, pending[1])
            try:
                item = await asyncio.wait_for(
                    queue.get(),
                    timeout=max(0.02, min(heartbeat, target - now, deadline - now)),
                )
            except asyncio.TimeoutError:
                now = time.monotonic()
                if pending is not None and now + 0.01 >= pending[1]:
                    phrase, _due, tool = pending
                    pending = None
                    logger.info(
                        "🛰️ [VAPI NARRATION] call=%s herramienta=%s: %r",
                        session_id,
                        tool,
                        phrase,
                    )
                    if _needs_space(tail_char, phrase):
                        yield _say(" ")
                    yield _say(phrase)
                    bridge.append(phrase)
                    tail_char = phrase[-1]
                    last_speech = now
                    last_narration = now
                    continue
                if now + 0.01 < target or gate.spoken:
                    # Pausa del LLM (p.ej. entre tokens tras una tool): keep-alive,
                    # nunca finish_reason ni [DONE]. Si ya hay prosa del ejecutor
                    # no se mete un «Sigo en ello» a mitad de frase.
                    yield KEEPALIVE
                    continue
                phrase = pick_wait_phrase(session_id) if tail_char else pick_filler(session_id)
                logger.info(
                    "🗣️ [VAPI FILLER] call=%s a los %.2f s: %r",
                    session_id,
                    now - started,
                    phrase,
                )
                if _needs_space(tail_char, phrase):
                    yield _say(" ")
                yield _say(phrase)
                bridge.append(phrase)
                tail_char = phrase[-1]
                last_speech = now
                continue

            if item is None:
                break
            kind = str(item.get("kind") or "")
            if kind == "error":
                final = ERROR_REPLY
                break
            if kind == "answer":
                final = str(item.get("text") or "")
                break
            if kind == "tool":
                tool = str(item.get("tool") or "")
                phrase = narrate_tool(tool)
                now = time.monotonic()
                if (
                    not phrase
                    or pending is not None
                    or phrase in bridge
                    or now - last_narration < NARRATION_MIN_GAP_SECONDS
                ):
                    continue
                # No se dice aún: si la herramienta contesta rápido, no se dirá.
                pending = (phrase, now + NARRATION_DELAY_SECONDS, tool)
                continue
            if kind == "token":
                text = gate.feed(str(item.get("text") or ""))
                if not text:
                    continue
                if _needs_space(tail_char, text):
                    yield _say(" ")
                yield _say(text)
                tail_char = text[-1]
                last_speech = time.monotonic()
    except GeneratorExit:
        logger.warning(
            "🔌 [VAPI DISCONNECT] call=%s Vapi abortó el SSE a mitad de frase (GeneratorExit).",
            session_id,
        )
        raise
    except asyncio.CancelledError:
        logger.warning(
            "🔌 [VAPI DISCONNECT] call=%s el SSE se canceló a mitad de frase; no mando [DONE].",
            session_id,
        )
        raise
    except Exception:
        logger.exception(
            "💥 [VAPI ERROR] call=%s fallo interno: respondo con voz para no dejar la llamada muda",
            session_id,
        )
        final = ERROR_REPLY

    # Los tokens del ejecutor ya suelen ser la respuesta: solo se pronuncia lo que
    # la frase final añade, para no decir dos veces lo mismo.
    remaining = _unsaid_tail(gate.spoken, final) if gate.spoken else final
    _log_outgoing(session_id, final, started, stream=True, bridge=" ".join(bridge))
    if not remaining:
        yield _openai_chunk(completion_id=cid, created=created, delta={}, finish_reason="stop")
        yield "data: [DONE]\n\n"
        return
    if _needs_space(tail_char, remaining):
        yield _say(" ")
    async for chunk in _sse_openai_chunks(
        remaining, completion_id=cid, created=created, include_role=False
    ):
        yield chunk


async def _authorized(request: Request) -> bool:
    return authorized_vapi_request(request, body=await request.body(), strict=False)


@router.get("/webhooks/vapi-llm", tags=["vapi"])
def vapi_llm_probe() -> dict[str, str]:
    return {"status": "ok", "model": "jarvis-supervisor"}


async def _vapi_payload(request: Request) -> dict[str, Any]:
    """JSON de Vapi; un cuerpo ilegible se registra en vez de perderse en silencio."""
    try:
        data = await request.json()
    except Exception:
        raw = await request.body()
        logger.warning(
            "🔥 [VAPI INCOMING] cuerpo no-JSON en %s: %s",
            request.url.path,
            _preview(raw.decode("utf-8", "replace")),
        )
        return {}
    if isinstance(data, dict):
        return data
    logger.warning("🔥 [VAPI INCOMING] JSON inesperado en %s: %s", request.url.path, _preview(data))
    return {}


def _empty_vapi_ack() -> JSONResponse:
    """200 vacío: Vapi no debe oír 'Sistema listo.' en un speech-update."""
    return JSONResponse(content={}, status_code=200)


async def _vapi_llm_reply(request: Request, *, force_stream: bool = True):
    """Custom LLM: ignora el ruido de Vapi; el turno del usuario sale en SSE."""
    if not await _authorized(request):
        raise HTTPException(status_code=401, detail="Vapi webhook no autorizado")
    try:
        data = await _vapi_payload(request)
        if is_vapi_noise_event(data):
            return _empty_vapi_ack()
        user_text, session_id, _requested_stream = extract_user_turn(data)
        if not user_text:
            user_text = last_user_content(data)
        if not user_text:
            # Sin turno no se inventa una frase: eso era lo que cortaba la llamada
            # cuando llegaba un status-update o un POST vacío.
            return _empty_vapi_ack()
        _log_incoming(data, user_text, session_id, stream=True)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        return StreamingResponse(
            _sse_supervisor_reply(user_text, session_id, completion_id),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error en Vapi Custom LLM")
        return StreamingResponse(
            _sse_openai_chunks(
                ERROR_REPLY,
                completion_id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )


@router.post("/webhooks/vapi-llm", tags=["vapi"])
async def vapi_custom_llm(request: Request):
    """Custom LLM de Vapi: solo el turno del usuario; siempre SSE."""
    return await _vapi_llm_reply(request, force_stream=True)


@router.get("/webhooks/vapi-llm/chat/completions", tags=["vapi"])
@router.get("/webhooks/vapi-llm/v1/chat/completions", tags=["vapi"])
@router.get("/v1/chat/completions", tags=["vapi"])
@router.get("/chat/completions", tags=["vapi"])
def vapi_chat_completions_probe() -> dict[str, str]:
    return {"status": "ok", "model": "jarvis-supervisor"}


@router.post("/webhooks/vapi-llm/chat/completions", tags=["vapi"])
@router.post("/webhooks/vapi-llm/chat/completions/", tags=["vapi"], include_in_schema=False)
@router.post("/webhooks/vapi-llm/v1/chat/completions", tags=["vapi"])
@router.post("/webhooks/vapi-llm/v1/chat/completions/", tags=["vapi"], include_in_schema=False)
@router.post("/v1/chat/completions", tags=["vapi"])
@router.post("/v1/chat/completions/", tags=["vapi"], include_in_schema=False)
@router.post("/chat/completions", tags=["vapi"])
@router.post("/chat/completions/", tags=["vapi"], include_in_schema=False)
async def vapi_chat_completions(request: Request):
    """Rutas OpenAI que Vapi llama (base + /chat/completions o /v1/chat/completions)."""
    return await _vapi_llm_reply(request, force_stream=True)


@router.get("/voice/config", tags=["vapi"])
def voice_config(request: Request) -> dict[str, Any]:
    """Claves públicas para el botón HABLAR del HUD (Vapi Web SDK)."""
    settings = get_settings()
    base = (settings.jarvis_public_url or str(request.base_url).rstrip("/")).rstrip("/")
    public_key = settings.vapi_public_key or ""
    llm_url = f"{base}/webhooks/vapi-llm"
    cartesia_ok = bool(settings.cartesia_api_key and settings.cartesia_voice_id)
    return {
        "custom_llm_path": "/webhooks/vapi-llm",
        "custom_llm_url": llm_url,
        "custom_llm_model": custom_llm_model(llm_url),
        "greeting_path": "/api/jarvis/vapi-events",
        "vapi_public_key": public_key,
        "vapi_public_key_configured": bool(public_key),
        "vapi_assistant_id": settings.vapi_assistant_id or "",
        "talk_enabled": bool(public_key),
        "cartesia_configured": cartesia_ok,
        "cartesia_voice_id": settings.cartesia_voice_id or "",
        "cartesia_model": settings.cartesia_model,
    }


@router.get("/voice/vapi-assistant", tags=["vapi"])
def vapi_assistant_blueprint(request: Request) -> dict[str, Any]:
    """JSON para pegar en el dashboard de Vapi: Custom LLM + voz Cartesia."""
    settings = get_settings()
    base = str(request.base_url).rstrip("/")
    return {
        "name": "J.A.R.V.I.S.",
        "model": custom_llm_model(f"{base}/webhooks/vapi-llm"),
        "voice": {
            "provider": "cartesia",
            "voiceId": settings.cartesia_voice_id or "<CARTESIA_VOICE_ID>",
            "model": settings.cartesia_model,
        },
        "transcriber": {"provider": "deepgram", "language": "es"},
        "serverUrl": f"{base}/api/jarvis/vapi-events",
        "firstMessage": "Sistemas en línea. No hay mensajes pendientes, maestro.",
    }


@router.post("/voice/tts", tags=["vapi"])
def voice_tts(payload: TtsRequest) -> Response:
    client = CartesiaClient()
    if not client.configured:
        raise HTTPException(
            status_code=503,
            detail="Cartesia no configurado. Usa el TTS del navegador o define CARTESIA_API_KEY.",
        )
    audio = client.synthesize(payload.text)
    return Response(content=audio, media_type="audio/wav")
