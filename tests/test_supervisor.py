import asyncio
import time

from jarvis.agent_core import extract_answer
from jarvis.supervisor import arun_jarvis, compile_supervisor_graph, run_jarvis


def test_supervisor_graph_compiles():
    graph = compile_supervisor_graph()
    mermaid = graph.get_graph().draw_mermaid()
    assert "supervisor" in mermaid
    assert "code_agent" in mermaid
    assert "comms_agent" in mermaid
    assert "shop_agent" in mermaid
    assert "research_agent" in mermaid


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


def test_supervisor_routes_zadarma_sms():
    state = run_jarvis("Envía un SMS de Zadarma al cliente del taller", session_id="t-sms")
    assert state.get("active_agent") == "comms_agent"
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "send_zadarma_sms" in tools_used
    assert "send_sms" not in tools_used
    answer = extract_answer(state)
    assert "zadarma" in answer.lower() or "demo" in answer.lower()


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


def test_supervisor_passes_context_and_gets_report():
    state = run_jarvis("Lista los productos de Shopify", session_id="t-handoff")
    log = state.get("delegation_log") or []
    assert log
    assert log[0]["agent"] == "shop_agent"
    assert "Auriculares Jarvis" in str(log[0]["result"])
    assert "shop_agent" in (state.get("visited_agents") or [])


def test_supervisor_chains_code_then_comms():
    state = run_jarvis(
        "Crea el archivo aviso.txt con el texto listo y envía un WhatsApp al equipo",
        session_id="t-multi",
    )
    visited = state.get("visited_agents") or []
    assert "code_agent" in visited
    assert "comms_agent" in visited
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "write_file" in tools_used
    assert "send_whatsapp_message" in tools_used
    from jarvis.tools import read_file

    assert "listo" in read_file.invoke({"path": "aviso.txt"}).lower() or "aviso" in extract_answer(state).lower()


def test_terminal_order_does_not_blow_up_the_graph():
    """Esta frase giraba en el subgrafo hasta el GraphRecursionError (Vapi oía un error)."""
    state = run_jarvis("Ejecuta en la terminal el comando sleep 6", session_id="t-loop")
    assert state.get("task_complete") is True
    assert extract_answer(state)


def test_arun_jarvis_answers_like_run_jarvis():
    state = asyncio.run(arun_jarvis("¿Cuánto es 17 * 24?", session_id="t-async"))
    assert state.get("active_agent") == "general"
    assert "408" in extract_answer(state)


def test_arun_jarvis_keeps_the_event_loop_free(monkeypatch):
    """Un nodo síncrono lento no puede parar el loop: el SSE de Vapi late en paralelo."""

    class _SlowMemory:
        def retrieve(self, _query: str) -> str:
            time.sleep(0.3)
            return ""

        def remember(self, _text: str) -> None:
            return None

    monkeypatch.setattr("jarvis.supervisor.get_memory", lambda: _SlowMemory())

    async def scenario() -> tuple[int, str]:
        beats = 0

        async def heartbeat() -> None:
            nonlocal beats
            while True:
                await asyncio.sleep(0.02)
                beats += 1

        pulse = asyncio.create_task(heartbeat())
        state = await arun_jarvis("¿Cuánto es 17 * 24?", session_id="t-async-loop")
        pulse.cancel()
        return beats, extract_answer(state)

    beats, answer = asyncio.run(scenario())
    assert "408" in answer
    assert beats >= 3, "el loop siguió bloqueado mientras LangGraph trabajaba"


def test_supervisor_routes_research_osint(monkeypatch):
    monkeypatch.setattr(
        "jarvis.agents.research_agent._tavily_search",
        lambda _query: None,
    )
    monkeypatch.setattr(
        "jarvis.agents.research_agent._duckduckgo_search",
        lambda query: [
            {
                "title": "Ada Lovelace",
                "snippet": "Matemática y pionera de la programación.",
                "url": "https://example.com/ada",
            }
        ],
    )
    state = run_jarvis(
        "Investiga a Ada Lovelace en Google y recopila información pública",
        session_id="t-osint",
    )
    assert state.get("active_agent") == "research_agent"
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "web_search" in tools_used
    log = state.get("delegation_log") or []
    assert log
    assert log[0]["agent"] == "research_agent"
    assert "research_agent" in (state.get("visited_agents") or [])
    answer = extract_answer(state)
    assert "Ada" in answer or "demo" in answer.lower() or "información" in answer.lower()
    from jarvis.hud_live import is_researching

    assert is_researching() is False
