"""Fábricas de LLMs (planificador vs ejecutor) y modelos offline para tests/demo."""

from __future__ import annotations

import re
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, Field

from jarvis.config import get_settings
from jarvis.state import SpecialistName


class RouteDecision(BaseModel):
    next_agent: SpecialistName = Field(
        description=(
            "Especialista a quien delegar: code_agent, comms_agent, shop_agent, "
            "general, o FINISH si la tarea ya está resuelta."
        )
    )
    rationale: str = Field(description="Justificación breve de la decisión.")


class Plan(BaseModel):
    reasoning: str = Field(description="Razonamiento conciso del planificador.")
    tasks: list[str] = Field(default_factory=list, description="Tareas pendientes para el ejecutor.")
    is_complete: bool = Field(default=False, description="True si ya hay respuesta final.")
    final_answer: str | None = Field(default=None, description="Respuesta para el usuario si is_complete.")


_MATH_RE = re.compile(
    r"(?P<expr>\d+(?:\.\d+)?(?:\s*[\+\-\*/x×]\s*\d+(?:\.\d+)?)+)",
    re.IGNORECASE,
)


def last_user_text(messages: list[BaseMessage] | list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
        if isinstance(msg, dict) and msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def _normalize_math(expr: str) -> str:
    return expr.replace("x", "*").replace("×", "*")


class OfflineChatModel(BaseChatModel):
    """Chat model determinista: no llama APIs. Sirve para tests y arranque sin claves."""

    role: str = "planner"
    bound_tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "jarvis-offline"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "OfflineChatModel":  # noqa: ARG002
        return self.model_copy(update={"bound_tools": list(tools), "role": "executor"})

    def with_structured_output(self, schema: type[BaseModel], **kwargs: Any):  # noqa: ARG002
        parent = self

        class _Structured:
            def invoke(self, input: Any, config: Any | None = None) -> BaseModel:  # noqa: A002, ARG002
                text = parent._flatten(input)
                if schema is RouteDecision:
                    return parent._route(text)
                if schema is Plan:
                    return parent._plan(text, input)
                return schema()  # type: ignore[call-arg]

        return _Structured()

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._act(messages))])

    def _flatten(self, input: Any) -> str:  # noqa: A002
        if isinstance(input, str):
            return input
        if isinstance(input, list):
            return last_user_text(input) or " ".join(
                str(getattr(m, "content", m)) for m in input if not isinstance(m, SystemMessage)
            )
        return str(input)

    def _route(self, text: str) -> RouteDecision:
        lowered = text.lower()
        if any(k in lowered for k in ("finish", "tarea completada", "ya está resuelto")):
            return RouteDecision(next_agent="FINISH", rationale="Tarea ya resuelta.")
        if any(
            k in lowered
            for k in (
                "código",
                "codigo",
                "archivo",
                "terminal",
                "python",
                "script",
                "sandbox",
                "git",
                "refactor",
                "leer fichero",
                "write file",
                "run_terminal",
            )
        ):
            return RouteDecision(next_agent="code_agent", rationale="Tarea de código / filesystem.")
        if any(
            k in lowered
            for k in ("whatsapp", "twilio", "sms", "mensaje", "webhook", "comunicación", "comunicacion")
        ):
            return RouteDecision(next_agent="comms_agent", rationale="Tarea de mensajería.")
        if any(
            k in lowered
            for k in (
                "shopify",
                "producto",
                "inventario",
                "pedido",
                "orden",
                "stock",
                "tienda",
                "ecommerce",
                "e-commerce",
            )
        ):
            return RouteDecision(next_agent="shop_agent", rationale="Tarea de e-commerce.")
        return RouteDecision(next_agent="general", rationale="Consulta general / herramientas core.")

    def _plan(self, text: str, raw: Any) -> Plan:
        messages = raw if isinstance(raw, list) else []
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        if tool_msgs:
            last = str(tool_msgs[-1].content)
            if last.lower().startswith("error"):
                return Plan(
                    reasoning="La herramienta falló; se reintenta con otro enfoque.",
                    tasks=["Reintentar la última herramienta con argumentos corregidos"],
                    is_complete=False,
                )
            return Plan(
                reasoning="Herramienta ejecutada. Se entrega el resultado.",
                tasks=[],
                is_complete=True,
                final_answer=last,
            )
        expr_match = _MATH_RE.search(text)
        if expr_match:
            expr = _normalize_math(expr_match.group("expr"))
            return Plan(
                reasoning="Se requiere la calculadora.",
                tasks=[f"Calcular {expr}"],
                is_complete=False,
            )
        lowered = text.lower()
        if any(k in lowered for k in ("hora", "fecha", "time")):
            return Plan(
                reasoning="Se requiere la hora actual.",
                tasks=["Obtener fecha/hora actual"],
                is_complete=False,
            )
        if any(
            k in lowered
            for k in ("shopify", "producto", "inventario", "pedido", "orden", "stock", "tienda")
        ):
            return Plan(
                reasoning="Consulta de e-commerce: se usan las tools de Shopify.",
                tasks=["Consultar Shopify"],
                is_complete=False,
            )
        if any(k in lowered for k in ("whatsapp", "twilio", "sms", "mensaje")):
            return Plan(
                reasoning="Consulta de mensajería: se usan las tools de Comms.",
                tasks=["Enviar mensaje"],
                is_complete=False,
            )
        if any(
            k in lowered
            for k in (
                "archivo",
                "sandbox",
                "directorio",
                "código",
                "codigo",
                "terminal",
                "script",
                "python",
            )
        ):
            return Plan(
                reasoning="Consulta de filesystem/código: se usa el sandbox.",
                tasks=["Inspeccionar sandbox"],
                is_complete=False,
            )
        return Plan(
            reasoning="Respuesta directa sin herramientas.",
            tasks=[],
            is_complete=True,
            final_answer="Listo. No se requieren herramientas para esta consulta.",
        )

    def _act(self, messages: list[BaseMessage]) -> AIMessage:
        if self.role != "executor":
            plan = self._plan(last_user_text(messages), messages)
            content = plan.final_answer or plan.reasoning
            return AIMessage(content=content)

        tool_names = {getattr(t, "name", "") for t in self.bound_tools}
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        if tool_msgs:
            return AIMessage(content=str(tool_msgs[-1].content))

        text = last_user_text(messages)
        expr_match = _MATH_RE.search(text)
        if expr_match and "calculate_expression" in tool_names:
            expr = _normalize_math(expr_match.group("expr"))
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "calculate_expression",
                        "args": {"expression": expr},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if any(k in text.lower() for k in ("hora", "fecha", "time")) and "get_current_time" in tool_names:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_current_time",
                        "args": {"timezone_name": "UTC"},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "list_directory" in tool_names and any(
            k in text.lower() for k in ("archivo", "sandbox", "directorio", "listar")
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_directory",
                        "args": {"path": "."},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "shopify_list_products" in tool_names and "producto" in text.lower():
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "shopify_list_products",
                        "args": {"first": 5},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "shopify_inventory_summary" in tool_names and "inventario" in text.lower():
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "shopify_inventory_summary",
                        "args": {},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "shopify_list_orders" in tool_names and any(k in text.lower() for k in ("pedido", "orden")):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "shopify_list_orders",
                        "args": {"first": 5},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "send_whatsapp_message" in tool_names and "whatsapp" in text.lower():
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "send_whatsapp_message",
                        "args": {"to": "+10000000000", "body": text},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "send_sms" in tool_names and "sms" in text.lower():
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "send_sms",
                        "args": {"to": "+10000000000", "body": text},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Sin herramientas aplicables. Tarea marcada para cierre.")


def get_planner_model() -> Any:
    settings = get_settings()
    if settings.offline:
        return OfflineChatModel(role="planner")
    if settings.planner_provider == "anthropic" and settings.anthropic_api_key:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.planner_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.planner_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )


def get_executor_model() -> BaseChatModel:
    settings = get_settings()
    if settings.offline:
        return OfflineChatModel(role="executor")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.executor_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )


def get_supervisor_model() -> Any:
    return get_planner_model()
