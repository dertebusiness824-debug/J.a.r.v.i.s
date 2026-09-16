from jarvis.agent_core import extract_answer
from jarvis.supervisor import compile_supervisor_graph, run_jarvis


def test_supervisor_graph_compiles():
    graph = compile_supervisor_graph()
    mermaid = graph.get_graph().draw_mermaid()
    assert "supervisor" in mermaid
    assert "code_agent" in mermaid
    assert "comms_agent" in mermaid
    assert "shop_agent" in mermaid


def test_supervisor_routes_math_to_general():
    state = run_jarvis("¿Cuánto es 17 * 24?", session_id="t-math")
    assert state.get("active_agent") == "general"
    assert "408" in extract_answer(state)


def test_supervisor_routes_shopify():
    state = run_jarvis("Lista los productos de Shopify", session_id="t-shop")
    assert state.get("active_agent") == "shop_agent"
    answer = extract_answer(state)
    assert "Auriculares Jarvis" in answer or "demo" in answer.lower()


def test_supervisor_routes_whatsapp():
    state = run_jarvis("Envía un WhatsApp de prueba al equipo", session_id="t-wa")
    assert state.get("active_agent") == "comms_agent"
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "send_whatsapp_message" in tools_used


def test_supervisor_routes_code_list_sandbox():
    state = run_jarvis("Lista los archivos del sandbox", session_id="t-code")
    assert state.get("active_agent") == "code_agent"
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "list_directory" in tools_used


def test_same_session_reroutes_on_second_turn():
    first = run_jarvis("¿Cuánto es 17 * 24?", session_id="same-thread")
    assert "408" in extract_answer(first)
    second = run_jarvis("Lista los productos de Shopify", session_id="same-thread")
    assert second.get("active_agent") == "shop_agent"
    assert "Auriculares Jarvis" in extract_answer(second)
