"""Señal en vivo para el HUD: el Research Agent está en OSINT."""

from __future__ import annotations

import threading
import time

RESEARCH_TOOLS = frozenset(
    {"web_search", "extract_social_profiles", "find_public_emails"}
)

_lock = threading.Lock()
_until = 0.0


def mark_researching(ttl: float = 45.0) -> None:
    global _until
    with _lock:
        _until = time.monotonic() + max(ttl, 1.0)


def clear_researching() -> None:
    global _until
    with _lock:
        _until = 0.0


def is_researching() -> bool:
    with _lock:
        return time.monotonic() < _until
