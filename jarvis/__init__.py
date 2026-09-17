"""Proyecto Jarvis v2.0 — orquestación multi-agente con LangGraph."""

from __future__ import annotations

from typing import Any

__all__ = [
    "compile_core_graph",
    "compile_supervisor_graph",
    "run_core_agent",
    "run_jarvis",
    "__version__",
]

__version__ = "2.0.0"


def __getattr__(name: str) -> Any:
    if name in {"compile_core_graph", "run_core_agent"}:
        from jarvis.agent_core import compile_core_graph, run_core_agent

        return compile_core_graph if name == "compile_core_graph" else run_core_agent
    if name in {"compile_supervisor_graph", "run_jarvis"}:
        from jarvis.supervisor import compile_supervisor_graph, run_jarvis

        return compile_supervisor_graph if name == "compile_supervisor_graph" else run_jarvis
    raise AttributeError(f"module 'jarvis' has no attribute {name!r}")
