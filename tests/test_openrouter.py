from jarvis.config import Settings, get_settings
from jarvis.llms import HERMES_3, OPENROUTER_BASE_URL, get_executor_model, get_planner_model


def test_openrouter_key_alone_counts_as_credentials():
    settings = Settings(
        openrouter_api_key="sk-or-test",
        openai_api_key=None,
        anthropic_api_key=None,
        jarvis_offline=False,
    )
    assert settings.has_llm_credentials is True
    assert settings.offline is False


def test_executor_is_hermes_3_on_openrouter(monkeypatch):
    monkeypatch.setenv("JARVIS_OFFLINE", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-no-network")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        llm = get_executor_model()
        assert llm.model_name == HERMES_3
        assert float(llm.temperature) == 0.7
        assert int(llm.max_tokens) == 1500
        base = str(getattr(llm, "openai_api_base", None) or "")
        assert OPENROUTER_BASE_URL in base
        planner = get_planner_model()
        assert planner.model_name == HERMES_3
        assert planner.disable_streaming is True
    finally:
        get_settings.cache_clear()
