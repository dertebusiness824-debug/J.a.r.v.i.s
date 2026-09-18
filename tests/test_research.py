import json
from typing import ClassVar

import pytest
import requests

from jarvis.agents.research_agent import (
    _tavily_search,
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


class _FakeTavily:
    """Cliente de Tavily de mentira: apunta los parámetros y devuelve lo pactado."""

    calls: ClassVar[list[dict]] = []
    response: ClassVar[object] = {"results": []}

    def __init__(self, api_key=None, **_kwargs):
        self.api_key = api_key

    def search(self, **params):
        _FakeTavily.calls.append({"api_key": self.api_key, **params})
        if isinstance(_FakeTavily.response, Exception):
            raise _FakeTavily.response
        return _FakeTavily.response


@pytest.fixture
def fake_tavily(monkeypatch):
    """Instala el cliente falso y una clave, para no salir a la red de verdad."""
    _FakeTavily.calls = []
    _FakeTavily.response = {
        "results": [
            {
                "title": "Ada Lovelace",
                "content": "Primera programadora.",
                "url": "https://example.com/ada",
                "score": 0.93,
            }
        ]
    }
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr("tavily.TavilyClient", _FakeTavily)
    return _FakeTavily


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


def test_advanced_dork_search_passes_the_site_as_a_filter(monkeypatch):
    captured = {}

    def _fake_search(query, **options):
        captured["query"] = query
        captured["options"] = options
        return {"provider": "duckduckgo", "query": query, "results": []}

    monkeypatch.setattr("jarvis.agents.research_agent._public_search", _fake_search)
    data = json.loads(
        advanced_dork_search.invoke({"query": "Ada Lovelace", "site": "linkedin.com/in"})
    )
    # La consulta viaja limpia y el dominio va aparte: el `site:` dentro del texto no
    # lo entiende Tavily.
    assert captured["query"] == "Ada Lovelace"
    assert captured["options"]["site"] == "linkedin.com/in"
    # El dork sigue en la respuesta como traza de lo que se buscó.
    assert data["dork"] == "site:linkedin.com/in Ada Lovelace"


def test_tavily_search_sends_the_documented_parameters(fake_tavily):
    results = _tavily_search("Ada Lovelace", site="site:linkedin.com/in", time_range="month")

    call = fake_tavily.calls[0]
    assert call["api_key"] == "tvly-test"
    assert call["query"] == "Ada Lovelace"
    assert call["search_depth"] == "advanced"
    assert call["include_domains"] == ["linkedin.com/in"]
    assert call["time_range"] == "month"
    assert results == [
        {
            "title": "Ada Lovelace",
            "snippet": "Primera programadora.",
            "url": "https://example.com/ada",
            "score": 0.93,
        }
    ]


def test_tavily_search_ignores_controls_it_does_not_support(fake_tavily):
    _tavily_search("Ada Lovelace", topic="astrología", time_range="decade")

    call = fake_tavily.calls[0]
    assert "topic" not in call
    assert "time_range" not in call


def test_tavily_search_trims_queries_the_api_would_reject(fake_tavily):
    _tavily_search("Ada " * 300)

    assert len(fake_tavily.calls[0]["query"]) == 400


def test_tavily_search_falls_back_and_leaves_a_trace(fake_tavily, caplog):
    fake_tavily.response = RuntimeError("401 unauthorized")

    with caplog.at_level("WARNING"):
        assert _tavily_search("Ada Lovelace") is None

    # Una clave caducada tiene que verse en los logs, no pasar por "sin resultados".
    assert "401 unauthorized" in caplog.text


def test_tavily_search_needs_a_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert _tavily_search("Ada Lovelace") is None


def test_web_search_uses_tavily_when_there_is_a_key(fake_tavily):
    data = json.loads(web_search.invoke({"query": "Ada Lovelace", "time_range": "week"}))

    assert data["provider"] == "tavily"
    assert data["time_range"] == "week"
    assert data["results"][0]["url"] == "https://example.com/ada"
    assert fake_tavily.calls[0]["time_range"] == "week"


def test_web_search_does_not_send_empty_controls(fake_tavily):
    web_search.invoke({"query": "Ada Lovelace"})

    call = fake_tavily.calls[0]
    assert "topic" not in call
    assert "time_range" not in call
    assert "include_domains" not in call


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
