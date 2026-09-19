#!/usr/bin/env python3
"""Nodo local de J.A.R.V.I.S.: ejecuta en tu ordenador lo que el agente emite desde Render.

Patrón Command Emission. El backend no puede tocar tu disco, así que apila comandos
en `GET /api/commands/pending`; este script los sondea cada 2 s y los ejecuta:

    CREATE_PROJECT  crea carpetas/archivos bajo --root y abre Cursor
    OPEN_URL        abre http(s) en el navegador por defecto (`webbrowser`)
    RUN_TERMINAL    lanza el comando en segundo plano (`subprocess.Popen`)
    APP_CONTROL     abre o cierra una app (macOS `open -a`, Windows `start`/`taskkill`)

Después confirma el resultado con `POST /api/commands/{id}/ack`.

Uso:

    python local_node.py                       # URL de Render por defecto, raíz ~/JarvisProjects
    python local_node.py --url https://mi-jarvis.onrender.com --root ~/Proyectos
    JARVIS_NODE_TOKEN=... python local_node.py # si el backend exige token (recomendado)
    python local_node.py --once                # una sola pasada (para probar)

Solo usa la biblioteca estándar: no hay nada que instalar. Requiere Python 3.9+.
Seguridad: CREATE_PROJECT solo escribe dentro de --root; OPEN_URL solo http(s);
RUN_TERMINAL pide Y/N en esta terminal si el comando borra archivos o reinicia
(y se niega si no hay TTY); los destructivos del sistema entero se rechazan.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse

DEFAULT_URL = os.environ.get("JARVIS_URL", "https://j-a-r-v-i-s-yghr.onrender.com")
DEFAULT_ROOT = os.environ.get("JARVIS_PROJECTS_ROOT", str(Path.home() / "JarvisProjects"))
DEFAULT_INTERVAL = 2.0
TOKEN_HEADER = "X-Jarvis-Node-Token"
IDE_COMMANDS = {"cursor": ["cursor"], "code": ["code"]}
_CONFIRM_RE = re.compile(
    r"(?:^|[;&|]|&&|\|\|)\s*(rm|del|erase|rmdir|rd|remove-item|reboot|shutdown|halt)\b",
    re.IGNORECASE,
)
_CATASTROPHIC = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    ":(){",
    "fork bomb",
    "dd if=",
    "chmod 777 /",
    "> /dev/sd",
    "format c:",
    "format c ",
    "diskpart",
)
# Nombres conocidos → binario / bundle / imagen de Windows.
APP_ALIASES = {
    "spotify": {"darwin": "Spotify", "windows": "Spotify.exe", "linux": "spotify"},
    "whatsapp": {"darwin": "WhatsApp", "windows": "WhatsApp.exe", "linux": "whatsapp-desktop"},
    "terminal": {"darwin": "Terminal", "windows": "wt.exe", "linux": "x-terminal-emulator"},
    "chrome": {"darwin": "Google Chrome", "windows": "chrome.exe", "linux": "google-chrome"},
    "firefox": {"darwin": "Firefox", "windows": "firefox.exe", "linux": "firefox"},
    "safari": {"darwin": "Safari", "windows": "", "linux": ""},
    "notes": {"darwin": "Notes", "windows": "notepad.exe", "linux": "gedit"},
    "calculator": {"darwin": "Calculator", "windows": "calc.exe", "linux": "gnome-calculator"},
    "slack": {"darwin": "Slack", "windows": "slack.exe", "linux": "slack"},
    "discord": {"darwin": "Discord", "windows": "Discord.exe", "linux": "discord"},
}


class CommandError(Exception):
    """Un comando no se puede ejecutar tal cual llega (ruta insegura, acción desconocida…)."""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------
# Rutas seguras
# ---------------------------------------------------------------------------


def safe_relative(raw: str, *, what: str = "path") -> PurePosixPath:
    """Convierte lo que manda el backend en una ruta relativa sin escapes.

    Se acepta './taller-web', 'taller-web' o 'css/style.css'. Se rechazan las
    absolutas, las de unidad de Windows, las que suben con '..' y las vacías.
    """
    text = str(raw or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if not text or text in {".", "/"}:
        raise CommandError(f"{what} vacío")
    if text.startswith(("/", "~")) or (len(text) > 1 and text[1] == ":"):
        raise CommandError(f"{what} absoluto no permitido: {raw!r}")
    parts = [p for p in PurePosixPath(text).parts if p not in {"", "."}]
    if not parts or any(p == ".." for p in parts):
        raise CommandError(f"{what} fuera de la raíz: {raw!r}")
    return PurePosixPath(*parts)


def resolve_under(root: Path, relative: PurePosixPath) -> Path:
    target = (root / Path(*relative.parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise CommandError(f"ruta resuelta fuera de la raíz: {target}")
    return target


# ---------------------------------------------------------------------------
# Ejecución de comandos
# ---------------------------------------------------------------------------


def ide_command(open_with: str) -> list[str] | None:
    """Ejecutable del IDE si está en el PATH (en Windows, `cursor.cmd` también cuenta)."""
    base = IDE_COMMANDS.get((open_with or "").lower())
    if not base:
        return None
    found = shutil.which(base[0])
    if found is None and platform.system() == "Windows":
        found = shutil.which(base[0] + ".cmd")
    return [found] if found else None


def is_catastrophic(command: str) -> bool:
    lowered = (command or "").lower()
    return any(token in lowered for token in _CATASTROPHIC)


def needs_confirmation(command: str) -> bool:
    if is_catastrophic(command):
        return False
    return bool(_CONFIRM_RE.search(command or ""))


def confirm_dangerous(command: str, *, ask: Callable[[str], str] | None = None) -> bool:
    """Alerta en la terminal física y pide Y/N. Sin TTY, se niega (no se cuelga el sondeo)."""
    prompt = (
        f"\n⚠ COMANDO PELIGROSO (borra archivos o reinicia el sistema):\n  {command}\n"
        "¿Ejecutar de todas formas? [Y/N] "
    )
    if ask is not None:
        reply = ask(prompt)
    elif sys.stdin.isatty():
        log(prompt.rstrip())
        try:
            reply = input()
        except EOFError:
            return False
    else:
        log("✖ Comando peligroso y no hay terminal física para confirmar. Lo rechazo.")
        return False
    return str(reply or "").strip().lower() in {"y", "yes", "s", "si", "sí"}


def family(system: str | None = None) -> str:
    name = (system or platform.system()).lower()
    if name in {"darwin", "mac", "macos"}:
        return "darwin"
    if name.startswith("win"):
        return "windows"
    return "linux"


def resolve_app(name: str, system: str | None = None) -> str:
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    mapped = APP_ALIASES.get(key, {}).get(family(system))
    return mapped or name.strip()


def app_argv(name: str, action: str, *, system: str | None = None) -> list[str]:
    """Argumentos para abrir o cerrar una app, sin pasar por el shell."""
    kind = family(system)
    target = resolve_app(name, system)
    if not target:
        raise CommandError(f"no sé cómo {action} {name!r} en {kind}")
    if action == "close":
        if kind == "darwin":
            return ["osascript", "-e", f'quit app "{target}"']
        if kind == "windows":
            image = target if target.lower().endswith(".exe") else f"{target}.exe"
            return ["taskkill", "/IM", image]
        return ["pkill", "-x", target]
    if kind == "darwin":
        return ["open", "-a", target]
    if kind == "windows":
        return ["cmd", "/c", "start", "", target]
    found = shutil.which(target)
    return [found or target]


def open_url(command: dict[str, Any], root: Path, **hooks: Any) -> str:  # noqa: ARG001
    url = str(command.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CommandError(f"url debe ser http(s): {url!r}")
    browser = hooks.get("browser") or webbrowser.open
    ok = browser(url)
    if ok is False:
        raise CommandError(f"el navegador rechazó {url}")
    return f"navegador · {url}"


def run_terminal(command: dict[str, Any], root: Path, **hooks: Any) -> str:
    line = str(command.get("command") or "").strip()
    if not line:
        raise CommandError("command vacío")
    if is_catastrophic(line):
        raise CommandError("comando bloqueado por política de seguridad")
    if needs_confirmation(line) and not confirm_dangerous(line, ask=hooks.get("ask")):
        raise CommandError("el maestro rechazó el comando en la terminal")
    cwd_raw = str(command.get("cwd") or "").strip()
    cwd = resolve_under(root, safe_relative(cwd_raw, what="cwd")) if cwd_raw else root.resolve()
    if not cwd.exists():
        cwd.mkdir(parents=True, exist_ok=True)
    popen = hooks.get("popen") or subprocess.Popen
    proc = popen(
        line,
        shell=True,
        cwd=str(cwd),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = getattr(proc, "pid", "?")
    return f"terminal · pid {pid} · cwd {cwd} · {line}"


def app_control(command: dict[str, Any], root: Path, **hooks: Any) -> str:  # noqa: ARG001
    app = str(command.get("app") or "").strip()
    action = str(command.get("app_action") or "open").strip().lower()
    if action not in {"open", "close"}:
        raise CommandError(f"app_action inválida: {action}")
    if not app or re.search(r"[;|&$`<>\\/\n\r]", app):
        raise CommandError(f"nombre de aplicación no válido: {app!r}")
    argv = app_argv(app, action, system=hooks.get("system"))
    runner = hooks.get("opener") or subprocess.run
    runner(argv, check=False, timeout=30)
    return f"app · {action} {app} · {' '.join(argv)}"


def create_project(command: dict[str, Any], root: Path, **hooks: Any) -> str:
    """Crea carpetas y archivos bajo `root` y abre el IDE si se pide. Devuelve el resumen."""
    project_rel = safe_relative(command.get("path", ""), what="path")
    project_dir = resolve_under(root, project_rel)
    files = command.get("files") or []
    if not isinstance(files, list):
        raise CommandError("files debe ser una lista")

    project_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            raise CommandError("cada archivo debe ser un objeto {name, content}")
        rel = safe_relative(item.get("name", ""), what="name")
        target = resolve_under(project_dir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item.get("content", "")), encoding="utf-8", newline="\n")
        written.append(str(rel))

    open_with = str(command.get("open_with") or "none").lower()
    opened = "no"
    opener = hooks.get("opener") or subprocess.run
    if open_with != "none":
        exe = ide_command(open_with)
        if exe:
            try:
                opener([*exe, str(project_dir)], check=False, timeout=30)
                opened = open_with
            except (OSError, subprocess.SubprocessError) as exc:
                opened = f"error al abrir {open_with}: {exc}"
        else:
            opened = f"{open_with} no está en el PATH (en Cursor: Paleta → 'Shell Command: Install cursor command')"

    return f"{project_dir} · {len(written)} archivo(s): {', '.join(written) or '(ninguno)'} · IDE: {opened}"


HANDLERS: dict[str, Callable[..., str]] = {
    "CREATE_PROJECT": create_project,
    "OPEN_URL": open_url,
    "RUN_TERMINAL": run_terminal,
    "APP_CONTROL": app_control,
}


def execute(command: dict[str, Any], root: Path, **hooks: Any) -> str:
    action = str(command.get("action") or "").upper()
    handler = HANDLERS.get(action)
    if handler is None:
        raise CommandError(f"acción desconocida: {action or '(vacía)'}")
    return handler(command, root, **hooks)


# ---------------------------------------------------------------------------
# Cliente HTTP (urllib, sin dependencias)
# ---------------------------------------------------------------------------


class JarvisClient:
    def __init__(self, base_url: str, token: str = "", node: str = "", timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.node = node
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", f"jarvis-local-node/{platform.system().lower()}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header(TOKEN_HEADER, self.token)
        with urllib.request.urlopen(req, timeout=self.timeout) as res:
            raw = res.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def pending(self) -> list[dict[str, Any]]:
        query = f"?node={urllib.request.quote(self.node)}" if self.node else ""
        payload = self._request("GET", f"/api/commands/pending{query}")
        return list(payload.get("commands") or [])

    def ack(self, command_id: str, ok: bool, detail: str) -> None:
        self._request("POST", f"/api/commands/{command_id}/ack", {"ok": ok, "detail": detail[:4000], "node": self.node})


# ---------------------------------------------------------------------------
# Bucle principal
# ---------------------------------------------------------------------------


def process_pending(client: JarvisClient, root: Path, **hooks: Any) -> int:
    """Una pasada: recoge, ejecuta y confirma. Devuelve cuántos comandos procesó."""
    commands = client.pending()
    for command in commands:
        cid = str(command.get("id") or "?")
        action = str(command.get("action") or "?")
        label = command.get("description") or command.get("url") or command.get("command") or command.get("app") or command.get("path")
        log(f"⬇ {action} {cid}: {label}")
        try:
            detail = execute(command, root, **hooks)
            log(f"✔ {cid}: {detail}")
            ok = True
        except CommandError as exc:
            detail = f"rechazado: {exc}"
            log(f"✖ {cid}: {detail}")
            ok = False
        except Exception as exc:  # noqa: BLE001 - el nodo no debe morir por un comando
            detail = f"{type(exc).__name__}: {exc}"
            log(f"✖ {cid}: {detail}")
            ok = False
        try:
            client.ack(cid, ok, detail)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"⚠ no pude confirmar {cid}: {exc}")
    return len(commands)


def run(client: JarvisClient, root: Path, interval: float, *, once: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    log(f"Nodo local de J.A.R.V.I.S. · backend {client.base_url} · raíz {root} · cada {interval:g}s")
    if not client.token:
        log("⚠ Sin JARVIS_NODE_TOKEN: cualquiera con la URL del backend podría encolar archivos hacia este PC.")
    if not ide_command("cursor"):
        log("ℹ `cursor` no está en el PATH: los proyectos se crearán igual, pero no se abrirán solos.")
    backoff = interval
    while True:
        try:
            process_pending(client, root)
            backoff = interval
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                log("✖ El backend exige token (401). Arranca con JARVIS_NODE_TOKEN=… o --token.")
                if once:
                    raise SystemExit(2)
            else:
                log(f"⚠ HTTP {exc.code} al sondear: {exc.reason}")
            backoff = min(backoff * 2, 30)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"⚠ Sin conexión con el backend ({exc}); reintento en {backoff:g}s")
            backoff = min(backoff * 2, 30)
        if once:
            return
        time.sleep(backoff)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nodo local de J.A.R.V.I.S. (Command Emission).")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"URL del backend (por defecto {DEFAULT_URL} o $JARVIS_URL).")
    parser.add_argument("--root", default=DEFAULT_ROOT, help=f"Carpeta raíz donde se crean los proyectos (por defecto {DEFAULT_ROOT}).")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="Segundos entre sondeos (2 por defecto).")
    parser.add_argument("--token", default=os.environ.get("JARVIS_NODE_TOKEN", ""), help="Token compartido ($JARVIS_NODE_TOKEN).")
    parser.add_argument("--node", default=platform.node() or "local-node", help="Nombre con el que este PC se identifica.")
    parser.add_argument("--once", action="store_true", help="Una sola pasada y salir.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = JarvisClient(args.url, token=args.token, node=args.node)
    try:
        run(client, Path(args.root).expanduser(), args.interval, once=args.once)
    except KeyboardInterrupt:
        log("Nodo detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
