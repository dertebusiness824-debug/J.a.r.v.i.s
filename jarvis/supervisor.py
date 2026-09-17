"""Supervisor LangGraph: enruta el prompt, delega contexto y espera el reporte del especialista."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from jarvis.agent_core import extract_answer
from jarvis.agents import SPECIALIST_NODES
from jarvis.config import get_settings
from jarvis.llms import RouteDecision, get_supervisor_model, last_user_text
from jarvis.memory import get_memory
from jarvis.prompts import SUPERVISOR_PROMPT
from jarvis.state import AgentState, SpecialistName

SpecialistTarget = Literal[
    "code_agent",
    "comms_agent",
    "shop_agent",
    "research_agent",
    "general",
    "__end__",
]

_SUPERVISOR_GRAPH = None
_CHECKPOINTER = None


def retrieve_node(state: AgentState) -> dict[str, Any]:
    """Memoria + reset de hop por cada turno de usuario (checkpointer)."""
    query = state.get("user_query") or last_user_text(state.get("messages") or [])
    return {
        "retrieved_context": get_memory().retrieve(query),
        "user_query": query,
        "hops": 0,
        "task_complete": False,
        "error": None,
        "active_agent": "",
        "next_agent": "FINISH",
        "final_answer": "",
        "planner_retries": 0,
        "tool_results": [],
        "plan": [],
        "visited_agents": [],
        "delegation_log": [],
        "last_rationale": "",
    }


def routing_card(state: AgentState) -> str:
    visited = state.get("visited_agents") or []
    last = (state.get("final_answer") or "")[:500]
    log = state.get("delegation_log") or []
    reports = "\n".join(f"- {h.get('agent')}: {h.get('result')}" for h in log) or "ninguno"
    return (
        f"Consulta: {state.get('user_query') or ''}\n"
        f"Visitados: {', '.join(visited) or 'ninguno'}\n"
        f"Último especialista: {state.get('active_agent') or 'ninguno'}\n"
        f"Último resultado: {last or 'N/A'}\n"
        f"Reportes:\n{reports}\n"
    )


def _route_with_llm(state: AgentState) -> RouteDecision:
    model = get_supervisor_model().with_structured_output(RouteDecision)
    return model.invoke(
        [
            SystemMessage(content=SUPERVISOR_PROMPT),
            HumanMessage(content=routing_card(state)),
        ]
    )


def supervisor_node(state: AgentState) -> Command[SpecialistTarget]:
    hops = int(state.get("hops") or 0) + 1
    settings = get_settings()
    visited = list(state.get("visited_agents") or [])

    if hops > settings.jarvis_max_iterations:
        return Command(
            goto=END,
            update={
                "hops": hops,
                "task_complete": True,
                "next_agent": "FINISH",
                "final_answer": state.get("final_answer")
                or "Se alcanzó el límite de hops del supervisor.",
            },
        )

    decision = _route_with_llm(state)
    destination: SpecialistName = decision.next_agent

    if destination == "FINISH" or destination in visited:
        synthesized = _synthesize_answer(state)
        return Command(
            goto=END,
            update={
                "hops": hops,
                "next_agent": "FINISH",
                "task_complete": True,
                "final_answer": synthesized,
                "last_rationale": decision.rationale,
            },
        )

    return Command(
        goto=destination,
        update={
            "hops": hops,
            "next_agent": destination,
            "active_agent": destination,
            "task_complete": False,
            "last_rationale": decision.rationale,
        },
    )


def _synthesize_answer(state: AgentState) -> str:
    log = state.get("delegation_log") or []
    if len(log) == 1:
        return str(log[0].get("result") or state.get("final_answer") or "")
    if log:
        parts = [f"{h.get('agent')}: {h.get('result')}" for h in log]
        return " | ".join(parts)
    return state.get("final_answer") or extract_answer(state) or ""


def compile_supervisor_graph(*, checkpointer: Any | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("supervisor", supervisor_node)
    # Cada especialista, incluido research_agent, vuelve al Supervisor con
    # Command(goto="supervisor") tras emitir su delegation_log (make_specialist_node).
    for name, node in SPECIALIST_NODES.items():
        builder.add_node(name, node)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "supervisor")
    return builder.compile(checkpointer=checkpointer)


def get_supervisor_graph():
    global _SUPERVISOR_GRAPH, _CHECKPOINTER
    if _SUPERVISOR_GRAPH is None:
        _CHECKPOINTER = MemorySaver()
        _SUPERVISOR_GRAPH = compile_supervisor_graph(checkpointer=_CHECKPOINTER)
    return _SUPERVISOR_GRAPH


def run_jarvis(query: str, *, session_id: str = "cli") -> AgentState:
    settings = get_settings()
    graph = get_supervisor_graph()
    state: AgentState = {
        "messages": [HumanMessage(content=query)],
        "user_query": query,
        "plan": [],
        "tool_results": [],
        "retrieved_context": "",
        "next_agent": "FINISH",
        "active_agent": "",
        "error": None,
        "task_complete": False,
        "final_answer": "",
        "hops": 0,
        "planner_retries": 0,
        "specialist_system_prompt": "",
        "visited_agents": [],
        "delegation_log": [],
        "last_rationale": "",
    }
    result = graph.invoke(
        state,
        {
            "recursion_limit": settings.jarvis_recursion_limit,
            "configurable": {"thread_id": session_id},
        },
    )
    answer = extract_answer(result)
    if answer:
        get_memory().remember(f"Q: {query}\nA: {answer}")
    return result


def graph_mermaid() -> str:
    return get_supervisor_graph().get_graph().draw_mermaid()
