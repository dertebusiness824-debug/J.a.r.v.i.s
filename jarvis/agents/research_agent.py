"""Agente Investigador (OSINT): búsqueda profunda, contactos públicos y aliases.

Mismo flujo interno que el resto de especialistas: el nodo invoca el subgrafo
Planificador → Ejecutor → Tools y reporta al Supervisor con `delegation_log`.
El `Command(goto="supervisor")` de `make_specialist_node` cierra la arista de vuelta.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import quote_plus

import requests
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from jarvis.agents.base import make_specialist_node
from jarvis.config import get_settings
from jarvis.hud_live import mark_researching
from jarvis.prompts import RESEARCH_PROMPT

NAME = "research_agent"
PROMPT = RESEARCH_PROMPT
USER_AGENT = "JarvisOSINT/2.0 (public-source research)"
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_EMAIL_FINDER_URL = "https://api.hunter.io/v2/email-finder"
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
USERNAME_TARGETS = (
    ("github", "https://github.com/{username}"),
    ("instagram", "https://instagram.com/{username}"),
    ("medium", "https://medium.com/@{username}"),
)


class WebSearchInput(BaseModel):
    query: str = Field(description="Consulta de búsqueda web (persona, empresa o tema).")


class AdvancedDorkSearchInput(BaseModel):
    query: str = Field(description="Consulta libre. Puede incluir operadores (intext:, intitle:, OR).")
    site: str | None = Field(
        default=None,
        description="Si se indica (ej. linkedin.com/in o twitter.com), se antepone site:{sitio}.",
    )


class ExtractSocialProfilesInput(BaseModel):
    name: str = Field(description="Nombre de la persona o empresa a localizar en redes.")


class FindPublicEmailsInput(BaseModel):
    domain: str = Field(
        description="Dominio corporativo (ej. acme.com) para el Domain Search de Hunter.io."
    )


class FindContactInfoInput(BaseModel):
    domain_or_company: str = Field(description="Dominio (acme.com) o nombre de empresa.")
    person_name: str | None = Field(
        default=None,
        description="Nombre de la persona para cruzar con el dominio/empresa.",
    )


class UsernameLookupInput(BaseModel):
    username: str = Field(description="Alias exacto sin @ ni barras (github/instagram/medium).")


def _looks_like_domain(value: str) -> bool:
    text = (value or "").strip()
    return "." in text and " " not in text and "@" not in text


def _hunter_api_key() -> str | None:
    """HUNTERIO_API_KEY del entorno; HUNTER_API_KEY y el .env valen como alias."""
    settings = get_settings()
    candidates = (
        os.getenv("HUNTERIO_API_KEY"),
        os.getenv("HUNTER_API_KEY"),
        settings.hunterio_api_key,
        settings.hunter_api_key,
    )
    for candidate in candidates:
        key = (candidate or "").strip()
        if key:
            return key
    return None


def _compose_dork(query: str, site: str | None = None) -> str:
    cleaned = " ".join((query or "").split())
    if not site:
        return cleaned
    host = str(site).strip()
    if host.lower().startswith("site:"):
        host = host[5:].strip()
    host = host.lstrip("/").rstrip("/")
    if not host:
        return cleaned
    return f"site:{host} {cleaned}".strip()


def _tavily_search(query: str) -> list[dict[str, Any]] | None:
    settings = get_settings()
    if not settings.tavily_api_key:
        return None
    try:
        from langchain_community.tools.tavily_search import TavilySearchResults
    except ImportError:
        return None
    os.environ.setdefault("TAVILY_API_KEY", settings.tavily_api_key)
    try:
        search = TavilySearchResults(max_results=8)
        raw = search.invoke({"query": query})
    except Exception:
        try:
            search = TavilySearchResults(max_results=8)
            raw = search.invoke(query)
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [{"title": query, "snippet": raw, "url": None}]
    if isinstance(raw, list):
        results: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                results.append(
                    {
                        "title": item.get("title") or query,
                        "snippet": item.get("content") or item.get("snippet") or "",
                        "url": item.get("url"),
                    }
                )
            else:
                results.append({"title": query, "snippet": str(item), "url": None})
        return results
    if isinstance(raw, dict):
        return [
            {
                "title": raw.get("title") or query,
                "snippet": raw.get("content") or raw.get("snippet") or json.dumps(raw),
                "url": raw.get("url"),
            }
        ]
    return None


def _duckduckgo_search(query: str) -> list[dict[str, Any]]:
    response = requests.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    data = response.json()
    results: list[dict[str, Any]] = []
    abstract = data.get("AbstractText") or ""
    if abstract:
        results.append(
            {
                "title": data.get("Heading") or query,
                "snippet": abstract,
                "url": data.get("AbstractURL"),
            }
        )
    for topic in data.get("RelatedTopics") or []:
        if not isinstance(topic, dict):
            continue
        if topic.get("Text"):
            results.append(
                {
                    "title": str(topic.get("Text") or "")[:80],
                    "snippet": topic.get("Text"),
                    "url": topic.get("FirstURL"),
                }
            )
        for nested in topic.get("Topics") or []:
            if isinstance(nested, dict) and nested.get("Text"):
                results.append(
                    {
                        "title": str(nested.get("Text") or "")[:80],
                        "snippet": nested.get("Text"),
                        "url": nested.get("FirstURL"),
                    }
                )
        if len(results) >= 8:
            break
    return results[:8]


def _public_search(query: str) -> dict[str, Any]:
    """Tavily o DuckDuckGo; nunca lanza: un bloqueo HTTP no tumba al agente."""
    tavily = _tavily_search(query)
    if tavily:
        return {"provider": "tavily", "query": query, "results": tavily}
    try:
        ddg = _duckduckgo_search(query)
        if ddg:
            return {"provider": "duckduckgo", "query": query, "results": ddg}
        return {
            "provider": "duckduckgo",
            "query": query,
            "results": [
                {
                    "title": f"Sin ficha instantánea para {query}",
                    "snippet": "DuckDuckGo Instant Answer no devolvió resultados. Reformula la consulta o usa otro dork.",
                    "url": f"https://duckduckgo.com/?q={quote_plus(query)}",
                }
            ],
        }
    except Exception as exc:
        return {
            "mode": "demo",
            "provider": "web_search",
            "query": query,
            "results": [
                {
                    "title": f"Resultados demo para {query}",
                    "snippet": "Sin Tavily ni DuckDuckGo disponibles. Búsqueda simulada.",
                    "url": f"https://duckduckgo.com/?q={quote_plus(query)}",
                }
            ],
            "error": str(exc),
        }


def _flatten_search_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = [str(payload.get("query") or "")]
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            chunks.append(str(item))
            continue
        chunks.append(str(item.get("title") or ""))
        chunks.append(str(item.get("snippet") or ""))
        chunks.append(str(item.get("url") or ""))
    return " ".join(chunks)


def _harvest_contacts(text: str) -> tuple[list[str], list[str]]:
    emails = sorted({match.lower() for match in EMAIL_RE.findall(text or "")})
    phones = sorted({match.group(0).strip() for match in PHONE_RE.finditer(text or "")})
    return emails, phones


def _hunter_error_detail(body: Any, status: int) -> str:
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list):
        details = [
            str(item.get("details") or item.get("code") or item)
            for item in errors
            if item is not None
        ]
        if details:
            return "; ".join(details)
    return f"HTTP {status}"


def _format_hunter_domain_search(domain: str, data: dict[str, Any]) -> str:
    emails = [item for item in (data.get("emails") or []) if isinstance(item, dict)]
    organization = data.get("organization")
    header = f"Hunter.io — dominio {domain}"
    if organization:
        header += f" ({organization})"
    if not emails:
        return f"{header}: sin correos públicos en Hunter.io. Cruza datos con otra herramienta."

    lines: list[str] = []
    for item in emails[:5]:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        attrs: list[str] = []
        role = item.get("department") or item.get("type")
        if role:
            attrs.append(str(role))
        person = " ".join(
            str(item.get(part) or "").strip() for part in ("first_name", "last_name")
        ).strip()
        if person:
            attrs.append(person)
        if item.get("position"):
            attrs.append(str(item["position"]))
        if item.get("confidence") is not None:
            attrs.append(f"confianza {item['confidence']}%")
        lines.append(f"- {value}" + (f" ({', '.join(attrs)})" if attrs else ""))

    total = len(emails)
    shown = len(lines)
    summary = f"{header}: {total} correo(s) público(s)"
    if total > shown:
        summary += f", muestro los {shown} primeros"
    pattern = data.get("pattern")
    if pattern:
        summary += f". Patrón corporativo: {pattern}"
    return "\n".join([summary + ":", *lines])


def _hunter_lookup(domain_or_company: str, person_name: str | None) -> dict[str, Any] | None:
    key = _hunter_api_key()
    if not key:
        return None

    params: dict[str, str] = {"api_key": key}
    if _looks_like_domain(domain_or_company) and person_name:
        url = HUNTER_EMAIL_FINDER_URL
        params["domain"] = domain_or_company.strip()
        params["full_name"] = person_name.strip()
        hunter_type = "email-finder"
    elif _looks_like_domain(domain_or_company):
        url = HUNTER_DOMAIN_SEARCH_URL
        params["domain"] = domain_or_company.strip()
        hunter_type = "domain-search"
    else:
        url = HUNTER_EMAIL_FINDER_URL
        params["full_name"] = (person_name or domain_or_company).strip()
        hunter_type = "email-finder"
    try:
        response = requests.get(url, params=params, timeout=15, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        return {"ok": False, "provider": "hunter.io", "type": hunter_type, "error": str(exc)}
    data = body.get("data") if isinstance(body, dict) else {}
    emails: list[str] = []
    if isinstance(data, dict):
        if data.get("email"):
            emails.append(str(data["email"]))
        for item in data.get("emails") or []:
            if isinstance(item, dict) and item.get("value"):
                emails.append(str(item["value"]))
            elif isinstance(item, str):
                emails.append(item)
    return {
        "ok": True,
        "provider": "hunter.io",
        "type": hunter_type,
        "endpoint": url,
        "emails": sorted(set(emails)),
        "raw": data if isinstance(data, dict) else {},
    }


def _probe_url(url: str) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        return {
            "url": url,
            "status": int(response.status_code),
            "active": int(response.status_code) == 200,
        }
    except Exception as exc:
        return {"url": url, "status": None, "active": False, "error": str(exc)}


@tool("web_search", args_schema=WebSearchInput)
def web_search(query: str) -> str:
    """Busca información pública en internet (Tavily si hay clave; si no, DuckDuckGo)."""
    mark_researching()
    return json.dumps(_public_search(query), ensure_ascii=False)


@tool("advanced_dork_search", args_schema=AdvancedDorkSearchInput)
def advanced_dork_search(query: str, site: str | None = None) -> str:
    """Búsqueda profunda con dorks. Si hay site, fuerza `site:{sitio} {query}`."""
    mark_researching()
    composed = _compose_dork(query, site)
    payload = _public_search(composed)
    payload["dork"] = composed
    payload["site"] = site
    payload["tool"] = "advanced_dork_search"
    return json.dumps(payload, ensure_ascii=False)


@tool("extract_social_profiles", args_schema=ExtractSocialProfilesInput)
def extract_social_profiles(name: str) -> str:
    """Simula dorks para localizar perfiles públicos de LinkedIn y Twitter/X."""
    mark_researching()
    cleaned = " ".join((name or "").strip().split())
    queries = [
        f"site:linkedin.com/in {cleaned}",
        f'site:x.com "{cleaned}"',
        f'site:twitter.com "{cleaned}"',
    ]
    return json.dumps(
        {
            "mode": "simulated",
            "name": cleaned,
            "queries": queries,
            "suggested_urls": [
                f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(cleaned)}",
                f"https://x.com/search?q={quote_plus(cleaned)}&src=typed_query",
            ],
            "note": "Pasa estas queries a advanced_dork_search para resultados reales.",
        },
        ensure_ascii=False,
    )


@tool("find_public_emails", args_schema=FindPublicEmailsInput)
def find_public_emails(domain: str) -> str:
    """Correos públicos de un dominio con el Domain Search real de Hunter.io."""
    mark_researching()
    target = (domain or "").strip().lstrip("@")
    if target.startswith("http://") or target.startswith("https://"):
        target = target.split("//", 1)[1]
    target = target.split("/")[0]
    if not target:
        return "Error: indica un dominio (ej. acme.com) para el Domain Search de Hunter.io."

    api_key = _hunter_api_key()
    if not api_key:
        return "Error: HUNTERIO_API_KEY no está configurada."

    try:
        response = requests.get(
            HUNTER_DOMAIN_SEARCH_URL,
            params={"domain": target, "api_key": api_key},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.exceptions.RequestException as exc:
        return f"Error de conexión con Hunter.io para {target}: {exc}"

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code != 200:
        return (
            f"Error de Hunter.io para {target}: {_hunter_error_detail(body, response.status_code)}"
        )

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return f"Hunter.io respondió sin datos utilizables para {target}."
    return _format_hunter_domain_search(target, data)


@tool("find_contact_info", args_schema=FindContactInfoInput)
def find_contact_info(domain_or_company: str, person_name: str | None = None) -> str:
    """Hunter.io si hay clave; si no, harvest de emails y teléfonos en texto público."""
    mark_researching()
    company = (domain_or_company or "").strip()
    person = (person_name or "").strip() or None
    hunter = _hunter_lookup(company, person)
    if hunter and hunter.get("ok"):
        hunter["domain_or_company"] = company
        hunter["person_name"] = person
        return json.dumps(hunter, ensure_ascii=False)

    query_parts = [part for part in (person, company) if part]
    query = " ".join(query_parts)
    if person and _looks_like_domain(company):
        query = f'"{person}" @{company} email OR correo OR contacto OR teléfono'
    elif person:
        query = f'"{person}" {company} email OR correo OR contacto OR teléfono'
    else:
        query = f"{company} email OR contacto OR teléfono OR phone"
    search = _public_search(query)
    emails, phones = _harvest_contacts(_flatten_search_text(search))
    payload: dict[str, Any] = {
        "mode": "harvest" if (emails or phones) else "demo",
        "provider": "public-text" if not hunter else "hunter.io+harvest",
        "domain_or_company": company,
        "person_name": person,
        "emails": emails,
        "phones": phones,
        "search": search,
    }
    if hunter and hunter.get("error"):
        payload["hunter_error"] = hunter.get("error")
    if _looks_like_domain(company):
        payload["type"] = "domain-search"
        payload["endpoint"] = f"https://api.hunter.io/v2/domain-search?domain={quote_plus(company)}"
    else:
        payload["type"] = "email-finder"
        payload["endpoint"] = (
            f"https://api.hunter.io/v2/email-finder?full_name={quote_plus(person or company)}"
        )
    return json.dumps(payload, ensure_ascii=False)


@tool("username_lookup", args_schema=UsernameLookupInput)
def username_lookup(username: str) -> str:
    """Comprueba si un alias exacto existe en GitHub, Instagram y Medium (HTTP 200 vs 404)."""
    mark_researching()
    handle = re.sub(
        r"[^A-Za-z0-9._-]",
        "",
        (username or "").strip().lstrip("@").split("/")[0],
    )
    networks: list[dict[str, Any]] = []
    for network, template in USERNAME_TARGETS:
        probed = _probe_url(template.format(username=handle))
        probed["network"] = network
        probed["username"] = handle
        networks.append(probed)
    active = [item["network"] for item in networks if item.get("active")]
    return json.dumps(
        {
            "username": handle,
            "active_networks": active,
            "profiles": networks,
        },
        ensure_ascii=False,
    )


RESEARCH_TOOLS = [
    web_search,
    advanced_dork_search,
    extract_social_profiles,
    find_public_emails,
    find_contact_info,
    username_lookup,
]
TOOLS = RESEARCH_TOOLS

research_agent_node = make_specialist_node(NAME, PROMPT)
