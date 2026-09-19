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

from jarvis.config import get_settings
from jarvis.device_bridge import manager

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
