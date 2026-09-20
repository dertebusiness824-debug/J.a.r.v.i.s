"""Grafo interno de Jarvis: Planificador → Ejecutor → Herramientas.

Imports modernos de `langchain-core` y `langgraph`. Cada especialista reutiliza
este StateGraph con su propio set de tools y prompt de sistema.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from typing import Any, Literal

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from jarvis.config import get_settings
from jarvis.llms import (
    Plan,
    StructuredOutputError,
    get_executor_model,
    get_planner_model,
    invoke_structured,
    last_user_text,
)
from jarvis.memory import get_memory
from jarvis.prompts import (
    EXECUTOR_PROMPT,
    JARVIS_PERSONA,
    NEURAL_ERROR_REPLY,
    PLANNER_PROMPT,
)
from jarvis.state import AgentState, TaskItem, ToolResult
from jarvis.tools import CORE_TOOLS, tools_by_agent

logger = logging.getLogger(__name__)


def loop_budget() -> int:
    """Vueltas de ejecutor permitidas antes de cerrar la tarea a la fuerza.

    Se queda por debajo de `jarvis_recursion_limit` a propósito: si LangGraph corta
    primero, el turno muere con `GraphRecursionError` y sin respuesta que hablar.
    """
    settings = get_settings()
    return max(1, min(settings.jarvis_max_iterations, settings.jarvis_recursion_limit // 3))


def _executor_draft(state: AgentState) -> str | None:
    """Último texto que el ejecutor redactó para el usuario, si lo hay."""
    for msg in reversed(state.get("messages") or []):
        if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
            return str(msg.content)
    return None


def _loop_answer(state: AgentState) -> str:
    """Lo mejor que hay en el estado cuando el ciclo se corta: borrador o última tool."""
    draft = _executor_draft(state)
    if draft:
        return draft
    results = state.get("tool_results") or []
    if results:
        return str(results[-1].get("output") or "")
    return "No pude cerrar la tarea dentro del límite de iteraciones."


def _rescue_plan(state: AgentState) -> Plan:
    """Plan de cierre cuando el planificador no devolvió uno válido.

    Solo se rescata el borrador del ejecutor, que ya es prosa hablable; la salida
    cruda de una herramienta (JSON de búsqueda) no se pone en boca de Jarvis.
    """
    return Plan(
        reasoning="El planificador no devolvió un plan válido; se cierra el turno.",
        tasks=[],
        is_complete=True,
        final_answer=_executor_draft(state) or NEURAL_ERROR_REPLY,
    )


def retrieve_node(state: AgentState) -> dict[str, Any]:
    """Inyecta contexto semántico en el estado antes de planificar."""
    query = state.get("user_query") or last_user_text(state.get("messages") or [])
    context = get_memory().retrieve(query)
    return {"retrieved_context": context, "user_query": query, "tool_results": []}


def _planner_messages(state: AgentState) -> list:
    specialist = state.get("specialist_system_prompt") or ""
    context = state.get("retrieved_context") or "(vacío)"
    extra = [
        SystemMessage(content=JARVIS_PERSONA),
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

    planner_failure: str | None = None
    try:
        plan = invoke_structured(get_planner_model(), Plan, _planner_messages(state))
    except StructuredOutputError as exc:
        # Sin plan no hay siguiente paso, pero sí hay turno: se cierra hablando en
        # vez de dejar que la excepción tumbe el grafo y con él el stream de voz.
        planner_failure = str(exc)
        logger.exception(
            "🧩 [PLANNER] %s: el plan estructurado no llegó; cierro el turno con lo que hay",
            state.get("active_agent") or "general",
        )
        plan = _rescue_plan(state)

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
    loops = int(state.get("hops") or 0)
    stalled = loops >= loop_budget()
    complete = bool(plan.is_complete) or exhausted or stalled
    answer = plan.final_answer or (state.get("error") if exhausted else None)
    if stalled and not answer:
        answer = _loop_answer(state)
        logger.warning(
            "🔁 [CORE LOOP] %s no cerró en %d vueltas; respondo con lo que hay: %r",
            state.get("active_agent") or "general",
            loops,
            answer,
        )

    updates: dict[str, Any] = {
        "plan": tasks,
        "task_complete": complete,
        "planner_retries": retries,
        "final_answer": answer or state.get("final_answer") or "",
    }
    if complete:
        updates["error"] = None if plan.is_complete else state.get("error")
        if planner_failure:
            updates["error"] = planner_failure
        updates["messages"] = [AIMessage(content=answer or plan.reasoning)]
        if not tasks and state.get("plan"):
            updates["plan"] = [{**item, "status": "done"} for item in state["plan"]]
    elif not state.get("error"):
        updates["error"] = None
    return updates


def executor_node(state: AgentState, config: RunnableConfig = None) -> dict[str, Any]:  # type: ignore[assignment]
    """Nodo de ejecución: Llama 3.3 70B free vía OpenRouter (o modelo offline) con function calling."""
    agent_name = state.get("active_agent") or "general"
    tools = tools_by_agent(agent_name)
    model = get_executor_model().bind_tools(tools)
    plan = state.get("plan") or []
    plan_text = "\n".join(f"- {t.get('description')}" for t in plan) or "(sin plan)"
    messages = [
        SystemMessage(content=JARVIS_PERSONA),
        SystemMessage(content=EXECUTOR_PROMPT),
        SystemMessage(content=f"Plan vigente:\n{plan_text}"),
        *(state.get("messages") or []),
    ]
    # `config` lleva los callbacks de `astream_events`: sin ellos `.stream()`
    # abre un run huérfano y Vapi no ve ningún token (TTS en underrun).
    response = _stream_chat(model, messages, config)
    # El ejecutor está en los dos ciclos del grafo (con el planificador y con las
    # herramientas), así que contar sus visitas acota cualquier vuelta infinita.
    return {"messages": [response], "hops": int(state.get("hops") or 0) + 1}


def _as_ai_message(message: Any) -> AIMessage:
    if isinstance(message, AIMessage) and not isinstance(message, AIMessageChunk):
        return message
    return AIMessage(
        content=getattr(message, "content", "") or "",
        tool_calls=list(getattr(message, "tool_calls", None) or []),
        additional_kwargs=dict(getattr(message, "additional_kwargs", None) or {}),
        id=getattr(message, "id", None),
    )


def _invoke_chat(model: Any, messages: list, config: RunnableConfig | None) -> AIMessage:
    invoke = getattr(model, "invoke", None)
    if not callable(invoke):
        return AIMessage(content="")
    try:
        result = invoke(messages, config=config) if config is not None else invoke(messages)
    except TypeError:
        result = invoke(messages)
    return _as_ai_message(result)


def _stream_chat(
    model: Any,
    messages: list,
    config: RunnableConfig | None = None,
) -> AIMessage:
    """Consume el LLM token a token para que `astream_events` los vea al vuelo.

    `invoke()` con `streaming=True` a veces espera el mensaje entero antes de
    notificar; `.stream()` emite cada chunk. El `config` del nodo tiene que
    viajar con la llamada: LangGraph suele correr el ejecutor en un hilo y
    ahí no llegan los callbacks por contextvar. Sin ellos Vapi no recibe
    deltas y el TTS se vacía.

    Los dobles de test solo implementan `invoke`: si no hay `stream` usable,
    se cae ahí sin romper el turno.
    """
    stream = getattr(model, "stream", None)
    if callable(stream):
        assembled: AIMessageChunk | None = None
        try:
            iterator = stream(messages, config=config) if config is not None else stream(messages)
        except TypeError:
            iterator = stream(messages)
        complete: AIMessage | None = None
        try:
            for chunk in iterator:
                # BaseChatModel.stream() sin `_stream` suelta el AIMessage entero
                # (tool_calls incluidos). Recortarlo a un chunk de solo texto
                # es lo que dejaba al ejecutor offline sin herramientas.
                if isinstance(chunk, AIMessage) and not isinstance(chunk, AIMessageChunk):
                    if complete is None:
                        complete = chunk
                    else:
                        complete = AIMessage(
                            content=f"{complete.content}{chunk.content}",
                            tool_calls=list(getattr(chunk, "tool_calls", None) or complete.tool_calls),
                            additional_kwargs=dict(
                                getattr(chunk, "additional_kwargs", None)
                                or complete.additional_kwargs
                                or {}
                            ),
                            id=getattr(chunk, "id", None) or complete.id,
                        )
                    continue
                piece = (
                    chunk
                    if isinstance(chunk, AIMessageChunk)
                    else AIMessageChunk(
                        content=getattr(chunk, "content", "") or "",
                        additional_kwargs=dict(getattr(chunk, "additional_kwargs", None) or {}),
                    )
                )
                assembled = piece if assembled is None else assembled + piece
        except TypeError:
            return _invoke_chat(model, messages, config)
        if assembled is not None:
            return _as_ai_message(assembled)
        if complete is not None:
            return _as_ai_message(complete)
        return AIMessage(content="")
    return _invoke_chat(model, messages, config)


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
    if int(state.get("hops") or 0) >= loop_budget():
        # Se vuelve al planificador (no al ejecutor) para que cierre: el historial
        # ya tiene los ToolMessage de esta ronda, así que sigue siendo válido.
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
