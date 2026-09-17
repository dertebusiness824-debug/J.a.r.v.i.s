from jarvis.api.vapi_routes import extract_user_turn, openai_completion
from jarvis.prompts import SUPERVISOR_PROMPT
from jarvis.voice import strip_markdown, to_spoken


def test_supervisor_prompt_is_voice_ready():
    assert "interfaz de voz" in SUPERVISOR_PROMPT
    assert "NUNCA uses formato Markdown" in SUPERVISOR_PROMPT
    assert "Archivo actualizado" in SUPERVISOR_PROMPT


def test_strip_markdown():
    raw = "**Inventario** revisado.\n```python\nprint(1)\n```"
    cleaned = strip_markdown(raw)
    assert "*" not in cleaned
    assert "```" not in cleaned
    assert "Inventario revisado." in cleaned


def test_spoken_calculator_and_shop():
    calc = to_spoken("408", tool_results=[{"tool": "calculate_expression", "output": "408", "ok": True}])
    assert calc == "El resultado es 408."
    shop = to_spoken(
        '{"mode": "demo"}',
        tool_results=[
            {
                "tool": "shopify_list_products",
                "output": '{"mode":"demo","products":[{"title":"Auriculares Jarvis","totalInventory":42}]}',
                "ok": True,
            }
        ],
    )
    assert "Inventario revisado" in shop
    assert "Auriculares Jarvis" in shop
    assert "*" not in shop
    file_out = to_spoken("Escrito 10 caracteres", tool_results=[{"tool": "write_file", "output": "ok", "ok": True}])
    assert file_out == "Archivo actualizado."
    sms = to_spoken(
        '{"mode":"demo"}',
        tool_results=[{"tool": "send_zadarma_sms", "output": '{"status":"queued"}', "ok": True}],
    )
    assert sms == "SMS enviado."


def test_extract_vapi_payload_shapes():
    openai_style, session, stream = extract_user_turn(
        {
            "stream": True,
            "call": {"id": "call-1"},
            "messages": [
                {"role": "system", "content": "ignore"},
                {"role": "user", "content": "Hola Jarvis"},
            ],
        }
    )
    assert openai_style == "Hola Jarvis"
    assert session == "call-1"
    assert stream is True

    wrapped, session2, _ = extract_user_turn(
        {"message": {"messages": [{"role": "user", "content": "Stock de Shopify"}]}}
    )
    assert wrapped == "Stock de Shopify"
    assert session2 == "vapi"


def test_openai_completion_shape():
    body = openai_completion("Archivo actualizado.")
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "Archivo actualizado."
