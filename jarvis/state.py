"""Estado global del grafo Jarvis (LangGraph StateGraph)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

SpecialistName = Literal[
    "code_agent",
    "comms_agent",
    "shop_agent",
    "research_agent",
    "general",
    "FINISH",
]


class TaskItem(TypedDict, total=False):
    id: str
    description: str
    status: Literal["pending", "in_progress", "done", "error"]
    assignee: str | None


class ToolResult(TypedDict, total=False):
    tool: str
    args: dict[str, Any]
    output: str
    ok: bool


class DelegationHop(TypedDict, total=False):
    """Reporte que un especialista devuelve al Supervisor."""

    agent: str
    rationale: str
    result: str
    ok: bool


class AgentState(TypedDict, total=False):
    """Estado compartido entre Supervisor y especialistas."""

    messages: Annotated[list[AnyMessage], add_messages]
    user_query: str
    plan: list[TaskItem]
    tool_results: list[ToolResult]
    retrieved_context: str
    next_agent: SpecialistName
    active_agent: str
    error: str | None
    task_complete: bool
    final_answer: str
    hops: int
    planner_retries: int
    specialist_system_prompt: str
    visited_agents: list[str]
    delegation_log: list[DelegationHop]
    last_rationale: str
