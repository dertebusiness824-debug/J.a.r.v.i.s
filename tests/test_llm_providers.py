import pytest

from jarvis.config import get_settings
from jarvis.llms import OfflineChatModel, get_executor_model, get_planner_model


@pytest.fixture
def online(monkeypatch):
    """Sale del modo offline sin dejar claves de otros proveedores en el entorno."""

    def _configure(**env):
        monkeypatch.setenv("JARVIS_OFFLINE", "false")
        for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PLANNER_PROVIDER"):
            monkeypatch.delenv(name, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    yield _configure
    get_settings.cache_clear()


def test_offline_without_any_key():
    settings = get_settings()
    assert settings.offline is True
    assert isinstance(get_planner_model(), OfflineChatModel)
    assert isinstance(get_executor_model(), OfflineChatModel)


def test_groq_key_alone_leaves_offline_mode(online):
    settings = online(GROQ_API_KEY="gsk-test")
    assert settings.has_llm_credentials is True
    assert settings.offline is False
    assert settings.llm_provider == "groq"


def test_groq_models_stream_for_vapi(online):
    settings = online(GROQ_API_KEY="gsk-test")
    planner = get_planner_model()
    executor = get_executor_model()
    assert type(planner).__name__ == "ChatGroq"
    assert type(executor).__name__ == "ChatGroq"
    assert planner.streaming is True
    assert executor.streaming is True
    assert planner.model_name == settings.groq_planner_model
    assert executor.model_name == settings.groq_executor_model


def test_groq_models_are_configurable(online):
    online(
        GROQ_API_KEY="gsk-test",
        GROQ_PLANNER_MODEL="llama-3.3-70b-versatile",
        GROQ_EXECUTOR_MODEL="llama-3.1-8b-instant",
    )
    assert get_planner_model().model_name == "llama-3.3-70b-versatile"
    assert get_executor_model().model_name == "llama-3.1-8b-instant"


def test_openai_key_alone_still_works(online):
    settings = online(OPENAI_API_KEY="sk-test")
    assert settings.llm_provider == "openai"
    assert type(get_planner_model()).__name__ == "ChatOpenAI"
    assert type(get_executor_model()).__name__ == "ChatOpenAI"


def test_explicit_provider_wins_over_groq(online):
    settings = online(GROQ_API_KEY="gsk-test", OPENAI_API_KEY="sk-test", PLANNER_PROVIDER="openai")
    assert settings.llm_provider == "openai"
    assert type(get_planner_model()).__name__ == "ChatOpenAI"


def test_anthropic_provider_selected_by_key(online):
    settings = online(ANTHROPIC_API_KEY="sk-ant-test", PLANNER_PROVIDER="anthropic")
    assert settings.llm_provider == "anthropic"
    assert type(get_planner_model()).__name__ == "ChatAnthropic"
