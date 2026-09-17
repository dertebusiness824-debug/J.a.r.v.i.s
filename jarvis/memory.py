"""Memoria semántica de largo plazo (ChromaDB local con fallback in-memory)."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from functools import lru_cache
from typing import Protocol

from jarvis.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_FACTS = [
    "Jarvis v2.0 es un sistema multi-agente: Supervisor, Code Agent, Comms Agent, Shop Agent y Research Agent.",
    "El Research Agent recopila información pública (OSINT): web_search, perfiles LinkedIn/Twitter y emails Hunter.io.",
    "El Code Agent opera solo dentro del sandbox configurado en WORKSPACE_ROOT.",
    "El Comms Agent envía WhatsApp por un puente local whatsapp-web.js (QR del número personal) y SMS por Zadarma PBX.",
    "El Shop Agent consulta productos, inventario y pedidos de Shopify por GraphQL Admin API.",
    "El Planificador razona y descompone tareas; el Ejecutor usa GPT-4o con function calling.",
    "El usuario trabaja en español, prioriza productividad extrema y respuestas concisas.",
]


class Retriever(Protocol):
    def add_texts(self, texts: list[str]) -> None: ...
    def retrieve(self, query: str, k: int = 4) -> str: ...


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-záéíóúñü0-9]+", text.lower())


def _bag(text: str) -> dict[str, float]:
    counts: dict[str, float] = {}
    for token in _tokenize(text):
        counts[token] = counts.get(token, 0.0) + 1.0
    return counts


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in set(a) | set(b))
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class InMemoryRetriever:
    """Retriever ligero (bolsa de palabras) para tests y arranque sin embeddings."""

    def __init__(self) -> None:
        self._docs: list[str] = []

    def add_texts(self, texts: list[str]) -> None:
        self._docs.extend(texts)

    def retrieve(self, query: str, k: int = 4) -> str:
        if not query.strip() or not self._docs:
            return ""
        q = _bag(query)
        ranked = sorted(self._docs, key=lambda doc: _cosine(q, _bag(doc)), reverse=True)
        hits = [doc for doc in ranked[:k] if _cosine(q, _bag(doc)) > 0]
        return "\n".join(f"- {h}" for h in hits)


class ChromaRetriever:
    def __init__(self) -> None:
        from langchain_chroma import Chroma
        from langchain_core.embeddings import FakeEmbeddings

        settings = get_settings()
        # FakeEmbeddings evita depender de una API de embeddings en el arranque local.
        # Sustituye por OpenAIEmbeddings cuando haya OPENAI_API_KEY y quieras semántica real.
        embeddings = FakeEmbeddings(size=32)
        if settings.openai_api_key:
            try:
                from langchain_openai import OpenAIEmbeddings

                embeddings = OpenAIEmbeddings(api_key=settings.openai_api_key)
            except Exception as exc:  # pragma: no cover
                logger.warning("OpenAIEmbeddings no disponible (%s). Usando FakeEmbeddings.", exc)

        self._store = Chroma(
            collection_name=settings.chroma_collection,
            persist_directory=str(settings.chroma_path),
            embedding_function=embeddings,
        )

    def add_texts(self, texts: list[str]) -> None:
        ids = [hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] for t in texts]
        self._store.add_texts(texts=texts, ids=ids)

    def retrieve(self, query: str, k: int = 4) -> str:
        docs = self._store.similarity_search(query, k=k)
        return "\n".join(f"- {d.page_content}" for d in docs)


class MemoryHub:
    def __init__(self, backend: Retriever) -> None:
        self.backend = backend

    def retrieve(self, query: str, k: int = 4) -> str:
        try:
            return self.backend.retrieve(query, k=k)
        except Exception as exc:
            logger.warning("Fallo de retrieval: %s", exc)
            return ""

    def remember(self, text: str) -> None:
        self.backend.add_texts([text])


def _build_backend() -> Retriever:
    settings = get_settings()
    if settings.offline:
        backend: Retriever = InMemoryRetriever()
        backend.add_texts(_DEFAULT_FACTS)
        return backend
    try:
        backend = ChromaRetriever()
        backend.add_texts(_DEFAULT_FACTS)
        return backend
    except Exception as exc:
        logger.warning("Chroma no disponible (%s). Fallback in-memory.", exc)
        backend = InMemoryRetriever()
        backend.add_texts(_DEFAULT_FACTS)
        return backend


@lru_cache(maxsize=1)
def get_memory() -> MemoryHub:
    return MemoryHub(_build_backend())
