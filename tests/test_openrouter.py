from jarvis.config import Settings, get_settings
from jarvis.llms import (
    OPENROUTER_BASE_URL,
    OPENROUTER_FREE_MODEL,
    _openrouter_model,
    get_executor_model,
    get_planner_model,
)


def test_openrouter_key_alone_counts_as_credentials():
    settings = Settings(
        openrouter_api_key="sk-or-test",
        openai_api_key=None,
        anthropic_api_key=None,
        jarvis_offline=False,
    )
    assert settings.has_llm_credentials is True
    assert settings.offline is False


def test_openrouter_model_prefers_the_free_llama_and_aliases():
    assert _openrouter_model(None) == OPENROUTER_FREE_MODEL
    assert _openrouter_model("gpt-4o") == OPENROUTER_FREE_MODEL
    assert _openrouter_model("nousresearch/hermes-3-llama-3.1-70b") == OPENROUTER_FREE_MODEL
    assert _openrouter_model("openrouter/free") == OPENROUTER_FREE_MODEL
    assert _openrouter_model("meta-llama/llama-3.3-70b-instruct:free") == OPENROUTER_FREE_MODEL


def test_executor_is_free_llama_on_openrouter(monkeypatch):
    monkeypatch.setenv("JARVIS_OFFLINE", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-no-network")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        llm = get_executor_model()
        assert llm.model_name == OPENROUTER_FREE_MODEL
        assert float(llm.temperature) == 0.7
        assert int(llm.max_tokens) == 1500
        base = str(getattr(llm, "openai_api_base", None) or "")
        assert OPENROUTER_BASE_URL in base
        headers = llm.default_headers or {}
        assert headers.get("HTTP-Referer")
        assert headers.get("X-Title") == "J.A.R.V.I.S."
        planner = get_planner_model()
        assert planner.model_name == OPENROUTER_FREE_MODEL
        assert planner.disable_streaming is True
    finally:
        get_settings.cache_clear()
