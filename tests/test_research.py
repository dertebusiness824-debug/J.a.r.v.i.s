import json

from jarvis.agents.research_agent import (
    extract_social_profiles,
    find_public_emails,
    web_search,
)
from jarvis.tools import tools_by_agent


def test_tools_by_agent_exposes_research_set():
    names = {t.name for t in tools_by_agent("research_agent")}
    assert names == {"web_search", "extract_social_profiles", "find_public_emails"}


def test_extract_social_profiles_formats_dorks():
    data = json.loads(extract_social_profiles.invoke({"name": "Ada Lovelace"}))
    assert data["mode"] == "simulated"
    assert "site:linkedin.com/in/ Ada Lovelace" in data["queries"]
    assert any("twitter.com" in q for q in data["queries"])
    assert any("x.com" in q for q in data["queries"])


def test_find_public_emails_structures_hunter_domain():
    data = json.loads(find_public_emails.invoke({"domain_or_name": "example.com"}))
    assert data["provider"] == "hunter.io"
    assert data["type"] == "domain-search"
    assert "api.hunter.io/v2/domain-search" in data["endpoint"]
    assert data["emails"] == []


def test_find_public_emails_structures_hunter_person():
    data = json.loads(find_public_emails.invoke({"domain_or_name": "Ada Lovelace"}))
    assert data["type"] == "email-finder"
    assert "email-finder" in data["endpoint"]


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
