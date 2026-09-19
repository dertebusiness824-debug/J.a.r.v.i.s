import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.api.app import create_app
from jarvis.api.device_ws import DEVICE_WS_PATH, manager
from jarvis.config import get_settings
from jarvis.tools import CORE_TOOLS, tools_by_agent
from jarvis.tools.device_control import execute_local_command
from jarvis.voice import TOOL_NARRATION


def test_execute_local_command_is_a_supervisor_tool():
    assert "execute_local_command" in [t.name for t in CORE_TOOLS]
    assert "execute_local_command" in [t.name for t in tools_by_agent("general")]
    assert "execute_local_command" in [t.name for t in tools_by_agent("code_agent")]
    assert "maestro" in TOOL_NARRATION["execute_local_command"]


def test_execute_local_command_reports_when_no_pc_is_connected():
    out = execute_local_command.invoke(
        {"command_type": "OPEN_APP", "payload": {"app": "calculator"}}
    )
    assert "no hay ningún ordenador" in out.lower() or "no hay ningún" in out.lower()
    assert manager.emit({"command_type": "OPEN_APP", "payload": {}}) == 0


def test_execute_local_command_rejects_unknown_types():
    out = execute_local_command.invoke({"command_type": "FORMAT_DISK", "payload": {}})
    assert "no soportado" in out.lower()


def test_device_status_starts_empty():
    client = TestClient(create_app())
    body = client.get("/api/device-control/status").json()
    assert body["path"] == DEVICE_WS_PATH
    assert body["connected"] == 0
    assert body["nodes"] == []


def test_websocket_delivers_the_tool_payload():
    client = TestClient(create_app())
    with client.websocket_connect(f"{DEVICE_WS_PATH}?node=mi-pc") as socket:
        welcome = socket.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["node"] == "mi-pc"
        assert client.get("/api/device-control/status").json()["connected"] == 1
        spoken = execute_local_command.invoke(
            {"command_type": "SYSTEM_ALERT", "payload": {"message": "Hola maestro"}}
        )
        assert "SYSTEM_ALERT" in spoken
        message = socket.receive_json()
        assert message["command_type"] == "SYSTEM_ALERT"
        assert message["payload"]["message"] == "Hola maestro"
        assert message["id"]


def test_websocket_rejects_a_bad_token(monkeypatch):
    monkeypatch.setenv("JARVIS_NODE_TOKEN", "secreto-ws")
    get_settings.cache_clear()
    client = TestClient(create_app())
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(DEVICE_WS_PATH) as socket:
            socket.receive_json()
    assert caught.value.code == 1008
    with client.websocket_connect(f"{DEVICE_WS_PATH}?token=secreto-ws") as socket:
        assert socket.receive_json()["type"] == "welcome"
    get_settings.cache_clear()
