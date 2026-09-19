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


def test_transcript_feed_turns_vapi_messages_into_terminal_lines():
    script = ROOT / "tests" / "test_transcripts.js"
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_transcripts_reach_the_supervisor_terminal():
    """Lo que dicen usuario y asistente en la llamada de Vapi se imprime en la terminal."""
    client = TestClient(create_app())
    js = client.get("/static/transcripts.js")
    assert js.status_code == 200
    assert 'type === "transcript"' in js.text
    assert '"final"' in js.text
    assert '"conversation-update"' in js.text
    assert "Tú:" in js.text and "JARVIS:" in js.text

    hud = client.get("/").text
    assert "/static/transcripts.js" in hud
    assert "createTranscriptFeed()" in hud
    # El listener de mensajes existe y alimenta la terminal con cada línea.
    assert 'vapi.on("message"' in hud
    assert "handleVapiMessage(msg)" in hud
    assert "transcriptFeed.ingest(msg)" in hud
    assert "log(said.line)" in hud
    # Cada llamada empieza limpia y, si nadie transcribió, la terminal dice por qué.
    assert hud.index('vapi.on("call-start"') < hud.index("transcriptFeed.reset()")
    assert "reportSilentTranscripts()" in hud
    assert "Client Messages" in hud
    # Un payload inesperado no puede dejar muda la terminal el resto de la llamada.
    assert hud.index("try {\n                handleVapiMessage(msg);") > 0


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
