"""Grafo interno de Jarvis: Planificador → Ejecutor → Herramientas.

Imports modernos de `langchain-core` y `langgraph`. Cada especialista reutiliza
este StateGraph con su propio set de tools y prompt de sistema.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from jarvis.config import get_settings
from jarvis.llms import Plan, get_executor_model, get_planner_model, last_user_text
from jarvis.memory import get_memory
from jarvis.prompts import EXECUTOR_PROMPT, PLANNER_PROMPT
from jarvis.state import AgentState, TaskItem, ToolResult
from jarvis.tools import CORE_TOOLS, tools_by_agent


def retrieve_node(state: AgentState) -> dict[str, Any]:
    """Inyecta contexto semántico en el estado antes de planificar."""
    query = state.get("user_query") or last_user_text(state.get("messages") or [])
    context = get_memory().retrieve(query)
    return {"retrieved_context": context, "user_query": query, "tool_results": []}


def _planner_messages(state: AgentState) -> list:
    specialist = state.get("specialist_system_prompt") or ""
    context = state.get("retrieved_context") or "(vacío)"
    extra = [
        SystemMessage(content=f"{PLANNER_PROMPT}\n{specialist}".strip()),
        SystemMessage(content=f"Contexto de memoria vectorial:\n{context}"),
    ]
    if state.get("error"):
        extra.append(SystemMessage(content=f"Error de herramienta previo: {state['error']}"))
    results = state.get("tool_results") or []
    if results:
        rendered = "\n".join(
            f"- {r.get('tool')}: {r.get('output')}" for r in results[-6:]
        )
        extra.append(SystemMessage(content=f"Resultados de herramientas:\n{rendered}"))
    last = (state.get("messages") or [])[-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None) and last.content:
        extra.append(SystemMessage(content=f"Borrador del ejecutor:\n{last.content}"))
    extra.extend(state.get("messages") or [])
    return extra


def planner_node(state: AgentState) -> dict[str, Any]:
    """Nodo de razonamiento: produce un plan estructurado o cierra la tarea."""
    settings = get_settings()
    retries = int(state.get("planner_retries") or 0)
    if state.get("error"):
        retries += 1

    planner = get_planner_model().with_structured_output(Plan)
    plan: Plan = planner.invoke(_planner_messages(state))

    tasks: list[TaskItem] = [
        {
            "id": uuid.uuid4().hex[:8],
            "description": item,
            "status": "pending",
            "assignee": "executor",
        }
        for item in plan.tasks
    ]

    exhausted = bool(state.get("error")) and retries >= settings.jarvis_max_iterations
    complete = bool(plan.is_complete) or exhausted
    answer = plan.final_answer or (state.get("error") if exhausted else None)

    updates: dict[str, Any] = {
        "plan": tasks,
        "task_complete": complete,
        "planner_retries": retries,
        "final_answer": answer or state.get("final_answer") or "",
    }
    if complete:
        updates["error"] = None if plan.is_complete else state.get("error")
        updates["messages"] = [AIMessage(content=answer or plan.reasoning)]
        if not tasks and state.get("plan"):
            updates["plan"] = [{**item, "status": "done"} for item in state["plan"]]
    elif not state.get("error"):
        updates["error"] = None
    return updates


def executor_node(state: AgentState) -> dict[str, Any]:
    """Nodo de ejecución: GPT-4o (o modelo offline) con function calling."""
    agent_name = state.get("active_agent") or "general"
    tools = tools_by_agent(agent_name)
    model = get_executor_model().bind_tools(tools)
    plan = state.get("plan") or []
    plan_text = "\n".join(f"- {t.get('description')}" for t in plan) or "(sin plan)"
    messages = [
        SystemMessage(content=EXECUTOR_PROMPT),
        SystemMessage(content=f"Plan vigente:\n{plan_text}"),
        *(state.get("messages") or []),
    ]
    response = model.invoke(messages)
    return {"messages": [response]}


def tools_node(state: AgentState) -> dict[str, Any]:
    """Ejecuta tool calls y registra resultados tipados en el estado."""
    agent_name = state.get("active_agent") or "general"
    tools = tools_by_agent(agent_name)
    raw = ToolNode(tools).invoke(state)
    new_messages = raw["messages"]

    last_ai = next(
        (m for m in reversed(state.get("messages") or []) if isinstance(m, AIMessage)),
        None,
    )
    calls = {c["id"]: c for c in (getattr(last_ai, "tool_calls", None) or [])}

    results: list[ToolResult] = []
    error: str | None = None
    for msg in new_messages:
        if not isinstance(msg, ToolMessage):
            continue
        content = str(msg.content)
        ok = not content.lower().startswith("error")
        call = calls.get(msg.tool_call_id) or {}
        results.append(
            {
                "tool": msg.name or str(call.get("name") or "unknown"),
                "args": dict(call.get("args") or {}),
                "output": content,
                "ok": ok,
            }
        )
        if not ok:
            error = content
    prior = list(state.get("tool_results") or [])
    return {"messages": new_messages, "tool_results": prior + results, "error": error}


def route_after_planner(state: AgentState) -> Literal["executor", "end"]:
    if state.get("task_complete"):
        return "end"
    return "executor"


def route_after_executor(state: AgentState) -> Literal["tools", "planner"]:
    last = (state.get("messages") or [None])[-1]
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "planner"


def route_after_tools(state: AgentState) -> Literal["planner", "executor"]:
    if state.get("error"):
        return "planner"
    return "executor"


def compile_core_graph(*, checkpointer: Any | None = None):
    """Compila el grafo cíclico Planificador → Ejecutor → Herramientas."""
    builder = StateGraph(AgentState)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("tools", tools_node)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {"executor": "executor", "end": END},
    )
    builder.add_conditional_edges(
        "executor",
        route_after_executor,
        {"tools": "tools", "planner": "planner"},
    )
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {"planner": "planner", "executor": "executor"},
    )
    return builder.compile(checkpointer=checkpointer)


_CORE_GRAPH = None


def get_core_graph():
    global _CORE_GRAPH
    if _CORE_GRAPH is None:
        _CORE_GRAPH = compile_core_graph()
    return _CORE_GRAPH


def initial_state(query: str, *, active_agent: str = "general", system_prompt: str = "") -> AgentState:
    return {
        "messages": [HumanMessage(content=query)],
        "user_query": query,
        "plan": [],
        "tool_results": [],
        "retrieved_context": "",
        "next_agent": "general",
        "active_agent": active_agent,
        "error": None,
        "task_complete": False,
        "final_answer": "",
        "hops": 0,
        "planner_retries": 0,
        "specialist_system_prompt": system_prompt,
    }


def extract_answer(state: AgentState) -> str:
    if state.get("final_answer"):
        return str(state["final_answer"])
    from jarvis.llms import since_last_human

    for msg in reversed(since_last_human(state.get("messages") or [])):
        if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
            return str(msg.content)
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
            return str(msg.content)
    return ""


def run_core_agent(query: str, *, active_agent: str = "general", system_prompt: str = "") -> AgentState:
    settings = get_settings()
    graph = get_core_graph()
    return graph.invoke(
        initial_state(query, active_agent=active_agent, system_prompt=system_prompt),
        {"recursion_limit": settings.jarvis_recursion_limit},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis agent_core — planificador + herramienta de prueba")
    parser.add_argument("query", nargs="*", default=["¿Cuánto es 17 * 24?"])
    args = parser.parse_args()
    query = " ".join(args.query)
    result = run_core_agent(query)
    print(extract_answer(result) or result)


if __name__ == "__main__":
    main()


# Referencia para el ejecutor general: las tools core deben permanecer importables.
__all__ = [
    "CORE_TOOLS",
    "compile_core_graph",
    "run_core_agent",
    "extract_answer",
    "initial_state",
    "get_core_graph",
]
