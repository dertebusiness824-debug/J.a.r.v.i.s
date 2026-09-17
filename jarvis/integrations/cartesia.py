"""Cliente TTS Cartesia (Sonic). Sin API key opera en modo no configurado."""

from __future__ import annotations

import httpx

from jarvis.config import get_settings

CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2026-08-14"
DEFAULT_MODEL = "sonic-3.6"


class CartesiaClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.cartesia_api_key and self.settings.cartesia_voice_id)

    def synthesize(self, transcript: str) -> bytes:
        if not self.configured:
            raise RuntimeError("Cartesia no configurado (CARTESIA_API_KEY / CARTESIA_VOICE_ID).")
        headers = {
            "Authorization": f"Bearer {self.settings.cartesia_api_key}",
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        }
        payload = {
            "model_id": self.settings.cartesia_model or DEFAULT_MODEL,
            "transcript": transcript,
            "voice": {"id": self.settings.cartesia_voice_id},
            "language": "es",
            "output_format": {
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": 22050,
            },
        }
        with httpx.Client(timeout=30.0) as client:
            response = client.post(CARTESIA_TTS_URL, headers=headers, json=payload)
            response.raise_for_status()
            return response.content
