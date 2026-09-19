"""Command Emission: la herramienta, la cola, la API y el nodo local."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.agent_core import extract_answer
from jarvis.api.app import create_app
from jarvis.commands import (
    SystemCommand,
    ack_command,
    enqueue_command,
    list_commands,
    normalize_relative_path,
    pending_commands,
)
from jarvis.config import get_settings
from jarvis.supervisor import run_jarvis
from jarvis.tools import CODE_TOOLS, tools_by_agent
from jarvis.tools.system_commander import build_command, slugify, system_commander, title_from_brief
from jarvis.voice import TOOL_NARRATION

ROOT = Path(__file__).resolve().parent.parent


def _load_local_node():
    spec = importlib.util.spec_from_file_location("local_node", ROOT / "local_node.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Dominio: el JSON estandarizado y sus límites
# ---------------------------------------------------------------------------


def test_the_command_matches_the_agreed_contract():
    cmd = SystemCommand(path="./nuevo_proyecto", files=[{"name": "index.html", "content": "<h1>Hola</h1>"}], open_with="cursor")
    wire = cmd.wire()
    assert wire["action"] == "CREATE_PROJECT"
    assert wire["path"] == "./nuevo_proyecto"
    assert wire["files"] == [{"name": "index.html", "content": "<h1>Hola</h1>"}]
    assert wire["open_with"] == "cursor"
    assert len(wire["id"]) == 16


@pytest.mark.parametrize(
    "raw",
    ["/etc", "../fuera", "C:\\Windows", "~/secreto", "a/../../b", "", ".", "proyecto/../../.ssh"],
)
def test_paths_that_leave_the_root_are_rejected_before_queueing(raw):
    with pytest.raises(ValueError):
        normalize_relative_path(raw)
    with pytest.raises(ValueError):
        SystemCommand(path=raw, files=[])
    if raw:
        with pytest.raises(ValueError):
            SystemCommand(path="ok", files=[{"name": raw, "content": ""}])


def test_paths_are_normalised_to_posix_relative():
    assert normalize_relative_path("./taller web/") == "taller web"
    assert normalize_relative_path("css\\style.css") == "css/style.css"
    assert SystemCommand(path="taller-web", files=[]).path == "./taller-web"


def test_duplicate_files_and_oversized_projects_are_rejected():
    with pytest.raises(ValueError):
        SystemCommand(path="p", files=[{"name": "a.txt", "content": ""}, {"name": "./a.txt", "content": ""}])
    with pytest.raises(ValueError):
        SystemCommand(path="p", files=[{"name": "big.bin", "content": "x" * 1_600_000}])


def test_queue_lifecycle_pending_delivered_done():
    record = enqueue_command(SystemCommand(path="demo", files=[{"name": "a.txt", "content": "1"}]), origin="test")
    assert record.status == "pending"
    peek = pending_commands(claim=False)
    assert [c["id"] for c in peek] == [record.id]
    delivered = pending_commands(node="pc-de-prueba")
    assert delivered[0]["files"] == [{"name": "a.txt", "content": "1"}]
    # Entregar reclama: el siguiente sondeo no vuelve a crear el mismo proyecto.
    assert pending_commands() == []
    done = ack_command(record.id, ok=True, detail="creado", node="pc-de-prueba")
    assert done is not None and done.status == "done" and done.node == "pc-de-prueba"
    history = list_commands()
    assert history[0].id == record.id and history[0].finished_at
    assert ack_command("no-existe", ok=True) is None


# ---------------------------------------------------------------------------
# La herramienta del agente
# ---------------------------------------------------------------------------


def test_system_commander_is_a_code_agent_tool_with_narration():
    assert "system_commander" in [t.name for t in CODE_TOOLS]
    assert "system_commander" in [t.name for t in tools_by_agent("code_agent")]
    assert "system_commander" not in [t.name for t in tools_by_agent("general")]
    assert "maestro" in TOOL_NARRATION["system_commander"]


def test_the_tool_does_not_touch_the_disk_it_only_queues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = system_commander.invoke(
        {"path": "./taller-web", "files": [{"name": "index.html", "content": "<h1>Taller</h1>"}], "open_with": "cursor"}
    )
    spoken, payload = out.split("\n", 1)
    assert "maestro" in spoken and "Cursor" in spoken
    data = json.loads(payload)
    assert data["queued"] is True and data["action"] == "CREATE_PROJECT"
    assert data["path"] == "./taller-web" and data["files"] == ["index.html"]
    assert data["pending_endpoint"].endswith("/api/commands/pending")
    assert not (tmp_path / "taller-web").exists()
    assert not (get_settings().workspace_path / "taller-web").exists()
    assert pending_commands()[0]["id"] == data["id"]


def test_the_tool_writes_the_project_itself_from_a_brief_when_offline():
    cmd = build_command(brief="Crea una web HTML para un taller mecánico y ábrela en Cursor")
    assert cmd.path == "./web-html-taller-mecanico"
    names = [f.name for f in cmd.files]
    assert "index.html" in names and "README.md" in names
    html = next(f.content for f in cmd.files if f.name == "index.html")
    assert "Web HTML para un taller mecánico" in html and "..." not in html
    assert cmd.open_with == "cursor"


def test_the_tool_needs_either_files_or_a_brief():
    out = system_commander.invoke({"path": "./vacio"})
    assert out.startswith("Error")
    assert pending_commands() == []


def test_the_tool_refuses_unsafe_paths_politely():
    out = system_commander.invoke({"path": "../../etc", "files": [{"name": "passwd", "content": ""}]})
    assert out.startswith("Error") and ("relativo" in out or "salir" in out)


def test_brief_slugs_and_titles():
    assert slugify("Crea una web HTML para un taller mecánico y ábrela en Cursor") == "web-html-taller-mecanico"
    assert slugify("Genera un script Python que renombre fotos, en mi ordenador") == "script-python-renombre-fotos"
    assert title_from_brief("Hazme una landing para la pizzería en Cursor.") == "Landing para la pizzería"
    assert slugify("!!!") == "proyecto-jarvis"


def test_open_with_aliases():
    assert build_command(path="p", files=[{"name": "a", "content": ""}], open_with="VS Code").open_with == "code"
    assert build_command(path="p", files=[{"name": "a", "content": ""}], open_with="ninguno").open_with == "none"
    assert build_command(path="p", files=[{"name": "a", "content": ""}], open_with="").open_with == "cursor"


def test_a_voice_request_reaches_the_code_agent_and_queues_a_project():
    result = run_jarvis("Crea una web HTML para un taller y ábrela en Cursor", session_id="cmd-1")
    assert result.get("active_agent") == "code_agent"
    tools = [t["tool"] for t in result.get("tool_results") or []]
    assert tools == ["system_commander"]
    answer = extract_answer(result)
    assert answer.startswith("Proyecto ./web-html-taller") and "Cursor" in answer
    assert "{" not in answer  # la respuesta hablada no lleva el JSON
    queued = pending_commands()
    assert len(queued) == 1 and queued[0]["open_with"] == "cursor"
    assert any(f["name"] == "index.html" for f in queued[0]["files"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _client():
    return TestClient(create_app())


def test_pending_endpoint_delivers_and_claims_then_ack_closes():
    client = _client()
    created = client.post(
        "/api/commands",
        json={"action": "CREATE_PROJECT", "path": "./demo", "files": [{"name": "index.html", "content": "<p>hola</p>"}], "open_with": "cursor"},
    )
    assert created.status_code == 201
    cid = created.json()["id"]

    first = client.get("/api/commands/pending", params={"node": "mi-pc"}).json()
    assert first["count"] == 1 and first["poll_seconds"] == 2
    cmd = first["commands"][0]
    assert cmd == {
        "id": cid,
        "action": "CREATE_PROJECT",
        "path": "./demo",
        "files": [{"name": "index.html", "content": "<p>hola</p>"}],
        "open_with": "cursor",
        "description": "",
    }
    assert client.get("/api/commands/pending").json()["count"] == 0
    assert client.get(f"/api/commands/{cid}").json()["status"] == "delivered"

    ack = client.post(f"/api/commands/{cid}/ack", json={"ok": True, "detail": "creado en ~/JarvisProjects/demo", "node": "mi-pc"})
    assert ack.status_code == 200 and ack.json()["status"] == "done"
    listed = client.get("/api/commands").json()
    assert listed["summary"]["done"] == 1 and listed["token_required"] is False
    assert client.post("/api/commands/nada/ack", json={"ok": False}).status_code == 404


def test_api_rejects_unsafe_commands():
    client = _client()
    res = client.post("/api/commands", json={"action": "CREATE_PROJECT", "path": "/etc", "files": []})
    assert res.status_code == 422
    res = client.post("/api/commands", json={"action": "DELETE_EVERYTHING", "path": "x", "files": []})
    assert res.status_code == 422


def test_node_token_protects_every_command_route(monkeypatch):
    monkeypatch.setenv("JARVIS_NODE_TOKEN", "secreto-123")
    get_settings.cache_clear()
    try:
        client = _client()
        assert client.get("/api/commands/pending").status_code == 401
        assert client.get("/api/commands").status_code == 401
        assert client.post("/api/commands", json={"path": "p", "files": []}).status_code == 401
        assert client.post("/api/commands/x/ack", json={"ok": True}).status_code == 401
        headers = {"X-Jarvis-Node-Token": "secreto-123"}
        assert client.get("/api/commands/pending", headers=headers).status_code == 200
        assert client.get("/api/commands", headers={"Authorization": "Bearer secreto-123"}).json()["token_required"] is True
        assert client.get("/api/commands/pending", headers={"X-Jarvis-Node-Token": "otro"}).status_code == 401
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# local_node.py: ejecuta en el PC lo que llega, y nada más
# ---------------------------------------------------------------------------


def test_local_node_creates_the_project_and_opens_cursor(tmp_path, monkeypatch):
    node = _load_local_node()
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))

    monkeypatch.setattr(node.shutil, "which", lambda name: f"/usr/local/bin/{name}" if name == "cursor" else None)
    command = {
        "id": "abc",
        "action": "CREATE_PROJECT",
        "path": "./taller-web",
        "files": [
            {"name": "index.html", "content": "<h1>Taller</h1>"},
            {"name": "css/style.css", "content": "body{}"},
        ],
        "open_with": "cursor",
    }
    detail = node.execute(command, tmp_path, opener=fake_run)
    project = tmp_path / "taller-web"
    assert (project / "index.html").read_text(encoding="utf-8") == "<h1>Taller</h1>"
    assert (project / "css" / "style.css").read_text(encoding="utf-8") == "body{}"
    assert calls == [["/usr/local/bin/cursor", str(project.resolve())]]
    assert "2 archivo(s)" in detail and "IDE: cursor" in detail


def test_local_node_without_cursor_in_path_still_creates_files(tmp_path, monkeypatch):
    node = _load_local_node()
    monkeypatch.setattr(node.shutil, "which", lambda name: None)
    opened: list = []
    detail = node.execute(
        {"action": "CREATE_PROJECT", "path": "solo", "files": [{"name": "a.txt", "content": "1"}], "open_with": "cursor"},
        tmp_path,
        opener=lambda *a, **k: opened.append(a),
    )
    assert (tmp_path / "solo" / "a.txt").exists() and opened == []
    assert "no está en el PATH" in detail


def test_local_node_never_writes_outside_its_root(tmp_path):
    node = _load_local_node()
    for bad in ("../fuera", "/tmp/x", "C:\\Users", "~/x"):
        with pytest.raises(node.CommandError):
            node.execute({"action": "CREATE_PROJECT", "path": bad, "files": [], "open_with": "none"}, tmp_path)
    with pytest.raises(node.CommandError):
        node.execute(
            {"action": "CREATE_PROJECT", "path": "ok", "files": [{"name": "../../escape.txt", "content": "x"}], "open_with": "none"},
            tmp_path,
        )
    with pytest.raises(node.CommandError):
        node.execute({"action": "RUN_SHELL", "path": "ok", "files": []}, tmp_path)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_local_node_polls_the_backend_creates_and_acks(tmp_path, monkeypatch):
    """Nodo contra la API real (TestClient hace de red): recoge, crea y confirma."""
    node = _load_local_node()
    client = _client()
    created = client.post(
        "/api/commands",
        json={"path": "./desde-render", "files": [{"name": "index.html", "content": "<h1>ok</h1>"}], "open_with": "none"},
    ).json()

    class FakeClient(node.JarvisClient):
        def _request(self, method, path, body=None):  # noqa: ANN001
            headers = {node.TOKEN_HEADER: self.token} if self.token else {}
            res = client.request(method, path, json=body, headers=headers)
            res.raise_for_status()
            return res.json()

    fake = FakeClient("http://testserver", node="pc-test")
    processed = node.process_pending(fake, tmp_path)
    assert processed == 1
    assert (tmp_path / "desde-render" / "index.html").read_text(encoding="utf-8") == "<h1>ok</h1>"
    record = client.get(f"/api/commands/{created['id']}").json()
    assert record["status"] == "done" and record["node"] == "pc-test" and "1 archivo(s)" in record["result"]
    assert node.process_pending(fake, tmp_path) == 0


def test_local_node_cli_defaults_and_stdlib_only():
    node = _load_local_node()
    args = node.parse_args(["--once", "--root", "/tmp/x", "--interval", "5"])
    assert args.once and args.interval == 5.0 and args.url.startswith("http")
    source = (ROOT / "local_node.py").read_text(encoding="utf-8")
    for forbidden in ("import requests", "import httpx", "from jarvis", "import fastapi"):
        assert forbidden not in source
    assert 'subprocess.run' in source or "opener([*exe" in source
    assert "/api/commands/pending" in source and "/ack" in source
    assert sys.version_info >= (3, 9)
