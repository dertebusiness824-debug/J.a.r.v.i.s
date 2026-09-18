"""Runner compartido: el Supervisor invoca un especialista y espera su reporte."""

from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import AIMessage
from langgraph.types import Command

from jarvis.agent_core import compile_core_graph, extract_answer, initial_state
from jarvis.config import get_settings
from jarvis.prompts import NEURAL_ERROR_REPLY
from jarvis.state import AgentState, DelegationHop

logger = logging.getLogger(__name__)

_CORE_SUBGRAPH = None


def core_subgraph():
    global _CORE_SUBGRAPH
    if _CORE_SUBGRAPH is None:
        _CORE_SUBGRAPH = compile_core_graph()
    return _CORE_SUBGRAPH


def reset_core_subgraph() -> None:
    global _CORE_SUBGRAPH
    _CORE_SUBGRAPH = None


def _failed_report(payload: AgentState, exc: BaseException) -> AgentState:
    """Estado de cierre de un especialista que reventó: frase hablable y error anotado."""
    return {
        **payload,
        "messages": [*(payload.get("messages") or []), AIMessage(content=NEURAL_ERROR_REPLY)],
        "final_answer": NEURAL_ERROR_REPLY,
        "error": f"{type(exc).__name__}: {exc}",
        "task_complete": True,
    }


def make_specialist_node(name: str, prompt: str):
    """Nodo LangGraph: recibe contexto del Supervisor, ejecuta el subgrafo y devuelve el resultado."""

    def _run(state: AgentState) -> Command[Literal["supervisor"]]:
        query = state.get("user_query") or ""
        prior = list(state.get("delegation_log") or [])
        prior_ctx = "\n".join(f"- {hop.get('agent')}: {hop.get('result')}" for hop in prior)
        extra_prompt = prompt
        if prior_ctx:
            extra_prompt = (
                f"{prompt}\n\nResultados de especialistas previos (contexto, no los repitas tal cual):\n{prior_ctx}"
            )
        payload = initial_state(query, active_agent=name, system_prompt=extra_prompt)
        payload["retrieved_context"] = state.get("retrieved_context") or ""
        if name == "research_agent":
            from jarvis.hud_live import mark_researching

            mark_researching(ttl=60)
        try:
            output = core_subgraph().invoke(payload, {"recursion_limit": get_settings().jarvis_recursion_limit})
        except Exception as exc:
            # Un especialista roto no puede tumbar el turno de voz. `CancelledError`
            # no es `Exception`: si Vapi cuelga, la cancelación sigue subiendo.
            logger.exception(
                "💥 [%s] el subgrafo falló; reporto el error al Supervisor en vez de romper el stream",
                name,
            )
            output = _failed_report(payload, exc)
        finally:
            if name == "research_agent":
                from jarvis.hud_live import clear_researching

                clear_researching()
        answer = extract_answer(output)
        hop: DelegationHop = {
            "agent": name,
            "rationale": state.get("last_rationale") or "",
            "result": answer,
            "ok": not bool(output.get("error")),
        }
        visited = list(state.get("visited_agents") or [])
        if name not in visited:
            visited.append(name)
        report = AIMessage(content=f"[{name}] {answer}", name=name)
        return Command(
            goto="supervisor",
            update={
                "messages": [report],
                "plan": output.get("plan") or [],
                "tool_results": list(state.get("tool_results") or []) + list(output.get("tool_results") or []),
                "error": output.get("error"),
                "task_complete": bool(output.get("task_complete")),
                "final_answer": answer,
                "active_agent": name,
                "visited_agents": visited,
                "delegation_log": [*prior, hop],
            },
        )

    _run.__name__ = name
    _run.__doc__ = f"Especialista {name}: ejecuta su subgrafo y reporta al Supervisor."
    return _run
