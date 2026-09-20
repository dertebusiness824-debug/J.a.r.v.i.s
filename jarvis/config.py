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
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    planner_provider: str = Field(default="openai")
    planner_model: str = Field(default="meta-llama/llama-3.3-70b-instruct:free")
    executor_model: str = Field(default="meta-llama/llama-3.3-70b-instruct:free")

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
    whatsapp_allowed_number: str = "34605686509"
    whatsapp_verify_token: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None

    jarvis_host: str = "0.0.0.0"
    jarvis_port: int = 8000
    jarvis_max_iterations: int = 8
    jarvis_recursion_limit: int = 25
    jarvis_db_path: str = "./data/jarvis.db"
    jarvis_public_url: str = "https://j-a-r-v-i-s-yghr.onrender.com"
    # Command Emission: secreto compartido entre el backend y local_node.py. Sin él,
    # /api/commands queda abierto y cualquiera con la URL podría encolar archivos
    # hacia el PC del usuario.
    jarvis_node_token: str | None = None

    email_host: str | None = None
    email_user: str | None = None
    email_pass: str | None = None
    email_port: int = 993
    email_folder: str = "INBOX"
    email_use_ssl: bool = True

    vapi_public_key: str | None = None
    vapi_assistant_id: str | None = None
    vapi_webhook_secret: str | None = None
    # Por debajo del `timeoutSeconds` (90) que Vapi aplica al Custom LLM: mientras
    # la voz siga narrando el progreso no hay silencio que corte la llamada, así que
    # una investigación larga cabe entera en vez de tirarse a la basura a los 30 s.
    vapi_response_timeout_seconds: float = 75.0
    vapi_keepalive_seconds: float = 5.0
    # Margen antes de soltar la frase puente: si el Supervisor contesta dentro de
    # este tiempo no hay silencio que tapar y la llamada va directa a la respuesta.
    vapi_filler_delay_seconds: float = 0.15
    # Silencio máximo en llamada: si el grafo no da señales en este tiempo se dice
    # algo igualmente. Un comentario SSE no cuenta como voz para Vapi.
    # El TTS de Vapi se vacía en ~1–2 s sin texto nuevo. Un keep-alive SSE no
    # cuenta: hay que decir algo. 1.5 s tapa el hueco sin llenar la llamada de
    # «Sigo en ello» si el ejecutor ya está soltando tokens.
    vapi_idle_speech_seconds: float = 1.5

    cartesia_api_key: str | None = None
    cartesia_voice_id: str | None = None
    cartesia_model: str = "sonic-3.6"

    tavily_api_key: str | None = None
    hunterio_api_key: str | None = None
    hunter_api_key: str | None = None

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
    def database_url(self) -> str:
        path = Path(self.jarvis_db_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path.resolve()}"

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.openrouter_api_key or self.openai_api_key or self.anthropic_api_key)

    @property
    def offline(self) -> bool:
        return self.jarvis_offline or not self.has_llm_credentials


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
