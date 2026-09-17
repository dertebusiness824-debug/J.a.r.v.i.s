import os

import pytest

os.environ["JARVIS_OFFLINE"] = "true"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

from jarvis import agent_core as agent_core_mod
from jarvis import supervisor as supervisor_mod
from jarvis.agents import base as agents_base
from jarvis.config import get_settings
from jarvis.integrations.zadarma import ZadarmaClient
from jarvis.memory import get_memory


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_OFFLINE", "true")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    get_settings.cache_clear()
    get_memory.cache_clear()
    ZadarmaClient.inbound_calls.clear()
    agent_core_mod._CORE_GRAPH = None
    agents_base.reset_core_subgraph()
    supervisor_mod._SUPERVISOR_GRAPH = None
    supervisor_mod._CHECKPOINTER = None
    yield
    get_settings.cache_clear()
    get_memory.cache_clear()
    ZadarmaClient.inbound_calls.clear()
