# Jarvis v2.0 — Arquitectura Multi-Agente

Sistema de agentes autónomos con **Supervisor LangGraph**: el router recibe el prompt, delega a un especialista (Code, Comms, Shop o General) y espera el resultado.

## Stack

- **Orquestación:** LangGraph `StateGraph` (flujos cíclicos)
- **Planificación:** OpenAI o Anthropic (configurable; `o1` / Claude 3.5 Sonnet)
- **Ejecución / function calling:** GPT-4o
- **Memoria:** ChromaDB local (fallback in-memory)
- **API:** FastAPI asíncrono
- **Frontend/Voz:** Fase 2 (Next.js + Vapi). Esta fase incluye un playground mínimo en `/`

## Estructura

```
jarvis/
  supervisor.py        # Router: decide, pasa contexto, espera reporte
  agents/
    code_agent.py      # Filesystem + terminal (sandbox)
    comms_agent.py     # WhatsApp Cloud API + Twilio
    shop_agent.py      # Shopify GraphQL
    general.py
  agent_core.py        # Subgrafo Planificador → Ejecutor → Tools
  api/                 # FastAPI + webhooks
  integrations/        # Shopify / WhatsApp / Twilio
```

## Arranque

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # añade OPENAI_API_KEY o ANTHROPIC_API_KEY
uvicorn jarvis.api.main:app --reload --port 8000
```

Sin claves API el sistema entra en **modo offline**: router heurístico + herramientas reales (calculadora, sandbox, wrappers demo de Shopify/WhatsApp/Twilio).

```bash
python agent_core.py "¿Cuánto es 17 * 24?"
python -m jarvis "¿Qué productos hay en Shopify?"
pytest
```

## Grafo

```mermaid
flowchart TD
  U[Usuario] --> R[Retriever / Chroma]
  R --> S[Supervisor]
  S -->|code| C[Code Agent]
  S -->|comms| M[Comms Agent]
  S -->|shop| H[Shop Agent]
  S -->|general| G[General]
  C --> P[Planificador]
  M --> P
  H --> P
  G --> P
  P -->|plan| E[Ejecutor GPT-4o]
  E -->|tool_calls| T[Herramientas]
  T -->|ok| E
  T -->|error| P
  E -->|texto| P
  P -->|completo| S
  S -->|FINISH| END[Respuesta]
```

Endpoints: `GET /health`, `POST /invoke`, `GET /graph`, `GET|POST /webhooks/whatsapp`, `POST /webhooks/twilio`.
