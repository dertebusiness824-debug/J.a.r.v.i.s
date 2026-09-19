import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_client():
    spec = importlib.util.spec_from_file_location("local_client", ROOT / "local_client.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_client_opens_calculator_via_subprocess(monkeypatch):
    client = _load_client()
    calls = []

    def _fake_popen(command, **_kwargs):
        calls.append(command)
        return object()

    monkeypatch.setattr(client.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(client.platform, "system", lambda: "Darwin")
    assert "Calculator" in client.handle_open_app({"app": "calculadora"})
    assert calls == [["open", "-a", "Calculator"]]
    assert "no permitida" in client.handle_open_app({"app": "spotify"})


def test_local_client_system_alert_prints_and_beeps(monkeypatch, capsys):
    client = _load_client()
    monkeypatch.setattr(client, "_system_beep", lambda: None)
    assert "SYSTEM_ALERT" in client.handle_system_alert({"message": "Puente listo"})
    assert "Puente listo" in capsys.readouterr().out


def test_local_client_dispatch_ignores_keepalives_and_unknowns():
    client = _load_client()
    assert client.dispatch({"type": "keepalive"}) == ""
    assert client.dispatch({"type": "welcome", "node": "pc"}) == ""
    assert client.dispatch({"command_type": "FORMAT_DISK", "payload": {}}) == "comando desconocido: FORMAT_DISK"


def test_local_client_cli_defaults_to_render_wss():
    client = _load_client()
    args = client.build_parser().parse_args([])
    assert args.url == client.DEFAULT_URL
    assert args.url.startswith("wss://")
    assert "/ws/device-control" in args.url
