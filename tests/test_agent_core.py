from langchain_core.messages import AIMessage, HumanMessage

from jarvis.agent_core import (
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


def test_core_graph_compiles():
    graph = compile_core_graph()
    assert graph is not None
    mermaid = graph.get_graph().draw_mermaid()
    assert "planner" in mermaid
    assert "executor" in mermaid
    assert "tools" in mermaid


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
