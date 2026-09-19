"""Fábricas de LLMs (planificador vs ejecutor) y modelos offline para tests/demo."""

from __future__ import annotations

import re
import uuid
from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, Field, ValidationError

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


StructuredT = TypeVar("StructuredT", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """El proveedor no devolvió una instancia válida del esquema pedido."""


def invoke_structured(model: Any, schema: type[StructuredT], messages: Any) -> StructuredT:
    """`with_structured_output(schema).invoke(...)` que siempre devuelve el esquema o falla claro.

    Los nodos del grafo consumen `RouteDecision` y `Plan` como objetos y los reducen
    a strings y dicts antes de tocar el estado, así que aquí es donde hay que
    absorber lo que el proveedor pueda devolver en su lugar: un dict (proveedores
    sin parseo nativo o `include_raw`), `None` (rechazo o JSON truncado) o una
    excepción de red/validación. Todo eso acaba en `StructuredOutputError`, que
    el llamador convierte en una degradación controlada en vez de tumbar el turno.
    """
    try:
        raw = model.with_structured_output(schema).invoke(messages)
    except Exception as exc:
        raise StructuredOutputError(f"{schema.__name__}: {type(exc).__name__}: {exc}") from exc
    if isinstance(raw, schema):
        return raw
    if isinstance(raw, dict):
        payload = raw.get("parsed", raw) if "parsed" in raw else raw
        if isinstance(payload, schema):
            return payload
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise StructuredOutputError(f"{schema.__name__}: {exc}") from exc
    raise StructuredOutputError(
        f"{schema.__name__}: el modelo devolvió {type(raw).__name__} en vez del esquema"
    )


_MATH_RE = re.compile(
    r"(?P<expr>\d+(?:\.\d+)?(?:\s*[\+\-\*/x×]\s*\d+(?:\.\d+)?)+)",
    re.IGNORECASE,
)

_GIT_RE = re.compile(r"\bgit\b")

_PROJECT_NOUNS = (
    "proyecto",
    "una web",
    "página web",
    "pagina web",
    "sitio web",
    "landing",
    "html",
    "cursor",
    "mi ordenador",
    "mi pc",
    "mi equipo",
    "en el ide",
)
_PROJECT_VERBS = ("crea", "crear", "genera", "generar", "haz", "hazme", "monta", "construye", "prepara", "abre", "ábre")


def _is_project_request(text: str) -> bool:
    """«Crea una web HTML para un taller» → proyecto para el ordenador del usuario."""
    lowered = (text or "").lower()
    return any(n in lowered for n in _PROJECT_NOUNS) and any(v in lowered for v in _PROJECT_VERBS)


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_KNOWN_SITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
}
_KNOWN_APPS = (
    "spotify",
    "whatsapp",
    "chrome",
    "firefox",
    "safari",
    "terminal",
    "notes",
    "calculator",
    "slack",
    "discord",
)


def _system_control_args(text: str) -> dict[str, str] | None:
    """Argumentos de system_commander para abrir URL, app o terminal en el PC del usuario."""
    raw = text or ""
    lowered = raw.lower()
    urls = _URL_RE.findall(raw)
    if urls:
        return {"action": "OPEN_URL", "url": urls[0].rstrip(".,);]}"), "brief": raw}
    for name, url in _KNOWN_SITES.items():
        if re.search(rf"\b(?:abre|abrir|ábreme|abreme)\b.*\b{name}\b", lowered):
            return {"action": "OPEN_URL", "url": url, "brief": raw}
    for app in _KNOWN_APPS:
        if not re.search(rf"\b{app}\b", lowered):
            continue
        if any(k in lowered for k in ("cierra", "cerrar", "quita", "cierra")):
            return {"action": "APP_CONTROL", "app": app, "app_action": "close", "brief": raw}
        if any(k in lowered for k in ("abre", "abrir", "ábre", "lanza", "abre")):
            return {"action": "APP_CONTROL", "app": app, "app_action": "open", "brief": raw}
    npm = re.search(r"(npm\s+run\s+\S+)", raw, re.IGNORECASE)
    if npm and any(k in lowered for k in ("arranca", "ejecuta", "corre", "lanza", "terminal", "servidor")):
        return {"action": "RUN_TERMINAL", "command": npm.group(1), "brief": raw}
    lead = re.search(
        r"(?:arranca|ejecuta|corre|lanza)\s+(?:en\s+mi\s+(?:pc|ordenador|terminal)\s+)?(.+)$",
        raw,
        re.IGNORECASE,
    )
    if lead and any(k in lowered for k in ("en mi pc", "en mi ordenador", "en mi terminal", "en la terminal")):
        return {"action": "RUN_TERMINAL", "command": lead.group(1).strip().rstrip("."), "brief": raw}
    return None


def _is_system_control_request(text: str) -> bool:
    return _system_control_args(text) is not None

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
        if _is_system_control_request(query) or _is_project_request(query):
            if "code_agent" in visited:
                return RouteDecision(next_agent="FINISH", rationale="La orden ya se emitió al nodo local.")
            return RouteDecision(
                next_agent="code_agent",
                rationale="Control del ordenador del usuario."
                if _is_system_control_request(query)
                else "Proyecto para el ordenador del usuario.",
            )
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
            if getattr(tool_msgs[-1], "name", "") == "system_commander":
                last = last.splitlines()[0]
            return Plan(
                reasoning="Herramienta ejecutada. Se entrega el resultado.",
                tasks=[],
                is_complete=True,
                final_answer=last,
            )
        draft = next(
            (
                msg
                for msg in reversed(since_last_human(messages))
                if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None)
            ),
            None,
        )
        if draft is not None:
            # El ejecutor ya redactó sin pedir herramientas: cerrar en vez de repetir
            # la vuelta planificador ↔ ejecutor hasta agotar el presupuesto.
            return Plan(
                reasoning="El ejecutor respondió sin herramientas.",
                tasks=[],
                is_complete=True,
                final_answer=str(draft.content),
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
        if _is_system_control_request(text):
            return Plan(
                reasoning="Control del ordenador del usuario: se emite con system_commander.",
                tasks=["Emitir la orden al nodo local"],
                is_complete=False,
            )
        if _is_project_request(text):
            return Plan(
                reasoning="Proyecto para el ordenador del usuario: se emite con system_commander.",
                tasks=["Emitir el proyecto al nodo local"],
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
            content = str(tool_msgs[-1].content)
            # Las tools que hablan ponen la frase en la primera línea y el JSON debajo.
            if getattr(tool_msgs[-1], "name", "") == "system_commander":
                content = content.splitlines()[0]
            return AIMessage(content=content)

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
        if "system_commander" in tool_names and (_is_system_control_request(text) or _is_project_request(text)):
            args = _system_control_args(text) or {"brief": text, "open_with": "cursor"}
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "system_commander",
                        "args": args,
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


def heuristic_route(text: str) -> RouteDecision:
    """Enrutado por palabras clave, sin LLM: respaldo cuando la decisión estructurada no llega."""
    return OfflineChatModel(role="planner")._route(text)


def get_planner_model() -> Any:
    """Planificador y Supervisor: solo producen `Plan` y `RouteDecision`, nunca voz.

    `disable_streaming=True` es deliberado. Dentro de `astream_events` LangChain
    pasa a stream cualquier `invoke`, y con salida estructurada eso obliga al
    proveedor a reensamblar el JSON por trozos: `langchain-openai` vuelca cada
    chunk con `model_dump()` sobre un tipo cuyo campo `parsed` no está resuelto
    (de ahí el `PydanticSerializationUnexpectedValue ... field_name='parsed'`),
    y un corte a medias deja un `Plan` o `RouteDecision` imposible de parsear.
    Sin streaming va en una sola petición con parseo nativo, sin aviso ni
    trozos, y `astream_jarvis` no pierde nada: esos tokens nunca se pronuncian.
    """
    settings = get_settings()
    if settings.offline:
        return OfflineChatModel(role="planner")
    if settings.planner_provider == "anthropic" and settings.anthropic_api_key:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.planner_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
            disable_streaming=True,
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.planner_model,
        api_key=settings.openai_api_key,
        temperature=0,
        disable_streaming=True,
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
        # El ejecutor es el único que redacta para el usuario. Con `streaming=True`
        # su `invoke` va emitiendo tokens, que `astream_jarvis` reexpide a Vapi para
        # que la voz empiece a hablar sin esperar el mensaje completo.
        streaming=True,
    )


def get_supervisor_model() -> Any:
    return get_planner_model()
