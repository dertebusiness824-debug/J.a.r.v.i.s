"""Proyecto Jarvis v2.0 — orquestación multi-agente con LangGraph."""

from jarvis.agent_core import compile_core_graph, run_core_agent
from jarvis.supervisor import compile_supervisor_graph, run_jarvis

__all__ = [
    "compile_core_graph",
    "compile_supervisor_graph",
    "run_core_agent",
    "run_jarvis",
    "__version__",
]

__version__ = "2.0.0"
