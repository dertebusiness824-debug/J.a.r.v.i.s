"""Agente de Comunicaciones: WhatsApp Web (whatsapp-web.js) + Zadarma PBX."""

from jarvis.agents.base import make_specialist_node
from jarvis.prompts import COMMS_PROMPT
from jarvis.tools import COMMS_TOOLS

NAME = "comms_agent"
TOOLS = COMMS_TOOLS
PROMPT = COMMS_PROMPT
comms_agent_node = make_specialist_node(NAME, PROMPT)
