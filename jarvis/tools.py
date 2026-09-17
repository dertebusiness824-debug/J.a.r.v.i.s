"""Herramientas de prueba y de dominio, tipadas con Pydantic para function-calling."""

from __future__ import annotations

import ast
import operator as op
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from jarvis.config import get_settings

_BIN_OPS: dict[type, object] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
}
_UNARY_OPS: dict[type, object] = {
    ast.UAdd: op.pos,
    ast.USub: op.neg,
}


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 32:
            raise ValueError("Exponente fuera de rango")
        return _BIN_OPS[type(node.op)](left, right)  # type: ignore[operator]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_ast(node.operand))  # type: ignore[operator]
    raise ValueError("Expresión no permitida. Usa aritmética básica (+ - * / ** % //).")


class CurrentTimeInput(BaseModel):
    timezone_name: str = Field(
        default="UTC",
        description="IANA timezone (p.ej. UTC, America/Mexico_City). Por defecto UTC.",
    )


class CalculateExpressionInput(BaseModel):
    expression: str = Field(
        description="Expresión aritmética a evaluar, p.ej. '(17 * 24) + 3'."
    )


class ReadFileInput(BaseModel):
    path: str = Field(description="Ruta relativa al sandbox del Code Agent.")


class WriteFileInput(BaseModel):
    path: str = Field(description="Ruta relativa al sandbox del Code Agent.")
    content: str = Field(description="Contenido completo a escribir.")


class ListDirectoryInput(BaseModel):
    path: str = Field(default=".", description="Directorio relativo al sandbox.")


class RunTerminalInput(BaseModel):
    command: str = Field(description="Comando de terminal a ejecutar dentro del sandbox.")


class SendWhatsAppInput(BaseModel):
    to: str = Field(
        description="Destinatario: E.164 (+52155…) o JID de WhatsApp Web (52155…@c.us)."
    )
    body: str = Field(description="Texto del mensaje de WhatsApp.")


class SendZadarmaSmsInput(BaseModel):
    to: str = Field(description="Número E.164 del destinatario.")
    body: str = Field(description="Texto del SMS.")
    sender: str | None = Field(
        default=None,
        description="Caller ID / SenderID Zadarma opcional (número verificado o alfanumérico).",
    )


class ShopifyProductsInput(BaseModel):
    first: int = Field(default=10, ge=1, le=50, description="Número de productos a listar.")


class ShopifyOrdersInput(BaseModel):
    first: int = Field(default=10, ge=1, le=50, description="Número de pedidos a listar.")
    query: str | None = Field(default=None, description="Filtro de búsqueda de pedidos Shopify.")


class ShopifyInventoryInput(BaseModel):
    product_id: str | None = Field(
        default=None,
        description="GID de producto Shopify. Si se omite, resume inventario general.",
    )


def _safe_workspace_path(relative: str) -> Path:
    settings = get_settings()
    root = settings.workspace_path
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Ruta fuera del sandbox del Code Agent.")
    return candidate


_BLOCKED_TERMINAL = (
    "rm -rf /",
    "mkfs",
    ":(){",
    "fork bomb",
    "dd if=",
    "shutdown",
    "reboot",
    "chmod 777 /",
    "mkfs.",
    "> /dev/sd",
)


@tool("get_current_time", args_schema=CurrentTimeInput)
def get_current_time(timezone_name: str = "UTC") -> str:
    """Devuelve la fecha y hora actual en ISO-8601."""
    if timezone_name.upper() == "UTC":
        now = datetime.now(timezone.utc)
        return now.isoformat()
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(timezone_name))
        return now.isoformat()
    except Exception as exc:  # pragma: no cover - zoneinfo edge
        return f"Error de timezone '{timezone_name}': {exc}"


@tool("calculate_expression", args_schema=CalculateExpressionInput)
def calculate_expression(expression: str) -> str:
    """Evalúa de forma segura una expresión aritmética y devuelve el resultado numérico."""
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_ast(tree)
        if value.is_integer():
            return str(int(value))
        return str(value)
    except Exception as exc:
        return f"Error al calcular '{expression}': {exc}"


@tool("read_file", args_schema=ReadFileInput)
def read_file(path: str) -> str:
    """Lee un archivo de texto dentro del sandbox del Code Agent."""
    try:
        target = _safe_workspace_path(path)
    except ValueError as exc:
        return f"Error: {exc}"
    if not target.exists():
        return f"Error: no existe {path}"
    if not target.is_file():
        return f"Error: {path} no es un archivo"
    return target.read_text(encoding="utf-8")


@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str) -> str:
    """Escribe (o crea) un archivo de texto dentro del sandbox del Code Agent."""
    try:
        target = _safe_workspace_path(path)
    except ValueError as exc:
        return f"Error: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Escrito {len(content)} caracteres en {path}"


@tool("list_directory", args_schema=ListDirectoryInput)
def list_directory(path: str = ".") -> str:
    """Lista archivos y carpetas del sandbox del Code Agent."""
    try:
        target = _safe_workspace_path(path)
    except ValueError as exc:
        return f"Error: {exc}"
    if not target.exists():
        return f"Error: no existe {path}"
    if not target.is_dir():
        return f"Error: {path} no es un directorio"
    entries = []
    for item in sorted(target.iterdir()):
        kind = "dir" if item.is_dir() else "file"
        entries.append(f"{kind}\t{item.relative_to(_safe_workspace_path('.'))}")
    return "\n".join(entries) if entries else "(vacío)"


@tool("run_terminal", args_schema=RunTerminalInput)
def run_terminal(command: str) -> str:
    """Ejecuta un comando de terminal con cwd y timeout restringidos al sandbox."""
    lowered = command.strip().lower()
    if any(token in lowered for token in _BLOCKED_TERMINAL):
        return "Error: comando bloqueado por política de seguridad del Code Agent."
    settings = get_settings()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(settings.workspace_path),
            capture_output=True,
            text=True,
            timeout=settings.terminal_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Error: timeout ({settings.terminal_timeout_seconds}s) al ejecutar el comando."
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return f"Error (exit {completed.returncode}): {stderr or stdout or 'sin salida'}"
    return stdout or "(sin salida)"


@tool("send_whatsapp_message", args_schema=SendWhatsAppInput)
def send_whatsapp_message(to: str, body: str) -> str:
    """Envía WhatsApp vía el puente local: POST http://127.0.0.1:3000/send."""
    import json

    import requests

    from jarvis.integrations.messaging import to_whatsapp_id

    payload = {"to": to_whatsapp_id(to), "message": body}
    try:
        response = requests.post("http://127.0.0.1:3000/send", json=payload, timeout=20)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}
        if isinstance(data, dict):
            data.setdefault("channel", "whatsapp-web")
            data.setdefault("to", payload["to"])
            return json.dumps(data, ensure_ascii=False)
        return json.dumps({"channel": "whatsapp-web", "result": data}, ensure_ascii=False)
    except requests.RequestException as exc:
        return json.dumps(
            {
                "mode": "demo",
                "channel": "whatsapp-web",
                "to": payload["to"],
                "body": body,
                "status": "queued",
                "error": f"puente no disponible: {exc}",
            },
            ensure_ascii=False,
        )


@tool("send_zadarma_sms", args_schema=SendZadarmaSmsInput)
def send_zadarma_sms(to: str, body: str, sender: str | None = None) -> str:
    """Envía un SMS vía Zadarma PBX (API REST firmada). En modo demo, simula el envío."""
    from jarvis.integrations.zadarma import ZadarmaClient

    return ZadarmaClient().send_sms(to=to, body=body, sender=sender)


@tool("shopify_list_products", args_schema=ShopifyProductsInput)
def shopify_list_products(first: int = 10) -> str:
    """Lista productos de la tienda Shopify vía GraphQL Admin API."""
    from jarvis.integrations.shopify import ShopifyClient

    return ShopifyClient().list_products(first=first)


@tool("shopify_list_orders", args_schema=ShopifyOrdersInput)
def shopify_list_orders(first: int = 10, query: str | None = None) -> str:
    """Lista pedidos de Shopify vía GraphQL Admin API."""
    from jarvis.integrations.shopify import ShopifyClient

    return ShopifyClient().list_orders(first=first, query=query)


@tool("shopify_inventory_summary", args_schema=ShopifyInventoryInput)
def shopify_inventory_summary(product_id: str | None = None) -> str:
    """Resume inventario de Shopify (producto concreto o vista general)."""
    from jarvis.integrations.shopify import ShopifyClient

    return ShopifyClient().inventory_summary(product_id=product_id)


CORE_TOOLS = [get_current_time, calculate_expression]
CODE_TOOLS = [read_file, write_file, list_directory, run_terminal, *CORE_TOOLS]
COMMS_TOOLS = [send_whatsapp_message, send_zadarma_sms, get_current_time]
SHOP_TOOLS = [
    shopify_list_products,
    shopify_list_orders,
    shopify_inventory_summary,
    calculate_expression,
]
ALL_TOOLS = [
    get_current_time,
    calculate_expression,
    read_file,
    write_file,
    list_directory,
    run_terminal,
    send_whatsapp_message,
    send_zadarma_sms,
    shopify_list_products,
    shopify_list_orders,
    shopify_inventory_summary,
]


def tools_by_agent(name: str) -> list:
    mapping = {
        "general": CORE_TOOLS,
        "code_agent": CODE_TOOLS,
        "comms_agent": COMMS_TOOLS,
        "shop_agent": SHOP_TOOLS,
    }
    return mapping.get(name, CORE_TOOLS)
