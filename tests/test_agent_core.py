from langchain_core.messages import AIMessage, HumanMessage

from jarvis.agent_core import (
    bind_executor_tools,
    compile_core_graph,
    extract_answer,
    loop_budget,
    route_after_executor,
    route_after_planner,
    route_after_tools,
    run_core_agent,
)
from jarvis.llms import Plan
from jarvis.state import AgentState


def test_the_persona_leads_every_prompt_that_writes_for_the_user(monkeypatch):
    """El texto hablado lo redactan planificador y ejecutor: la personalidad va ahí."""
    from langchain_core.messages import SystemMessage

    from jarvis.agent_core import _planner_messages, executor_node
    from jarvis.prompts import JARVIS_PERSONA

    planner_first = _planner_messages({"messages": [HumanMessage(content="hola")]})[0]
    assert isinstance(planner_first, SystemMessage)
    assert planner_first.content == JARVIS_PERSONA

    seen: list = []

    class _Spy:
        def bind_tools(self, _tools):
            return self

        def invoke(self, messages):
            seen.extend(messages)
            return AIMessage(content="Hecho, maestro.")

    monkeypatch.setattr("jarvis.agent_core.get_executor_model", lambda: _Spy())
    executor_node({"messages": [HumanMessage(content="hola")]})
    assert seen and seen[0].content == JARVIS_PERSONA


def test_executor_streams_tokens_with_the_parent_config(monkeypatch):
    """Sin los callbacks del padre, `.stream()` no llega a `astream_events` ni a Vapi."""
    from langchain_core.messages import AIMessageChunk

    from jarvis.agent_core import executor_node
    from jarvis.prompts import JARVIS_PERSONA

    seen: dict = {}

    class _Streamer:
        def bind_tools(self, _tools):
            return self

        def stream(self, messages, config=None):
            seen["config"] = config
            seen["messages"] = messages
            yield AIMessageChunk(content="Hecho, ")
            yield AIMessageChunk(content="maestro.")

    monkeypatch.setattr("jarvis.agent_core.get_executor_model", lambda: _Streamer())
    parent = {"callbacks": ["keep-streaming"], "configurable": {"thread_id": "voice"}}
    result = executor_node({"messages": [HumanMessage(content="hola")]}, parent)
    assert seen["config"]["callbacks"] == ["keep-streaming"]
    assert seen["config"]["configurable"]["thread_id"] == "voice"
    assert seen["messages"][0].content == JARVIS_PERSONA
    assert result["messages"][0].content == "Hecho, maestro."


def test_bind_executor_tools_attaches_tavily_and_hunter():
    """El research_agent no puede actuar si el LLM no ve web_search / Hunter."""
    from jarvis.llms import OfflineChatModel

    bound, tools = bind_executor_tools("research_agent")
    names = {t.name for t in tools}
    assert {"web_search", "find_public_emails", "advanced_dork_search"} <= names
    assert isinstance(bound, OfflineChatModel)
    assert {t.name for t in bound.bound_tools} == names


def test_executor_calls_bind_tools_before_the_llm(monkeypatch):
    seen: dict = {}

    class _LLM:
        def bind_tools(self, tools):
            seen["names"] = [t.name for t in tools]
            return self

        def invoke(self, _messages, config=None):
            return AIMessage(content="ok")

    monkeypatch.setattr("jarvis.agent_core.get_executor_model", lambda: _LLM())
    from jarvis.agent_core import executor_node

    executor_node({"messages": [HumanMessage(content="investiga")], "active_agent": "research_agent"})
    assert "web_search" in seen["names"]
    assert "find_public_emails" in seen["names"]


def test_core_graph_runs_web_search_through_toolnode(monkeypatch):
    """bind_tools → tool_call del ejecutor → ToolNode (Tavily) → resultado en el estado."""
    monkeypatch.setattr(
        "jarvis.agents.research_agent._public_search",
        lambda query, **_opts: {
            "provider": "tavily",
            "query": query,
            "results": [{"title": "Ada Lovelace", "url": "https://example.com/ada"}],
        },
    )
    state = run_core_agent(
        "Investiga a Ada Lovelace en Google y recopila información pública",
        active_agent="research_agent",
    )
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "web_search" in tools_used
    assert any("Ada" in str(r.get("output") or "") for r in state.get("tool_results") or [])


def test_streamed_tool_call_chunks_become_tool_calls():
    """OpenRouter suelta tool_calls a trozos: hay que reensamblarlos para ToolNode."""
    from langchain_core.messages import AIMessageChunk

    from jarvis.agent_core import _as_ai_message

    first = AIMessageChunk(
        content="",
        tool_call_chunks=[{"index": 0, "id": "c1", "name": "web_search", "args": ""}],
    )
    second = AIMessageChunk(
        content="",
        tool_call_chunks=[{"index": 0, "id": None, "name": None, "args": '{"query": "Ada"}'}],
    )
    assembled = first + second
    message = _as_ai_message(assembled)
    assert message.tool_calls
    assert message.tool_calls[0]["name"] == "web_search"
    assert message.tool_calls[0]["args"]["query"] == "Ada"


def test_executor_keeps_tool_calls_when_stream_yields_a_full_message(monkeypatch):
    """OfflineChatModel.stream() emite un AIMessage: hay que conservar tool_calls."""
    from jarvis.agent_core import executor_node

    class _OfflineStyle:
        def bind_tools(self, _tools):
            return self

        def stream(self, _messages, config=None):
            yield AIMessage(
                content="",
                tool_calls=[
                    {"name": "calculate_expression", "args": {"expression": "17*24"}, "id": "c1"}
                ],
            )

    monkeypatch.setattr("jarvis.agent_core.get_executor_model", lambda: _OfflineStyle())
    result = executor_node({"messages": [HumanMessage(content="¿Cuánto es 17 * 24?")]})
    calls = getattr(result["messages"][0], "tool_calls", None) or []
    assert calls and calls[0]["name"] == "calculate_expression"


def test_core_graph_compiles():
    graph = compile_core_graph()
    assert graph is not None
    mermaid = graph.get_graph().draw_mermaid()
    assert "planner" in mermaid
    assert "executor" in mermaid
    assert "tools" in mermaid
    assert "executor --> tools" in mermaid or "executor -.-> tools" in mermaid or "tools" in mermaid


def test_core_calculator_offline():
    state = run_core_agent("¿Cuánto es 17 * 24?")
    assert "408" in extract_answer(state)
    tools_used = [r["tool"] for r in state.get("tool_results") or []]
    assert "calculate_expression" in tools_used
    assert state.get("task_complete") is True


def test_conditional_edges_error_returns_to_planner():
    state: AgentState = {"error": "Error: boom", "task_complete": False, "messages": []}
    assert route_after_tools(state) == "planner"


def test_conditional_edges_complete_ends():
    assert route_after_planner({"task_complete": True}) == "end"
    assert route_after_planner({"task_complete": False}) == "executor"


def test_tools_return_to_planner_once_the_loop_budget_is_spent():
    spent = {"hops": loop_budget(), "error": None, "messages": []}
    assert route_after_tools(spent) == "planner"
    fresh: AgentState = {"hops": 0, "error": None, "messages": []}
    assert route_after_tools(fresh) == "executor"


def test_a_planner_that_never_finishes_still_closes_the_turn(monkeypatch):
    """Sin tope, planificador ↔ ejecutor giraban hasta el GraphRecursionError."""
    calls = {"planner": 0}

    class _NeverDone:
        def with_structured_output(self, _schema):
            return self

        def invoke(self, _messages):
            calls["planner"] += 1
            return Plan(reasoning="sigo dándole vueltas", tasks=["seguir"], is_complete=False)

    class _NoToolCalls:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            return AIMessage(content="Borrador sin herramientas.")

    monkeypatch.setattr("jarvis.agent_core.get_planner_model", lambda: _NeverDone())
    monkeypatch.setattr("jarvis.agent_core.get_executor_model", lambda: _NoToolCalls())

    state = run_core_agent("dale vueltas para siempre")
    assert state.get("task_complete") is True
    assert extract_answer(state) == "Borrador sin herramientas."
    assert calls["planner"] <= loop_budget() + 1


def test_executor_routes_to_tools_on_tool_calls():
    state: AgentState = {
        "messages": [
            HumanMessage(content="calc"),
            AIMessage(
                content="",
                tool_calls=[{"name": "calculate_expression", "args": {"expression": "1+1"}, "id": "c1"}],
            ),
        ]
    }
    assert route_after_executor(state) == "tools"
    state["messages"] = [AIMessage(content="listo")]
    assert route_after_executor(state) == "planner"


def _broken_structured_model(error: Exception | None = None, returns=None):
    """Modelo cuyo `with_structured_output().invoke` falla o devuelve algo que no es el esquema."""

    class _Structured:
        def invoke(self, _messages, config=None):
            if error is not None:
                raise error
            return returns

    class _Model:
        def with_structured_output(self, _schema, **_kwargs):
            return _Structured()

    return _Model()


def test_the_planner_and_router_never_stream_their_structured_output(monkeypatch):
    """Dentro de `astream_events` LangChain pasa a stream cualquier invoke; el JSON de
    `Plan`/`RouteDecision` reensamblado por trozos es lo que dispara el aviso
    `PydanticSerializationUnexpectedValue(field_name='parsed')` y lo que puede
    llegar truncado. Planificador y Supervisor lo piden en una sola petición."""
    from jarvis.config import get_settings
    from jarvis.llms import get_executor_model, get_planner_model, get_supervisor_model

    monkeypatch.setenv("JARVIS_OFFLINE", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-no-network")
    monkeypatch.setenv("PLANNER_PROVIDER", "openai")
    get_settings.cache_clear()
    try:
        assert get_planner_model().disable_streaming is True
        assert get_supervisor_model().disable_streaming is True
        executor = get_executor_model()
        assert executor.streaming is True, "el ejecutor sí habla en directo para Vapi"
        assert executor.disable_streaming is False
        assert "llama-3.3-70b-instruct:free" in (executor.model_name or "")
        base = str(getattr(executor, "openai_api_base", None) or getattr(executor, "base_url", "") or "")
        assert "openrouter.ai/api/v1" in base
        from jarvis.tools import tools_by_agent

        research = tools_by_agent("research_agent")
        assert {t.name for t in research} >= {"web_search", "advanced_dork_search"}
        bound = executor.bind_tools(research)
        assert callable(bound.invoke)
    finally:
        get_settings.cache_clear()


def test_invoke_structured_accepts_dicts_and_refuses_anything_else():
    from jarvis.llms import StructuredOutputError, invoke_structured

    as_dict = _broken_structured_model(returns={"reasoning": "ok", "is_complete": True, "final_answer": "Hola"})
    plan = invoke_structured(as_dict, Plan, [])
    assert isinstance(plan, Plan) and plan.final_answer == "Hola"

    wrapped = _broken_structured_model(returns={"raw": object(), "parsed": Plan(reasoning="r"), "parsing_error": None})
    assert invoke_structured(wrapped, Plan, []).reasoning == "r"

    import pytest

    with pytest.raises(StructuredOutputError):
        invoke_structured(_broken_structured_model(returns=None), Plan, [])
    with pytest.raises(StructuredOutputError):
        invoke_structured(_broken_structured_model(returns={"reasoning": 3.5, "tasks": "no-es-lista"}), Plan, [])
    with pytest.raises(StructuredOutputError) as info:
        invoke_structured(_broken_structured_model(error=ValueError("stream cortado")), Plan, [])
    assert "stream cortado" in str(info.value)
    assert isinstance(info.value.__cause__, ValueError)


def test_a_planner_that_cannot_be_parsed_closes_the_turn_speaking(monkeypatch, caplog):
    """El fallo de parseo no sube por el grafo: se convierte en una frase y un `error`."""
    import logging

    from jarvis.agent_core import planner_node
    from jarvis.prompts import NEURAL_ERROR_REPLY

    monkeypatch.setattr(
        "jarvis.agent_core.get_planner_model",
        lambda: _broken_structured_model(error=ValueError("Plan: JSON truncado")),
    )
    caplog.set_level(logging.ERROR, logger="jarvis.agent_core")
    state: AgentState = {"messages": [HumanMessage(content="investiga a alguien")], "active_agent": "research_agent"}
    update = planner_node(state)

    assert update["task_complete"] is True
    assert update["final_answer"] == NEURAL_ERROR_REPLY
    assert update["messages"][0].content == NEURAL_ERROR_REPLY
    assert "JSON truncado" in (update["error"] or "")
    records = [rec for rec in caplog.records if "[PLANNER]" in rec.getMessage()]
    assert records and records[0].exc_info, "la traza tiene que quedar en el log de Render"


def test_a_planner_that_cannot_be_parsed_keeps_the_executor_draft(monkeypatch):
    """Si el ejecutor ya redactó, eso es lo que se dice; el JSON de una tool nunca."""
    from jarvis.agent_core import planner_node
    from jarvis.prompts import NEURAL_ERROR_REPLY

    monkeypatch.setattr(
        "jarvis.agent_core.get_planner_model",
        lambda: _broken_structured_model(error=RuntimeError("proveedor caído")),
    )
    with_draft: AgentState = {
        "messages": [HumanMessage(content="hola"), AIMessage(content="Ada Lovelace fue matemática, maestro.")],
    }
    assert planner_node(with_draft)["final_answer"] == "Ada Lovelace fue matemática, maestro."

    only_tools: AgentState = {
        "messages": [HumanMessage(content="hola")],
        "tool_results": [{"tool": "web_search", "output": '{"results": []}', "ok": True}],
    }
    assert planner_node(only_tools)["final_answer"] == NEURAL_ERROR_REPLY
