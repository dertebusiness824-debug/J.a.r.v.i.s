"""Supervisor LangGraph: enruta el prompt a un especialista y espera el resultado."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from jarvis.agent_core import compile_core_graph, extract_answer
from jarvis.config import get_settings
from jarvis.llms import RouteDecision, get_supervisor_model, last_user_text
from jarvis.memory import get_memory
from jarvis.prompts import CODE_PROMPT, COMMS_PROMPT, SHOP_PROMPT, SUPERVISOR_PROMPT
from jarvis.state import AgentState, SpecialistName

SpecialistTarget = Literal["code_agent", "comms_agent", "shop_agent", "general", "__end__"]

_SPECIALIST_PROMPTS = {
    "code_agent": CODE_PROMPT,
    "comms_agent": COMMS_PROMPT,
    "shop_agent": SHOP_PROMPT,
    "general": "",
}

_CORE_SUBGRAPH = None
_SUPERVISOR_GRAPH = None
_CHECKPOINTER = None


def _core_subgraph():
    global _CORE_SUBGRAPH
    if _CORE_SUBGRAPH is None:
        _CORE_SUBGRAPH = compile_core_graph()
    return _CORE_SUBGRAPH


def retrieve_node(state: AgentState) -> dict[str, Any]:
    query = state.get("user_query") or last_user_text(state.get("messages") or [])
    return {"retrieved_context": get_memory().retrieve(query), "user_query": query}


def _route_with_llm(state: AgentState) -> RouteDecision:
    model = get_supervisor_model().with_structured_output(RouteDecision)
    snapshot = []
    if state.get("retrieved_context"):
        snapshot.append(SystemMessage(content=f"Memoria:\n{state['retrieved_context']}"))
    if state.get("final_answer"):
        snapshot.append(SystemMessage(content=f"Último resultado de especialista:\n{state['final_answer']}"))
        snapshot.append(SystemMessage(content="Si el resultado ya responde al usuario, elige FINISH."))
    snapshot.append(SystemMessage(content=SUPERVISOR_PROMPT))
    snapshot.extend(state.get("messages") or [])
    return model.invoke(snapshot)


def supervisor_node(state: AgentState) -> Command[SpecialistTarget]:
    hops = int(state.get("hops") or 0) + 1
    settings = get_settings()

    if hops > settings.jarvis_max_iterations:
        return Command(
            goto=END,
            update={
                "hops": hops,
                "task_complete": True,
                "next_agent": "FINISH",
                "final_answer": state.get("final_answer") or "Se alcanzó el límite de hops del supervisor.",
            },
        )

    returning = bool(state.get("active_agent")) and hops > 1
    if returning and state.get("task_complete") and not state.get("error"):
        return Command(
            goto=END,
            update={"hops": hops, "next_agent": "FINISH", "task_complete": True},
        )

    decision = _route_with_llm(state)
    destination: SpecialistName = decision.next_agent
    if destination == "FINISH" or (returning and destination == state.get("active_agent")):
        return Command(
            goto=END,
            update={
                "hops": hops,
                "next_agent": "FINISH",
                "task_complete": True,
                "final_answer": state.get("final_answer")
                or extract_answer(state)
                or decision.rationale,
            },
        )

    return Command(
        goto=destination,
        update={
            "hops": hops,
            "next_agent": destination,
            "active_agent": destination,
            "task_complete": False,
            "specialist_system_prompt": _SPECIALIST_PROMPTS.get(destination, ""),
        },
    )


def _specialist_node(name: str):
    def _run(state: AgentState) -> Command[Literal["supervisor"]]:
        inbound_len = len(state.get("messages") or [])
        results_len = len(state.get("tool_results") or [])
        payload = {
            **state,
            "active_agent": name,
            "specialist_system_prompt": _SPECIALIST_PROMPTS.get(name, ""),
            "task_complete": False,
            "planner_retries": 0,
        }
        output = _core_subgraph().invoke(payload, {"recursion_limit": get_settings().jarvis_recursion_limit})
        new_messages = (output.get("messages") or [])[inbound_len:]
        new_results = (output.get("tool_results") or [])[results_len:]
        answer = extract_answer(output)
        return Command(
            goto="supervisor",
            update={
                "messages": new_messages,
                "plan": output.get("plan") or [],
                "tool_results": new_results,
                "error": output.get("error"),
                "task_complete": bool(output.get("task_complete")),
                "final_answer": answer,
                "active_agent": name,
                "retrieved_context": output.get("retrieved_context") or state.get("retrieved_context") or "",
            },
        )

    _run.__name__ = name
    return _run


def compile_supervisor_graph(*, checkpointer: Any | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("code_agent", _specialist_node("code_agent"))
    builder.add_node("comms_agent", _specialist_node("comms_agent"))
    builder.add_node("shop_agent", _specialist_node("shop_agent"))
    builder.add_node("general", _specialist_node("general"))

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
