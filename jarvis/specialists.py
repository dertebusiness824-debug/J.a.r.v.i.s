"""Compat: especialistas de primer nivel viven en `jarvis.agents`."""

from jarvis.agents import code_agent_node, comms_agent_node, research_agent_node, shop_agent_node
from jarvis.prompts import CODE_PROMPT, COMMS_PROMPT, RESEARCH_PROMPT, SHOP_PROMPT

__all__ = [
    "CODE_PROMPT",
    "COMMS_PROMPT",
    "RESEARCH_PROMPT",
    "SHOP_PROMPT",
    "code_agent_node",
    "comms_agent_node",
    "research_agent_node",
    "shop_agent_node",
]
