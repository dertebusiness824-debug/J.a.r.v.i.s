"""Agente Investigador (OSINT): búsqueda pública, perfiles sociales y emails.

Mismo flujo interno que el resto de especialistas: el nodo invoca el subgrafo
Planificador → Ejecutor → Tools y reporta al Supervisor con `delegation_log`.
El `Command(goto="supervisor")` de `make_specialist_node` cierra la arista de vuelta.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote_plus

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from jarvis.agents.base import make_specialist_node
from jarvis.config import get_settings
from jarvis.prompts import RESEARCH_PROMPT

NAME = "research_agent"
PROMPT = RESEARCH_PROMPT


class WebSearchInput(BaseModel):
    query: str = Field(description="Consulta de búsqueda web (persona, empresa o tema).")


class ExtractSocialProfilesInput(BaseModel):
    name: str = Field(description="Nombre de la persona o empresa a localizar en redes.")


class FindPublicEmailsInput(BaseModel):
    domain_or_name: str = Field(
        description="Dominio (acme.com) o nombre de persona/empresa para Hunter.io."
    )


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
        search = TavilySearchResults(max_results=5)
        raw = search.invoke({"query": query})
    except Exception:
        try:
            search = TavilySearchResults(max_results=5)
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
    import requests

    response = requests.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        timeout=15,
        headers={"User-Agent": "JarvisResearch/1.0"},
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
        if len(results) >= 5:
            break
    return results[:5]


@tool("web_search", args_schema=WebSearchInput)
def web_search(query: str) -> str:
    """Busca información pública en internet (Tavily si hay clave; si no, DuckDuckGo)."""
    tavily = _tavily_search(query)
    if tavily:
        return json.dumps(
            {"provider": "tavily", "query": query, "results": tavily},
            ensure_ascii=False,
        )
    try:
        ddg = _duckduckgo_search(query)
        if ddg:
            return json.dumps(
                {"provider": "duckduckgo", "query": query, "results": ddg},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "provider": "duckduckgo",
                "query": query,
                "results": [
                    {
                        "title": f"Sin ficha instantánea para {query}",
                        "snippet": "DuckDuckGo Instant Answer no devolvió resultados. Reformula la consulta.",
                        "url": f"https://duckduckgo.com/?q={quote_plus(query)}",
                    }
                ],
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {
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
            },
            ensure_ascii=False,
        )


@tool("extract_social_profiles", args_schema=ExtractSocialProfilesInput)
def extract_social_profiles(name: str) -> str:
    """Simula dorks para localizar perfiles públicos de LinkedIn y Twitter/X."""
    cleaned = " ".join((name or "").strip().split())
    queries = [
        f"site:linkedin.com/in/ {cleaned}",
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
            "note": "Búsqueda simulada. Pasa estas queries a web_search para resultados reales.",
        },
        ensure_ascii=False,
    )


@tool("find_public_emails", args_schema=FindPublicEmailsInput)
def find_public_emails(domain_or_name: str) -> str:
    """Estructura una llamada a Hunter.io (simulada) para emails públicos."""
    query = (domain_or_name or "").strip()
    looks_like_domain = "." in query and " " not in query and "@" not in query
    if looks_like_domain:
        endpoint = f"https://api.hunter.io/v2/domain-search?domain={quote_plus(query)}"
        hunter_type = "domain-search"
    else:
        endpoint = f"https://api.hunter.io/v2/email-finder?full_name={quote_plus(query)}"
        hunter_type = "email-finder"
    settings = get_settings()
    return json.dumps(
        {
            "mode": "structured" if settings.hunter_api_key else "demo",
            "provider": "hunter.io",
            "query": query,
            "type": hunter_type,
            "endpoint": endpoint,
            "emails": [],
            "note": "Llamada simulada. Con HUNTER_API_KEY se enviaría este request firmado.",
        },
        ensure_ascii=False,
    )


RESEARCH_TOOLS = [web_search, extract_social_profiles, find_public_emails]
TOOLS = RESEARCH_TOOLS

research_agent_node = make_specialist_node(NAME, PROMPT)
