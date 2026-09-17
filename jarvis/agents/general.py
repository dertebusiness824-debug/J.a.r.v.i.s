"""Agente general: cálculo, hora y consultas que no requieren un especialista de dominio."""

from jarvis.agents.base import make_specialist_node
from jarvis.tools import CORE_TOOLS

NAME = "general"
TOOLS = CORE_TOOLS
PROMPT = "Eres el agente general de Jarvis. Usa la calculadora y la hora; no inventes datos de tienda ni de mensajería."
general_agent_node = make_specialist_node(NAME, PROMPT)
