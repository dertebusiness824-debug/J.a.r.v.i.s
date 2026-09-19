"""Puente WebSocket Render → PC: el cliente local se queda conectado y recibe órdenes.

    WS  /ws/device-control          → el PC se registra y espera JSON
    GET /api/device-control/status  → cuántos nodos hay en línea (HUD / depuración)

Con `JARVIS_NODE_TOKEN` el socket exige el mismo secreto que `/api/commands`
(`?token=`, `X-Jarvis-Node-Token` o `Authorization: Bearer`). Sin token
configurado queda abierto, igual que la cola HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any
from weakref import WeakSet

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from jarvis.config import get_settings

logger = logging.getLogger(__name__)

DEVICE_WS_PATH = "/ws/device-control"
KEEPALIVE_SECONDS = 25
WS_UNAUTHORIZED = 1008


def _token_ok(websocket: WebSocket) -> bool:
    expected = (get_settings().jarvis_node_token or "").strip()
    if not expected:
        return True
    query = (websocket.query_params.get("token") or "").strip()
    header = (websocket.headers.get("x-jarvis-node-token") or "").strip()
    auth = (websocket.headers.get("authorization") or "").strip()
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    provided = query or header or bearer
    return bool(provided) and secrets.compare_digest(provided, expected)


class DeviceConnectionManager:
    """Sockets vivos del PC. `emit` es seguro desde el hilo del Ejecutor LangGraph."""

    def __init__(self) -> None:
        self._sockets: dict[str, WebSocket] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connected_nodes(self) -> list[str]:
        return sorted(self._sockets)

    def __len__(self) -> int:
        return len(self._sockets)

    async def connect(self, websocket: WebSocket, node: str) -> str:
        self._loop = asyncio.get_running_loop()
        name = _unique_node(node, self._sockets)
        previous = self._sockets.pop(name, None)
        if previous is not None and previous is not websocket:
            await _safe_close(previous)
        self._sockets[name] = websocket
        logger.info("🖥 [DEVICE WS] conectado %s (%d en línea)", name, len(self._sockets))
        return name

    def disconnect(self, node: str, websocket: WebSocket | None = None) -> None:
        current = self._sockets.get(node)
        if current is None:
            return
        if websocket is not None and current is not websocket:
            return
        self._sockets.pop(node, None)
        logger.info("🖥 [DEVICE WS] desconectado %s (%d en línea)", node, len(self._sockets))

    def reset(self) -> None:
        self._sockets.clear()
        self._loop = None

    async def broadcast(self, message: dict[str, Any]) -> int:
        body = json.dumps(message, ensure_ascii=False)
        dead: list[str] = []
        sent = 0
        for name, socket in list(self._sockets.items()):
            try:
                if socket.client_state != WebSocketState.CONNECTED:
                    dead.append(name)
                    continue
                await socket.send_text(body)
                sent += 1
            except Exception:
                logger.warning("🖥 [DEVICE WS] no pude escribir a %s; lo doy de baja", name)
                dead.append(name)
        for name in dead:
            self.disconnect(name)
        return sent

    def emit(self, message: dict[str, Any]) -> int:
        """Envía el JSON a todos los PCs. 0 si no hay nadie escuchando."""
        if not self._sockets:
            return 0
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (loop is None or running is loop):
            self._loop = running
            running.create_task(self.broadcast(message))
            return len(self._sockets)
        if loop is None or not loop.is_running():
            return 0
        future = asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
        return int(future.result(timeout=5))


def _unique_node(node: str, taken: dict[str, WebSocket]) -> str:
    base = "".join(ch for ch in (node or "pc") if ch.isalnum() or ch in "-_")[:48] or "pc"
    if base not in taken:
        return base
    for index in range(2, 100):
        candidate = f"{base}-{index}"
        if candidate not in taken:
            return candidate
    return f"{base}-{len(taken) + 1}"


async def _safe_close(websocket: WebSocket) -> None:
    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1001)
    except Exception:
        return


manager = DeviceConnectionManager()

# Evita registrar la ruta dos veces (create_app + main.py).
_ATTACHED: WeakSet[FastAPI] = WeakSet()


def attach_device_control(app: FastAPI) -> None:
    if app in _ATTACHED:
        return
    _ATTACHED.add(app)

    @app.websocket(DEVICE_WS_PATH)
    async def device_control_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        if not _token_ok(websocket):
            await websocket.close(code=WS_UNAUTHORIZED, reason="Token del nodo local inválido")
            return
        node = (websocket.query_params.get("node") or "pc").strip() or "pc"
        name = await manager.connect(websocket, node)
        try:
            await websocket.send_text(
                json.dumps({"type": "welcome", "node": name, "path": DEVICE_WS_PATH}, ensure_ascii=False)
            )
            while True:
                try:
                    incoming = await asyncio.wait_for(
                        websocket.receive_text(), timeout=KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "keepalive"}))
                    continue
                if incoming.strip().lower() in {"ping", "pong"}:
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            manager.disconnect(name, websocket)
        except Exception:
            logger.exception("🖥 [DEVICE WS] socket %s se rompió", name)
            manager.disconnect(name, websocket)

    @app.get("/api/device-control/status", tags=["device"])
    def device_control_status() -> dict[str, Any]:
        return {
            "path": DEVICE_WS_PATH,
            "connected": len(manager),
            "nodes": manager.connected_nodes,
            "token_required": bool((get_settings().jarvis_node_token or "").strip()),
        }
