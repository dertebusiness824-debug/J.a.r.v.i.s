from langchain_core.messages import AIMessage, HumanMessage

from jarvis.agent_core import (
    compile_core_graph,
    extract_answer,
    route_after_executor,
    route_after_planner,
    route_after_tools,
    run_core_agent,
)
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
