"""Especialistas que el Supervisor puede invocar."""

from jarvis.agents.code_agent import NAME as CODE_NAME
from jarvis.agents.code_agent import code_agent_node
from jarvis.agents.comms_agent import NAME as COMMS_NAME
from jarvis.agents.comms_agent import comms_agent_node
from jarvis.agents.general import NAME as GENERAL_NAME
from jarvis.agents.general import general_agent_node
from jarvis.agents.research_agent import NAME as RESEARCH_NAME
from jarvis.agents.research_agent import research_agent_node
from jarvis.agents.shop_agent import NAME as SHOP_NAME
from jarvis.agents.shop_agent import shop_agent_node

SPECIALIST_NODES = {
    CODE_NAME: code_agent_node,
    COMMS_NAME: comms_agent_node,
    SHOP_NAME: shop_agent_node,
    RESEARCH_NAME: research_agent_node,
    GENERAL_NAME: general_agent_node,
}

__all__ = [
    "SPECIALIST_NODES",
    "code_agent_node",
    "comms_agent_node",
    "shop_agent_node",
    "research_agent_node",
    "general_agent_node",
]
