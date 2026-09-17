import json

import pytest
import requests

from jarvis.agents.research_agent import (
    advanced_dork_search,
    extract_social_profiles,
    find_contact_info,
    find_public_emails,
    username_lookup,
    web_search,
)
from jarvis.tools import tools_by_agent


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _hunter_payload(count=6):
    return {
        "data": {
            "domain": "acme.com",
            "organization": "Acme Inc",
            "pattern": "{first}.{last}",
            "emails": [
                {
                    "value": f"person{i}@acme.com",
                    "type": "personal",
                    "department": "executive" if i == 0 else "it",
                    "first_name": "Ada" if i == 0 else None,
                    "last_name": "Lovelace" if i == 0 else None,
                    "position": "CTO" if i == 0 else None,
                    "confidence": 90 - i,
                }
                for i in range(count)
            ],
        }
    }


def test_tools_by_agent_exposes_research_set():
    names = {t.name for t in tools_by_agent("research_agent")}
    assert names == {
        "web_search",
        "advanced_dork_search",
        "extract_social_profiles",
        "find_public_emails",
        "find_contact_info",
        "username_lookup",
    }


def test_extract_social_profiles_formats_dorks():
    data = json.loads(extract_social_profiles.invoke({"name": "Ada Lovelace"}))
    assert data["mode"] == "simulated"
    assert "site:linkedin.com/in Ada Lovelace" in data["queries"]
    assert any("twitter.com" in q for q in data["queries"])
    assert any("x.com" in q for q in data["queries"])


def test_advanced_dork_search_prefixes_site(monkeypatch):
    captured = {}

    def _fake_search(query):
        captured["query"] = query
        return {"provider": "duckduckgo", "query": query, "results": []}

    monkeypatch.setattr("jarvis.agents.research_agent._public_search", _fake_search)
    data = json.loads(
        advanced_dork_search.invoke({"query": "Ada Lovelace", "site": "linkedin.com/in"})
    )
    assert captured["query"] == "site:linkedin.com/in Ada Lovelace"
    assert data["dork"] == "site:linkedin.com/in Ada Lovelace"


def test_find_public_emails_requires_api_key():
    assert (
        find_public_emails.invoke({"domain": "acme.com"})
        == "Error: HUNTERIO_API_KEY no está configurada."
    )


def test_find_public_emails_calls_hunter_domain_search(monkeypatch):
    monkeypatch.setenv("HUNTERIO_API_KEY", "hunter-key")
    captured = {}

    def _fake_get(url, params=None, timeout=None, headers=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(_hunter_payload())

    monkeypatch.setattr("jarvis.agents.research_agent.requests.get", _fake_get)
    out = find_public_emails.invoke({"domain": "https://acme.com/contacto"})

    assert captured["url"] == "https://api.hunter.io/v2/domain-search"
    assert captured["params"] == {"domain": "acme.com", "api_key": "hunter-key"}
    assert "dominio acme.com (Acme Inc)" in out
    assert "6 correo(s) público(s)" in out
    assert "muestro los 5 primeros" in out
    assert "Patrón corporativo: {first}.{last}" in out
    assert "- person0@acme.com (executive, Ada Lovelace, CTO, confianza 90%)" in out
    assert "person5@acme.com" not in out


def test_find_public_emails_reports_empty_domain(monkeypatch):
    monkeypatch.setenv("HUNTERIO_API_KEY", "hunter-key")
    monkeypatch.setattr(
        "jarvis.agents.research_agent.requests.get",
        lambda *a, **k: _FakeResponse({"data": {"domain": "acme.com", "emails": []}}),
    )
    out = find_public_emails.invoke({"domain": "acme.com"})
    assert "sin correos públicos" in out


def test_find_public_emails_handles_connection_error(monkeypatch):
    monkeypatch.setenv("HUNTERIO_API_KEY", "hunter-key")

    def _boom(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("sin red")

    monkeypatch.setattr("jarvis.agents.research_agent.requests.get", _boom)
    out = find_public_emails.invoke({"domain": "acme.com"})
    assert out.startswith("Error de conexión con Hunter.io para acme.com")


def test_find_public_emails_handles_api_error(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "hunter-key")
    monkeypatch.setattr(
        "jarvis.agents.research_agent.requests.get",
        lambda *a, **k: _FakeResponse(
            {"errors": [{"details": "Clave inválida", "code": 401}]}, status_code=401
        ),
    )
    out = find_public_emails.invoke({"domain": "acme.com"})
    assert out == "Error de Hunter.io para acme.com: Clave inválida"


@pytest.mark.parametrize("domain", ["", "   "])
def test_find_public_emails_requires_domain(domain):
    assert "indica un dominio" in find_public_emails.invoke({"domain": domain})


def test_find_contact_info_harvests_without_hunter(monkeypatch):
    monkeypatch.setattr(
        "jarvis.agents.research_agent._public_search",
        lambda query: {
            "provider": "duckduckgo",
            "query": query,
            "results": [
                {
                    "title": "Contacto Acme",
                    "snippet": "Escribe a ada@acme.com o llama al +34 600 123 456",
                    "url": "https://acme.com/contacto",
                }
            ],
        },
    )
    data = json.loads(find_contact_info.invoke({"domain_or_company": "acme.com"}))
    assert data["mode"] == "harvest"
    assert "ada@acme.com" in data["emails"]
    assert data["phones"]


def test_username_lookup_reports_active_networks(monkeypatch):
    monkeypatch.setattr(
        "jarvis.agents.research_agent._probe_url",
        lambda url: {"url": url, "status": 200 if "github" in url else 404,
                     "active": "github" in url},
    )
    data = json.loads(username_lookup.invoke({"username": "@ada"}))
    assert data["username"] == "ada"
    assert data["active_networks"] == ["github"]
    assert len(data["profiles"]) == 3


def test_web_search_uses_duckduckgo_without_tavily(monkeypatch):
    monkeypatch.setattr("jarvis.agents.research_agent._tavily_search", lambda _q: None)
    monkeypatch.setattr(
        "jarvis.agents.research_agent._duckduckgo_search",
        lambda query: [
            {"title": "Ada Lovelace", "snippet": "matemática", "url": "https://example.com"}
        ],
    )
    data = json.loads(web_search.invoke({"query": "Ada Lovelace"}))
    assert data["provider"] == "duckduckgo"
    assert data["results"][0]["title"] == "Ada Lovelace"


def test_web_search_demo_when_providers_fail(monkeypatch):
    monkeypatch.setattr("jarvis.agents.research_agent._tavily_search", lambda _q: None)

    def _boom(_query):
        raise RuntimeError("red bloqueada")

    monkeypatch.setattr("jarvis.agents.research_agent._duckduckgo_search", _boom)
    data = json.loads(web_search.invoke({"query": "Ada Lovelace"}))
    assert data["mode"] == "demo"
    assert data["query"] == "Ada Lovelace"


def test_web_search_marks_hud_researching(monkeypatch):
    from jarvis.hud_live import clear_researching, is_researching

    monkeypatch.setattr("jarvis.agents.research_agent._tavily_search", lambda _q: None)
    monkeypatch.setattr("jarvis.agents.research_agent._duckduckgo_search", lambda _q: [])
    clear_researching()
    assert is_researching() is False
    web_search.invoke({"query": "Ada Lovelace"})
    assert is_researching() is True
    clear_researching()
    assert is_researching() is False
