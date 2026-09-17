"""Esquemas Pydantic de la API Jarvis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InvokeRequest(BaseModel):
    message: str = Field(min_length=1, description="Prompt del usuario.")
    session_id: str = Field(default="default", description="Hilo de conversación (checkpointer).")


class ToolResultOut(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: str
    ok: bool = True


class TaskOut(BaseModel):
    id: str | None = None
    description: str | None = None
    status: str | None = None
    assignee: str | None = None


class InvokeResponse(BaseModel):
    answer: str
    agent: str
    visited_agents: list[str] = Field(default_factory=list)
    plan: list[TaskOut] = Field(default_factory=list)
    tool_results: list[ToolResultOut] = Field(default_factory=list)
    retrieved_context: str = ""
    error: str | None = None
    offline: bool = False


class DirectiveRequest(BaseModel):
    command: str = Field(default="", description="Orden escrita desde el HUD (commandInput).")
    message: str = Field(default="", description="Alias de command.")
    session_id: str = Field(default="hud", description="Hilo de conversación.")

    def text(self) -> str:
        return (self.command or self.message or "").strip()


class InboxStatusResponse(BaseModel):
    whatsapp: int = 0
    correo: int = 0
    gmail: int = 0
    webmail: int = 0
    total: int = 0
    pending: int = 0


class HealthResponse(BaseModel):
    status: str
    version: str
    offline: bool
    agents: list[str]
