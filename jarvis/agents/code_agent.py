"""Agente Programador: filesystem local (sandbox) + terminal."""

from jarvis.agents.base import make_specialist_node
from jarvis.prompts import CODE_PROMPT
from jarvis.tools import CODE_TOOLS

NAME = "code_agent"
TOOLS = CODE_TOOLS
PROMPT = CODE_PROMPT
code_agent_node = make_specialist_node(NAME, PROMPT)
