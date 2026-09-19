import os

import pytest

os.environ["JARVIS_OFFLINE"] = "true"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

from jarvis import agent_core as agent_core_mod
from jarvis import supervisor as supervisor_mod
from jarvis.agents import base as agents_base
from jarvis.config import Settings, get_settings
from jarvis.db import reset_engine
from jarvis.integrations.zadarma import ZadarmaClient
from jarvis.memory import get_memory


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch, tmp_path):
    # Sin el .env real: ninguna prueba debe llegar a APIs de pago con las claves del dev.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("JARVIS_OFFLINE", "true")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "jarvis.db"))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("HUNTERIO_API_KEY", raising=False)
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "")
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    get_memory.cache_clear()
    reset_engine()
    ZadarmaClient.inbound_calls.clear()
    agent_core_mod._CORE_GRAPH = None
    agents_base.reset_core_subgraph()
    supervisor_mod._SUPERVISOR_GRAPH = None
    supervisor_mod._CHECKPOINTER = None
    yield
    get_settings.cache_clear()
    get_memory.cache_clear()
    reset_engine()
    ZadarmaClient.inbound_calls.clear()
