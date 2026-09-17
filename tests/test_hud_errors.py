import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.api.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_format_error_unwraps_nested_vapi_objects():
    script = ROOT / "tests" / "test_format_error.js"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_format_error_script_is_served_and_wired():
    client = TestClient(create_app())
    js = client.get("/static/format-error.js")
    assert js.status_code == 200
    assert "JSON.stringify" in js.text
    hud = client.get("/")
    assert "/static/format-error.js" in hud.text
    assert "ERROR Vapi" in hud.text
    assert "terminalMessages.push" in hud.text
    assert "console.error(\"ERROR Vapi\"" in hud.text
