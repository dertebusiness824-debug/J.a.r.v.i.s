"""Connection Manager del puente WebSocket. Sin FastAPI: las tools pueden importarlo."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DeviceConnectionManager:
    """Sockets vivos del PC. `emit` es seguro desde el hilo del Ejecutor LangGraph."""

    def __init__(self) -> None:
        self._sockets: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connected_nodes(self) -> list[str]:
        return sorted(self._sockets)

    def __len__(self) -> int:
        return len(self._sockets)

    async def connect(self, websocket: Any, node: str) -> str:
        self._loop = asyncio.get_running_loop()
        name = _unique_node(node, self._sockets)
        previous = self._sockets.pop(name, None)
        if previous is not None and previous is not websocket:
            await _safe_close(previous)
        self._sockets[name] = websocket
        logger.info("🖥 [DEVICE WS] conectado %s (%d en línea)", name, len(self._sockets))
        return name

    def disconnect(self, node: str, websocket: Any | None = None) -> None:
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


def _unique_node(node: str, taken: dict[str, Any]) -> str:
    base = "".join(ch for ch in (node or "pc") if ch.isalnum() or ch in "-_")[:48] or "pc"
    if base not in taken:
        return base
    for index in range(2, 100):
        candidate = f"{base}-{index}"
        if candidate not in taken:
            return candidate
    return f"{base}-{len(taken) + 1}"


async def _safe_close(websocket: Any) -> None:
    try:
        await websocket.close(code=1001)
    except Exception:
        return


manager = DeviceConnectionManager()
