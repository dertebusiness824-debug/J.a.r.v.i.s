"""Custom LLM de Vapi (OpenAI-compatible) → Supervisor LangGraph + Cartesia TTS."""

from __future__ import annotations

import asyncio
import json
import logging
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
from jarvis.supervisor import run_jarvis
from jarvis.voice import spoken_from_state

logger = logging.getLogger(__name__)

router = APIRouter()


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
    for word in text.split(" "):
        piece = word if not buf else f" {word}"
        buf += piece
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


async def _sse_supervisor_reply(
    user_text: str,
    session_id: str,
    completion_id: str,
) -> AsyncIterator[str]:
    """Primer chunk inmediato (Vapi no corta) y luego el Supervisor."""
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    yield _openai_chunk(
        completion_id=cid,
        created=created,
        delta={"role": "assistant"},
        finish_reason=None,
    )
    try:
        if not user_text:
            spoken = "Sistema listo."
        else:
            result = await asyncio.to_thread(run_jarvis, user_text, session_id=f"vapi:{session_id}")
            spoken = spoken_from_state(result)
    except Exception:
        logger.exception("Error en Vapi Custom LLM")
        spoken = "He encontrado un error de procesamiento."
    async for chunk in _sse_openai_chunks(
        spoken, completion_id=cid, created=created, include_role=False
    ):
        yield chunk


async def _sse_chunks(content: str, *, completion_id: str) -> AsyncIterator[str]:
    async for chunk in _sse_openai_chunks(content, completion_id=completion_id):
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


async def _vapi_llm_reply(request: Request, *, force_stream: bool = False):
    """Ejecuta el Supervisor y responde JSON OpenAI o SSE."""
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="Vapi webhook no autorizado")
    try:
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        user_text, session_id, stream = extract_user_turn(data)
        if not user_text:
            user_text = last_user_content(data)
        stream = force_stream or stream
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        if stream:
            return StreamingResponse(
                _sse_supervisor_reply(user_text, session_id, completion_id),
                media_type="text/event-stream",
                headers=_sse_headers(),
            )
        if not user_text:
            spoken = "Sistema listo."
        else:
            result = await asyncio.to_thread(run_jarvis, user_text, session_id=f"vapi:{session_id}")
            spoken = spoken_from_state(result)
        return openai_completion(spoken, completion_id=completion_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error en Vapi Custom LLM")
        if force_stream:
            return StreamingResponse(
                _sse_openai_chunks(
                    "He encontrado un error de procesamiento.",
                    completion_id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
                ),
                media_type="text/event-stream",
                headers=_sse_headers(),
            )
        return openai_completion("He encontrado un error de procesamiento.")


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
