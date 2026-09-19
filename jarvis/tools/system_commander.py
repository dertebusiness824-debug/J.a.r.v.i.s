"""SystemCommanderTool: el agente pide acciones en el ordenador del usuario.

El backend corre en Render y no debe (ni puede) tocar el disco ni las apps del
usuario. Esta herramienta no ejecuta nada: valida, estructura un comando
estandarizado y lo deja en la cola de `jarvis.commands`. `local_node.py` lo
recoge por `GET /api/commands/pending` y lo ejecuta en el PC.

Acciones:

    CREATE_PROJECT  {path, files, open_with}
    OPEN_URL        {url}
    RUN_TERMINAL    {command, cwd?}
    APP_CONTROL     {app, app_action: open|close}

Para CREATE_PROJECT el LLM puede llegar con los archivos ya redactados o solo
con la instrucción (`brief`); sin credenciales se monta un andamio mínimo.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from jarvis.commands import MAX_FILES, ProjectFile, SystemCommand, enqueue_command, needs_confirmation
from jarvis.config import get_settings

logger = logging.getLogger(__name__)

TOOL_NAME = "system_commander"

_AUTHOR_PROMPT = """Eres el arquitecto de proyectos de J.A.R.V.I.S.
Te dan una instrucción de creación de código y devuelves el proyecto completo:
una carpeta (path, relativa, en kebab-case, sin espacios) y la lista de archivos
con su contenido íntegro y funcional. Sin marcadores de posición ni "...":
cada archivo debe poder abrirse y funcionar tal cual. HTML/CSS/JS en español si
la instrucción lo está. Sé sobrio: los archivos justos, sin dependencias externas
salvo que se pidan. Incluye un README.md breve.
"""


class ProjectFileInput(BaseModel):
    name: str = Field(description="Ruta relativa dentro del proyecto, p.ej. 'index.html' o 'css/style.css'.")
    content: str = Field(description="Contenido completo del archivo, listo para guardar.")


class SystemCommanderInput(BaseModel):
    action: str = Field(
        default="CREATE_PROJECT",
        description="CREATE_PROJECT (archivos + Cursor), OPEN_URL (navegador), RUN_TERMINAL (comando en el PC) o APP_CONTROL (abrir/cerrar una app).",
    )
    brief: str = Field(
        default="",
        description=(
            "Instrucción en lenguaje natural. En CREATE_PROJECT es el encargo si no se pasan files; "
            "en el resto, una frase para el log del nodo."
        ),
    )
    path: str = Field(
        default="",
        description="Carpeta del proyecto, relativa, p.ej. './taller-web'. Solo CREATE_PROJECT.",
    )
    files: list[ProjectFileInput] = Field(
        default_factory=list,
        description=f"Archivos completos del proyecto (máx. {MAX_FILES}). Si se omiten, se redactan a partir del brief.",
    )
    open_with: str = Field(
        default="cursor",
        description="IDE al crear el proyecto: 'cursor' (por defecto), 'code' o 'none'.",
    )
    url: str = Field(default="", description="URL http(s) para OPEN_URL, p.ej. 'https://github.com'.")
    command: str = Field(default="", description="Comando de terminal para RUN_TERMINAL, p.ej. 'npm run dev'.")
    cwd: str = Field(default="", description="Directorio relativo a la raíz del nodo para RUN_TERMINAL.")
    app: str = Field(default="", description="Nombre de la aplicación para APP_CONTROL, p.ej. 'Spotify'.")
    app_action: str = Field(default="open", description="Para APP_CONTROL: 'open' o 'close'.")


class ProjectSpec(BaseModel):
    """Lo que devuelve el modelo cuando redacta el proyecto a partir del brief."""

    path: str = Field(description="Carpeta del proyecto, relativa y en kebab-case, p.ej. 'taller-web'.")
    description: str = Field(default="", description="Qué es el proyecto, en una frase.")
    files: list[ProjectFileInput] = Field(default_factory=list)


_STOP_WORDS = {
    "crea", "crear", "creame", "hazme", "haz", "genera", "generar", "monta", "construye", "prepara",
    "quiero", "necesito", "una", "un", "el", "la", "los", "las", "de", "del", "por", "favor", "me",
    "que", "y", "con", "para", "en", "mi", "ordenador", "pc", "equipo", "cursor", "ide",
    "abre", "abrelo", "abrela", "abrelos", "abrelas", "luego", "despues",
}
_TAIL_RE = re.compile(
    r"(?:,?\s+(?:y|e)\s+(?:ábre|abre|luego|después|despues)\w*.*$)"
    r"|(?:,?\s+en\s+(?:mi|el|la)\s+(?:ordenador|pc|equipo|ide|m[áa]quina|cursor)\s*[.!]?$)"
    r"|(?:,?\s+en\s+cursor\s*[.!]?$)",
    re.IGNORECASE,
)
_LEAD_RE = re.compile(r"^(?:crea(?:me)?|hazme|haz|genera|monta|construye|prepara|quiero|necesito)\s+(?:una?\s+|el\s+|la\s+)?", re.IGNORECASE)


def core_brief(text: str) -> str:
    """Quita la orden y la coletilla: 'Crea una web HTML para un taller y ábrela en Cursor' → 'web HTML para un taller'."""
    cleaned = _TAIL_RE.sub("", (text or "").strip().rstrip(".!"))
    cleaned = _LEAD_RE.sub("", cleaned).strip()
    return cleaned or (text or "").strip()


def title_from_brief(text: str) -> str:
    core = core_brief(text)
    return (core[:1].upper() + core[1:]) if core else "Proyecto"


def slugify(text: str, *, fallback: str = "proyecto-jarvis") -> str:
    """'Crea una web HTML para un Taller y ábrela en Cursor' → 'web-html-taller'."""
    normalized = unicodedata.normalize("NFKD", core_brief(text)).encode("ascii", "ignore").decode()
    words = [w for w in re.sub(r"[^a-zA-Z0-9]+", " ", normalized).lower().split() if w]
    kept = [w for w in words if w not in _STOP_WORDS][:6]
    slug = "-".join(kept)
    return slug or fallback


def _scaffold(brief: str, path: str) -> ProjectSpec:
    """Andamio mínimo para modo offline: el circuito entero funciona sin claves."""
    title = title_from_brief(brief)
    html = f"""<!DOCTYPE html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <link rel="stylesheet" href="css/style.css" />
  </head>
  <body>
    <main>
      <h1>{title}</h1>
      <p>Proyecto generado por J.A.R.V.I.S. Edita este archivo para empezar.</p>
    </main>
    <script src="js/app.js"></script>
  </body>
</html>
"""
    css = """:root { color-scheme: dark; font-family: system-ui, sans-serif; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0f172a; color: #e2e8f0; }
main { max-width: 42rem; padding: 2rem; text-align: center; }
h1 { letter-spacing: 0.08em; text-transform: uppercase; color: #67e8f9; }
"""
    js = 'document.addEventListener("DOMContentLoaded", () => {\n  console.log("Proyecto listo, maestro.");\n});\n'
    readme = f"# {title}\n\nProyecto creado por J.A.R.V.I.S. (modo offline, andamio mínimo).\n\n- `index.html`: página principal\n- `css/style.css`: estilos\n- `js/app.js`: lógica\n"
    return ProjectSpec(
        path=path,
        description=title,
        files=[
            ProjectFileInput(name="index.html", content=html),
            ProjectFileInput(name="css/style.css", content=css),
            ProjectFileInput(name="js/app.js", content=js),
            ProjectFileInput(name="README.md", content=readme),
        ],
    )


def author_project(brief: str, path_hint: str = "") -> ProjectSpec:
    """Redacta el proyecto a partir del brief con el modelo ejecutor (o el andamio offline)."""
    path = path_hint.strip() or slugify(brief)
    settings = get_settings()
    if settings.offline:
        return _scaffold(brief, path)
    from jarvis.llms import StructuredOutputError, get_executor_model, invoke_structured

    try:
        spec = invoke_structured(
            get_executor_model(),
            ProjectSpec,
            [
                SystemMessage(content=_AUTHOR_PROMPT),
                HumanMessage(content=f"Instrucción: {brief}\nCarpeta sugerida: {path}"),
            ],
        )
    except StructuredOutputError as exc:
        logger.warning("system_commander: el modelo no redactó el proyecto (%s); uso el andamio", exc)
        return _scaffold(brief, path)
    if not spec.files:
        return _scaffold(brief, path)
    if path_hint.strip():
        spec.path = path_hint.strip()
    return spec


def _normalize_open_with(value: str) -> str:
    lowered = (value or "cursor").strip().lower()
    if lowered in {"", "cursor", "cursor.exe"}:
        return "cursor"
    if lowered in {"code", "vscode", "vs code", "visual studio code"}:
        return "code"
    if lowered in {"none", "no", "ninguno", "nada", "false"}:
        return "none"
    return "cursor"


def _normalize_action(value: str) -> str:
    raw = (value or "CREATE_PROJECT").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "CREATE": "CREATE_PROJECT",
        "PROJECT": "CREATE_PROJECT",
        "OPENURL": "OPEN_URL",
        "OPEN": "OPEN_URL",
        "URL": "OPEN_URL",
        "RUN": "RUN_TERMINAL",
        "TERMINAL": "RUN_TERMINAL",
        "SHELL": "RUN_TERMINAL",
        "APP": "APP_CONTROL",
        "APPLICATION": "APP_CONTROL",
    }
    return aliases.get(raw, raw)


def _normalize_app_action(value: str) -> str:
    lowered = (value or "open").strip().lower()
    if lowered in {"close", "cerrar", "quita", "kill", "quit", "stop"}:
        return "close"
    return "open"


def _project_command(*, brief: str, path: str, files: list[Any] | None, open_with: str) -> SystemCommand:
    items = [f if isinstance(f, ProjectFileInput) else ProjectFileInput(**dict(f)) for f in (files or [])]
    description = brief.strip()
    if not items:
        if not brief.strip():
            raise ValueError("Falta el brief o la lista de archivos del proyecto.")
        spec = author_project(brief, path)
        items = spec.files
        path = spec.path
        description = spec.description or description
    if not path.strip():
        path = slugify(brief)
    return SystemCommand(
        action="CREATE_PROJECT",
        path=path,
        files=[ProjectFile(name=f.name, content=f.content) for f in items],
        open_with=_normalize_open_with(open_with),  # type: ignore[arg-type]
        description=description[:200],
    )


def build_command(
    *,
    action: str = "CREATE_PROJECT",
    brief: str = "",
    path: str = "",
    files: list[Any] | None = None,
    open_with: str = "cursor",
    url: str = "",
    command: str = "",
    cwd: str = "",
    app: str = "",
    app_action: str = "open",
) -> SystemCommand:
    """Convierte los argumentos del LLM en el comando validado (sin encolarlo)."""
    kind = _normalize_action(action)
    description = brief.strip()[:200]
    if kind == "CREATE_PROJECT":
        return _project_command(brief=brief, path=path, files=files, open_with=open_with)
    if kind == "OPEN_URL":
        return SystemCommand(action="OPEN_URL", url=url or brief, description=description or url)
    if kind == "RUN_TERMINAL":
        return SystemCommand(
            action="RUN_TERMINAL",
            command=command or brief,
            cwd=cwd,
            description=description or command,
        )
    if kind == "APP_CONTROL":
        return SystemCommand(
            action="APP_CONTROL",
            app=app or brief,
            app_action=_normalize_app_action(app_action),  # type: ignore[arg-type]
            description=description or app,
        )
    raise ValueError(f"acción desconocida: {action}")


def _spoken(command: SystemCommand) -> str:
    if command.action == "CREATE_PROJECT":
        n = len(command.files)
        ide = {"cursor": "Cursor", "code": "VS Code"}.get(command.open_with)
        return (
            f"Proyecto {command.path} con {n} archivo{'s' if n != 1 else ''} enviado a su equipo, maestro"
            + (f"; se abrirá en {ide} en cuanto el nodo local lo recoja." if ide else ".")
        )
    if command.action == "OPEN_URL":
        return f"Abriendo {command.url} en su navegador, maestro."
    if command.action == "RUN_TERMINAL":
        extra = " El nodo pedirá confirmación Y/N si el comando borra archivos o reinicia." if needs_confirmation(command.command) else ""
        return f"Comando enviado a su terminal, maestro.{extra}"
    verb = "Cerrando" if command.app_action == "close" else "Abriendo"
    return f"{verb} {command.app} en su equipo, maestro."


@tool(TOOL_NAME, args_schema=SystemCommanderInput)
def system_commander(
    action: str = "CREATE_PROJECT",
    brief: str = "",
    path: str = "",
    files: list[ProjectFileInput] | None = None,
    open_with: str = "cursor",
    url: str = "",
    command: str = "",
    cwd: str = "",
    app: str = "",
    app_action: str = "open",
) -> str:
    """Controla el ORDENADOR DEL USUARIO: crear un proyecto, abrir una URL, lanzar un
    comando de terminal o abrir/cerrar una aplicación (Spotify, WhatsApp, Terminal).

    No ejecuta nada en el servidor: emite un comando (CREATE_PROJECT, OPEN_URL,
    RUN_TERMINAL, APP_CONTROL) que el nodo local (local_node.py) recoge y ejecuta.
    Úsalo cuando pidan algo "en mi ordenador", "en Cursor", "ábrelo", "abre Spotify"
    o "arranca npm run dev". Los comandos destructivos (rm, del, reboot) los
    confirmará el usuario en la terminal del nodo.
    """
    try:
        built = build_command(
            action=action,
            brief=brief,
            path=path,
            files=files,
            open_with=open_with,
            url=url,
            command=command,
            cwd=cwd,
            app=app,
            app_action=app_action,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    record = enqueue_command(built, origin="code_agent")
    settings = get_settings()
    summary = {
        "queued": True,
        "id": record.id,
        "action": built.action,
        "path": built.path,
        "files": [f.name for f in built.files],
        "open_with": built.open_with,
        "url": built.url,
        "command": built.command,
        "cwd": built.cwd,
        "app": built.app,
        "app_action": built.app_action,
        "pending_endpoint": f"{settings.jarvis_public_url.rstrip('/')}/api/commands/pending",
        "note": "El nodo local (local_node.py) ejecutará esto en el PC del usuario.",
    }
    logger.info("📦 [SYSTEM COMMANDER] %s %s encolado", record.id, built.action)
    return _spoken(built) + "\n" + json.dumps(summary, ensure_ascii=False)
