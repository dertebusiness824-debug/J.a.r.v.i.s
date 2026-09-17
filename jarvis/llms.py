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
            "research_agent, general, o FINISH si la tarea ya está resuelta."
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

_GIT_RE = re.compile(r"\bgit\b")

_RESEARCH_PHRASES = (
    "recopilar información",
    "recopila información",
    "información pública",
    "informacion publica",
    "investigar a",
    "investiga a",
    "investigar sobre",
    "investiga sobre",
    "buscar en google",
    "busca en google",
    "buscar en internet",
    "busca en internet",
    "perfiles sociales",
    "perfil de linkedin",
    "emails públicos",
    "emails publicos",
    "correo público",
    "correo publico",
    "correos públicos",
    "correos publicos",
    "correo corporativo",
    "correos de",
    "nombre de usuario",
    "due diligence",
)

_RESEARCH_TOKENS = (
    "osint",
    "linkedin",
    "hunter.io",
    "hunter io",
    "hunterio",
    "username",
    "github",
    "instagram",
    "dork",
)


def _is_research_query(text: str) -> bool:
    lowered = text.lower()
    if any(phrase in lowered for phrase in _RESEARCH_PHRASES):
        return True
    if any(token in lowered for token in _RESEARCH_TOKENS):
        return True
    if "google" in lowered and any(k in lowered for k in ("buscar", "busca", "investiga", "investigar")):
        return True
    if any(k in lowered for k in ("twitter", "x.com")) and any(
        k in lowered for k in ("perfil", "investiga", "investigar", "osint")
    ):
        return True
    return False


_USERNAME_STOPWORDS = frozenset(
    {
        "alias",
        "comprueba",
        "de",
        "el",
        "en",
        "github",
        "instagram",
        "medium",
        "nombre",
        "para",
        "usuario",
        "username",
    }
)


def _guess_username(text: str) -> str:
    """Alias más probable en modo offline: un @handle, o la última palabra útil."""
    tokens = [token.strip(".,;:¿?!()\"'") for token in text.split()]
    for token in tokens:
        if token.startswith("@") and len(token) > 1:
            return token[1:]
    for token in reversed(tokens):
        if token and token.lower() not in _USERNAME_STOPWORDS:
            return token
    return tokens[-1] if tokens else ""


def last_user_text(messages: list[BaseMessage] | list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
        if isinstance(msg, dict) and msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def since_last_human(messages: list[Any]) -> list[Any]:
    last_human = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage) or (isinstance(msg, dict) and msg.get("role") == "user"):
            last_human = i
    if last_human < 0:
        return list(messages)
    return list(messages[last_human + 1 :])


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
        query = text
        visited: set[str] = set()
        for line in text.splitlines():
            lowered_line = line.lower().strip()
            if lowered_line.startswith("consulta:"):
                query = line.split(":", 1)[1].strip()
            elif lowered_line.startswith("visitados:"):
                raw = line.split(":", 1)[1].strip().lower()
                if raw not in {"ninguno", "none", ""}:
                    visited = {part.strip() for part in raw.split(",") if part.strip()}

        lowered = query.lower()
        if (
            any(
                k in lowered
                for k in (
                    "código",
                    "codigo",
                    "archivo",
                    "terminal",
                    "python",
                    "script",
                    "sandbox",
                    "refactor",
                    "leer fichero",
                    "write file",
                    "run_terminal",
                )
            )
            or _GIT_RE.search(lowered)
        ) and "code_agent" not in visited:
            return RouteDecision(next_agent="code_agent", rationale="Tarea de código / filesystem.")
        if any(
            k in lowered
            for k in (
                "whatsapp",
                "zadarma",
                "sms",
                "mensaje",
                "webhook",
                "comunicación",
                "comunicacion",
                "centralita",
                "pbx",
                "llamada",
            )
        ) and "comms_agent" not in visited:
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
        ) and "shop_agent" not in visited:
            return RouteDecision(next_agent="shop_agent", rationale="Tarea de e-commerce.")
        if _is_research_query(query) and "research_agent" not in visited:
            return RouteDecision(next_agent="research_agent", rationale="Tarea de OSINT / información pública.")
        if visited:
            return RouteDecision(next_agent="FINISH", rationale="Especialistas necesarios ya reportaron.")
        return RouteDecision(next_agent="general", rationale="Consulta general / herramientas core.")

    def _plan(self, text: str, raw: Any) -> Plan:
        messages = raw if isinstance(raw, list) else []
        tool_msgs = [m for m in since_last_human(messages) if isinstance(m, ToolMessage)]
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
        if any(
            k in lowered
            for k in ("whatsapp", "zadarma", "sms", "mensaje", "centralita", "pbx", "llamada")
        ):
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
        if _is_research_query(text):
            return Plan(
                reasoning="Consulta OSINT: se usan las tools de investigación pública.",
                tasks=["Buscar información pública"],
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
        tool_msgs = [m for m in since_last_human(messages) if isinstance(m, ToolMessage)]
        if tool_msgs:
            return AIMessage(content=str(tool_msgs[-1].content))

        text = last_user_text(messages)
        lowered = text.lower()
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
        if any(k in lowered for k in ("hora", "fecha", "time")) and "get_current_time" in tool_names:
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
        if "write_file" in tool_names and any(
            k in lowered for k in ("crea", "escribe", "guardar", "write", "crear")
        ):
            match = re.search(r"([\w./-]+\.(?:py|txt|md|json|js|ts|csv))", text)
            path = match.group(1) if match else "nota.txt"
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": path, "content": text},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "list_directory" in tool_names and any(
            k in lowered for k in ("archivo", "sandbox", "directorio", "listar")
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
        if "send_zadarma_sms" in tool_names and any(
            k in text.lower() for k in ("sms", "zadarma", "centralita")
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "send_zadarma_sms",
                        "args": {"to": "+10000000000", "body": text},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "username_lookup" in tool_names and any(
            k in lowered for k in ("github", "instagram", "medium", "usuario", "username", "alias")
        ):
            handle = _guess_username(text)
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "username_lookup",
                        "args": {"username": handle},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        contact_keywords = (
            "email",
            "correo",
            "hunter",
            "@",
            "mail",
            "teléfono",
            "telefono",
            "contacto",
        )
        domain_match = re.search(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", lowered)
        if (
            "find_public_emails" in tool_names
            and domain_match
            and any(k in lowered for k in contact_keywords)
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "find_public_emails",
                        "args": {"domain": domain_match.group(0)},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "find_contact_info" in tool_names and any(k in lowered for k in contact_keywords):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "find_contact_info",
                        "args": {"domain_or_company": text, "person_name": None},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "advanced_dork_search" in tool_names and any(
            k in lowered for k in ("site:", "linkedin", "twitter", "dork", "intitle:", "intext:")
        ):
            site = None
            if "linkedin" in lowered:
                site = "linkedin.com/in"
            elif "twitter" in lowered or "x.com" in lowered:
                site = "twitter.com"
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "advanced_dork_search",
                        "args": {"query": text, "site": site},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "extract_social_profiles" in tool_names and any(
            k in lowered for k in ("linkedin", "twitter", "perfil", "social", "x.com")
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "extract_social_profiles",
                        "args": {"name": text},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        if "web_search" in tool_names:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": text},
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Sin herramientas aplicables. Tarea marcada para cierre.")


def _groq_chat_model(model: str) -> BaseChatModel:
    """Groq para voz: streaming activo, así Vapi recibe fragmentos sin esperar el final."""
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=model,
        api_key=get_settings().groq_api_key,
        temperature=0,
        streaming=True,
    )


def get_planner_model() -> Any:
    settings = get_settings()
    if settings.offline:
        return OfflineChatModel(role="planner")
    provider = settings.llm_provider
    if provider == "groq":
        return _groq_chat_model(settings.groq_planner_model)
    if provider == "anthropic":
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
    if settings.llm_provider == "groq":
        return _groq_chat_model(settings.groq_executor_model)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.executor_model,
        api_key=settings.openai_api_key,
        temperature=0,
        streaming=True,
    )


def get_supervisor_model() -> Any:
    return get_planner_model()
