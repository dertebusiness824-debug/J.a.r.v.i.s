"""Custom LLM de Vapi (OpenAI-compatible) → Supervisor LangGraph + Cartesia TTS."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from jarvis.config import get_settings
from jarvis.api.vapi_events import custom_llm_model
from jarvis.integrations.cartesia import CartesiaClient
from jarvis.supervisor import arun_jarvis
from jarvis.voice import spoken_from_state

logger = logging.getLogger(__name__)

router = APIRouter()

# Comentario SSE: el cliente lo ignora, pero mantiene viva la conexión mientras
# LangGraph trabaja, así Vapi no cierra la llamada con "Did Not Receive Response".
KEEPALIVE = ": keep-alive\n\n"
SLOW_REPLY = "Sigo procesando la petición, señor. Dame unos segundos y pregúntame de nuevo."
# Se dice en voz alta a propósito: si Vapi lo pronuncia, el endpoint sí respondió
# y el fallo está dentro del Supervisor (traza completa en el log del servidor).
ERROR_REPLY = "Maestro, ha habido un fallo en la red neuronal. Revisa la terminal del servidor."
# Frases puente. Vapi habla en cuanto recibe el primer delta con texto, así que
# esto convierte el silencio de LangGraph en tiempo de proceso gratis.
FILLER_PHRASES = (
    "Un momento, por favor…",
    "Analizando la directiva…",
    "Accediendo a los sistemas…",
    "Procesando, señor…",
    "Consultando la red neuronal…",
)
# Los payloads de Vapi traen la conversación entera: en el log solo cabe un adelanto.
LOG_PREVIEW_CHARS = 800
# Última frase puente por llamada: repetirla en dos turnos seguidos suena a bucle.
# Es una caché, no un registro: se vacía sola para no crecer con cada llamada.
_LAST_FILLER: dict[str, str] = {}
_FILLER_MEMORY = 256


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


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


async def _supervisor_spoken(user_text: str, session_id: str) -> str:
    """Supervisor LangGraph fuera del event loop, ya convertido a frase hablable."""
    if not user_text:
        return "Sistema listo."
    result = await arun_jarvis(user_text, session_id=f"vapi:{session_id}")
    logger.info(
        "🧠 [VAPI SUPERVISOR] call=%s agente=%s herramientas=%s",
        session_id,
        result.get("active_agent") or "supervisor",
        [str(item.get("tool") or "") for item in (result.get("tool_results") or [])],
    )
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
    filler: str = "",
) -> None:
    logger.info(
        "✅ [VAPI OUTGOING] call=%s stream=%s puente=%r en %.2f s: %r",
        session_id,
        stream,
        filler,
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


def pick_filler(session_id: str) -> str:
    """Frase puente al azar, sin repetir la del turno anterior de esta llamada."""
    previous = _LAST_FILLER.get(session_id)
    options = [phrase for phrase in FILLER_PHRASES if phrase != previous]
    phrase = random.choice(options or list(FILLER_PHRASES))
    if len(_LAST_FILLER) >= _FILLER_MEMORY:
        _LAST_FILLER.clear()
    _LAST_FILLER[session_id] = phrase
    return phrase


def _detach(task: "asyncio.Future[str]") -> None:
    """El hilo del Supervisor no se puede cancelar: consume su resultado tardío."""

    def _drain(done: "asyncio.Future[str]") -> None:
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error("Vapi: el Supervisor falló tras el timeout", exc_info=error)
        else:
            logger.info("Vapi: el Supervisor respondió tarde; se descarta el texto.")

    task.add_done_callback(_drain)


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


async def _sse_supervisor_reply(
    user_text: str,
    session_id: str,
    completion_id: str,
) -> AsyncIterator[str]:
    """Streaming en dos fases para que la llamada nunca se quede muda.

    Fase 1: el Supervisor arranca ya y, si no contesta en el primer parpadeo, sale
    una frase puente como delta de texto. Vapi la pronuncia al instante, lo que
    regala unos segundos de proceso. Fase 2: mientras LangGraph trabaja solo van
    keep-alives. Fase 3: la respuesta real, y siempre `finish_reason` +
    `data: [DONE]`, incluso si algo explota por dentro.
    """
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    started = time.monotonic()
    yield _openai_chunk(
        completion_id=cid,
        created=created,
        delta={"role": "assistant"},
        finish_reason=None,
    )
    spoken = ERROR_REPLY
    filler = ""
    try:
        timeout, beat = _budget()
        task = asyncio.ensure_future(_supervisor_spoken(user_text, session_id))
        deadline = started + timeout
        # El primer tramo es corto a propósito: es el margen para decidir si hace
        # falta la frase puente. A partir de ahí se espera a ritmo de keep-alive.
        slice_seconds = _filler_delay()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _detach(task)
                logger.error(
                    "⚠️ [VAPI TIMEOUT] call=%s el Supervisor superó %.1f s; respondo para no colgar la llamada.",
                    session_id,
                    timeout,
                )
                spoken = SLOW_REPLY
                break
            try:
                spoken = await asyncio.wait_for(
                    asyncio.shield(task), timeout=min(slice_seconds, remaining)
                )
                break
            except asyncio.TimeoutError:
                # `TimeoutError` también puede venir del propio Supervisor (HTTP de
                # una herramienta): si la tarea ya acabó, el resultado manda.
                if task.done():
                    spoken = task.result()
                    break
                if not filler:
                    filler = pick_filler(session_id)
                    logger.info(
                        "🗣️ [VAPI FILLER] call=%s a los %.2f s: %r",
                        session_id,
                        time.monotonic() - started,
                        filler,
                    )
                    yield _openai_chunk(
                        completion_id=cid,
                        created=created,
                        delta={"content": filler},
                        finish_reason=None,
                    )
                else:
                    yield KEEPALIVE
                slice_seconds = beat
                continue
    except Exception:
        logger.exception(
            "💥 [VAPI ERROR] call=%s fallo interno: respondo con voz para no dejar la llamada muda",
            session_id,
        )
        spoken = ERROR_REPLY
    _log_outgoing(session_id, spoken, started, stream=True, filler=filler)
    if filler:
        # Separador entre la frase puente y la respuesta: sin él el TTS las pega.
        yield _openai_chunk(
            completion_id=cid,
            created=created,
            delta={"content": " "},
            finish_reason=None,
        )
    async for chunk in _sse_openai_chunks(
        spoken, completion_id=cid, created=created, include_role=False
    ):
        yield chunk


def _authorized(request: Request) -> bool:
    secret = get_settings().vapi_webhook_secret
    if not secret:
        return True
    header = request.headers.get("authorization") or ""
    return header in {f"Bearer {secret}", secret}


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


async def _vapi_llm_reply(request: Request, *, force_stream: bool = False):
    """Ejecuta el Supervisor y responde JSON OpenAI o SSE."""
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="Vapi webhook no autorizado")
    try:
        data = await _vapi_payload(request)
        user_text, session_id, stream = extract_user_turn(data)
        if not user_text:
            user_text = last_user_content(data)
        stream = force_stream or stream
        _log_incoming(data, user_text, session_id, stream=stream)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        if stream:
            return StreamingResponse(
                _sse_supervisor_reply(user_text, session_id, completion_id),
                media_type="text/event-stream",
                headers=_sse_headers(),
            )
        spoken = await _spoken_within_budget(user_text, session_id)
        return openai_completion(spoken, completion_id=completion_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error en Vapi Custom LLM")
        if force_stream:
            return StreamingResponse(
                _sse_openai_chunks(
                    ERROR_REPLY,
                    completion_id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
                ),
                media_type="text/event-stream",
                headers=_sse_headers(),
            )
        return openai_completion(ERROR_REPLY)


@router.post("/webhooks/vapi-llm", tags=["vapi"])
async def vapi_custom_llm(request: Request):
    """Custom LLM de Vapi: payload OpenAI o message.messages → Supervisor LangGraph."""
    return await _vapi_llm_reply(request, force_stream=False)


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
        "firstMessage": "Sistemas en línea. No hay mensajes pendientes, señor.",
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
