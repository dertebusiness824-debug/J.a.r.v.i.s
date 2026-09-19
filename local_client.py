#!/usr/bin/env python3
"""Cliente WebSocket que corre EN TU ORDENADOR y ejecuta las órdenes de Jarvis.

El backend en Render no puede abrir la calculadora de tu PC. Este script se
queda conectado a `/ws/device-control` y, cuando LangGraph llama a
`execute_local_command`, recibe el JSON y lo despacha.

    python local_client.py
    JARVIS_NODE_TOKEN=... python local_client.py
    python local_client.py --url wss://j-a-r-v-i-s-yghr.onrender.com/ws/device-control

Dependencia: `pip install websockets`. Reconexión automática si el socket se cae.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_URL = "wss://j-a-r-v-i-s-yghr.onrender.com/ws/device-control"
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0

APP_ALIASES = {
    "calculator": "calculator",
    "calc": "calculator",
    "calculadora": "calculator",
    "notepad": "notepad",
    "notes": "notepad",
    "bloc de notas": "notepad",
    "textedit": "notepad",
    "gedit": "notepad",
}


def log(message: str) -> None:
    print(f"[jarvis-ws] {message}", flush=True)


def _popen_kwargs() -> dict[str, Any]:
    if platform.system() == "Windows":
        return {}
    return {"start_new_session": True}


def _open_commands(app: str) -> list[list[str]]:
    system = platform.system()
    if app == "calculator":
        if system == "Darwin":
            return [["open", "-a", "Calculator"]]
        if system == "Windows":
            return [["calc.exe"]]
        return [["gnome-calculator"], ["kcalc"], ["xcalc"]]
    if app == "notepad":
        if system == "Darwin":
            return [["open", "-a", "TextEdit"]]
        if system == "Windows":
            return [["notepad.exe"]]
        return [["gedit"], ["kate"], ["xdg-open", os.path.expanduser("~/")]]
    return []


def handle_open_app(payload: dict[str, Any]) -> str:
    raw = str(payload.get("app") or "").strip().lower()
    app = APP_ALIASES.get(raw)
    if not app:
        return f"app no permitida: {raw or '?'}"
    last_error = "sin comando"
    for command in _open_commands(app):
        try:
            subprocess.Popen(command, **_popen_kwargs())
            return f"OPEN_APP {app} → {' '.join(command)}"
        except FileNotFoundError as exc:
            last_error = str(exc)
        except OSError as exc:
            last_error = str(exc)
    return f"no pude abrir {app}: {last_error}"


def _system_beep() -> None:
    print("\a", end="", flush=True)
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_popen_kwargs(),
            )
        elif system == "Windows":
            import winsound

            winsound.MessageBeep()
        else:
            subprocess.Popen(
                ["printf", "\\a"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_popen_kwargs(),
            )
    except Exception:
        return


def handle_system_alert(payload: dict[str, Any]) -> str:
    message = str(payload.get("message") or "Alerta de Jarvis").strip() or "Alerta de Jarvis"
    log(f"ALERTA: {message}")
    _system_beep()
    return f"SYSTEM_ALERT: {message}"


HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "OPEN_APP": handle_open_app,
    "SYSTEM_ALERT": handle_system_alert,
}


def dispatch(message: dict[str, Any]) -> str:
    """Enruta el JSON del backend. Ignora keepalives y bienvenidas."""
    kind = str(message.get("command_type") or message.get("type") or "").strip()
    if kind in {"welcome", "keepalive", "pong", ""}:
        return ""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    handler = HANDLERS.get(kind)
    if handler is None:
        return f"comando desconocido: {kind}"
    return handler(payload)


def _with_token(url: str, token: str) -> str:
    if not token:
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("token", token)
    return urlunparse(parts._replace(query=urlencode(query)))


async def _listen(url: str, token: str, node: str) -> None:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - se prueba el mensaje
        raise SystemExit(
            "Falta el paquete websockets. En tu PC: pip install websockets"
        ) from exc

    target = _with_token(url, token)
    headers = [("X-Jarvis-Node-Token", token)] if token else None
    connect_kwargs: dict[str, Any] = {"ping_interval": 20, "ping_timeout": 20}
    # websockets 10 usaba extra_headers; 11+ additional_headers.
    if headers:
        connect_kwargs["additional_headers"] = headers

    try:
        socket_ctx = websockets.connect(target, **connect_kwargs)
    except TypeError:
        connect_kwargs.pop("additional_headers", None)
        if headers:
            connect_kwargs["extra_headers"] = headers
        socket_ctx = websockets.connect(target, **connect_kwargs)

    async with socket_ctx as socket:
        log(f"conectado a {url} como {node}")
        async for raw in socket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if str(raw).strip().lower() == "pong":
                    continue
                log(f"JSON ilegible: {raw!r:.120}")
                continue
            if not isinstance(data, dict):
                continue
            result = dispatch(data)
            if not result:
                continue
            log(result)
            try:
                await socket.send(
                    json.dumps({"type": "ack", "id": data.get("id"), "detail": result})
                )
            except Exception:
                return


async def run_forever(url: str, token: str, node: str) -> None:
    delay = RECONNECT_MIN
    while True:
        try:
            await _listen(url, token, node)
            log("el servidor cerró el socket")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"conexión caída ({exc})")
        log(f"reintento en {delay:.0f}s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Puente WebSocket local de Jarvis.")
    parser.add_argument("--url", default=os.environ.get("JARVIS_WS_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("JARVIS_NODE_TOKEN", ""))
    parser.add_argument("--node", default=os.environ.get("JARVIS_NODE_NAME", platform.node() or "pc"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        log("sin JARVIS_NODE_TOKEN: si Render lo exige, la conexión cerrará con 1008")
    try:
        asyncio.run(run_forever(args.url, args.token, args.node))
    except KeyboardInterrupt:
        log("parado")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
