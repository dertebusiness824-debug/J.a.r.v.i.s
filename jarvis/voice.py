"""Respuestas hablables para Vapi/Cartesia: sin Markdown y en una o dos frases."""

from __future__ import annotations

import json
import re
from typing import Any

from jarvis.agent_core import extract_answer
from jarvis.state import AgentState

_FENCE = re.compile(r"```[\s\S]*?```")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD = re.compile(r"[*_#>`~]+")
_SPACE = re.compile(r"\s+")


def strip_markdown(text: str) -> str:
    cleaned = _FENCE.sub(" ", text or "")
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _MD.sub("", cleaned)
    return _SPACE.sub(" ", cleaned).strip()


def _tool_names(tool_results: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("tool") or "") for item in (tool_results or [])]


def _parse_json(blob: str) -> Any | None:
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None


def _shop_spoken(tool_results: list[dict[str, Any]]) -> str | None:
    for item in reversed(tool_results):
        name = str(item.get("tool") or "")
        if not name.startswith("shopify"):
            continue
        data = _parse_json(str(item.get("output") or ""))
        if not isinstance(data, dict):
            return "Inventario revisado."
        products = data.get("products")
        if isinstance(products, list) and products:
            first = products[0] if isinstance(products[0], dict) else {}
            title = str(first.get("title") or "producto")
            stock = first.get("totalInventory")
            extra = f", {stock} en stock" if stock is not None else ""
            if len(products) == 1:
                return f"Inventario revisado. Tienes {title}{extra}."
            return f"Inventario revisado. Hay {len(products)} productos. El primero es {title}{extra}."
        if "totalInventory" in data:
            return f"Inventario revisado. Stock total {data['totalInventory']}."
        if name == "shopify_list_orders":
            return "Pedidos revisados."
        return "Inventario revisado."
    return None


def to_spoken(
    answer: str,
    *,
    tool_results: list[dict[str, Any]] | None = None,
    agent: str | None = None,
) -> str:
    """Convierte la salida del Supervisor en una frase para TTS (Cartesia/Vapi)."""
    results = list(tool_results or [])
    names = _tool_names(results)
    shop = _shop_spoken(results)
    parts: list[str] = []
    if "write_file" in names or "run_terminal" in names:
        parts.append("Archivo actualizado." if "write_file" in names else "Comando ejecutado.")
    if shop:
        parts.append(shop)
    if "send_whatsapp_message" in names:
        parts.append("WhatsApp enviado.")
    if "send_zadarma_sms" in names:
        parts.append("SMS enviado.")
    if any(
        name in names
        for name in ("web_search", "extract_social_profiles", "find_public_emails")
    ):
        parts.append("Información pública recopilada.")
    if "calculate_expression" in names:
        value = None
        for item in reversed(results):
            if item.get("tool") == "calculate_expression" and item.get("ok"):
                value = str(item.get("output") or "").strip()
                break
        if value and not value.lower().startswith("error"):
            parts.append(f"El resultado es {value}.")
    if "get_current_time" in names and not parts:
        stamp = strip_markdown(answer)
        parts.append(f"Hora actual: {stamp}." if stamp else "Hora consultada.")
    if parts:
        spoken = " ".join(parts)
        return strip_markdown(spoken)

    cleaned = strip_markdown(answer)
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", cleaned or ""):
        return f"El resultado es {cleaned}."
    if not cleaned:
        return "Listo."
    if len(cleaned) > 280:
        cleaned = cleaned[:277].rsplit(" ", 1)[0] + "."
    return cleaned


def spoken_from_state(state: AgentState) -> str:
    return to_spoken(
        extract_answer(state),
        tool_results=list(state.get("tool_results") or []),
        agent=str(state.get("active_agent") or ""),
    )
