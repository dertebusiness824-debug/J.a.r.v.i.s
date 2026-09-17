"""Configuración centralizada de Jarvis (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    planner_provider: str = Field(default="openai")
    planner_model: str = Field(default="gpt-4o")
    executor_model: str = Field(default="gpt-4o")

    jarvis_offline: bool = False

    chroma_dir: str = "./data/chroma"
    chroma_collection: str = "jarvis_memory"

    workspace_root: str = "./sandbox"
    terminal_timeout_seconds: int = 30

    shopify_store_url: str | None = None
    shopify_access_token: str | None = None
    shopify_api_version: str = "2024-10"

    zadarma_key: str | None = None
    zadarma_secret: str | None = None
    zadarma_pbx_id: str | None = None

    whatsapp_bridge_url: str = "http://127.0.0.1:3000"
    whatsapp_verify_token: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None

    jarvis_host: str = "0.0.0.0"
    jarvis_port: int = 8000
    jarvis_max_iterations: int = 8
    jarvis_recursion_limit: int = 25

    vapi_public_key: str | None = None
    vapi_assistant_id: str | None = None
    vapi_webhook_secret: str | None = None

    cartesia_api_key: str | None = None
    cartesia_voice_id: str | None = None
    cartesia_model: str = "sonic-3.6"

    @property
    def workspace_path(self) -> Path:
        path = Path(self.workspace_root)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    @property
    def chroma_path(self) -> Path:
        path = Path(self.chroma_dir)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.openai_api_key or self.anthropic_api_key)

    @property
    def offline(self) -> bool:
        return self.jarvis_offline or not self.has_llm_credentials


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
