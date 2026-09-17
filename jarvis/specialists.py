from jarvis.prompts import CODE_PROMPT, COMMS_PROMPT, SHOP_PROMPT
from jarvis.supervisor import _specialist_node

code_agent_node = _specialist_node("code_agent")
comms_agent_node = _specialist_node("comms_agent")
shop_agent_node = _specialist_node("shop_agent")

__all__ = [
    "CODE_PROMPT",
    "COMMS_PROMPT",
    "SHOP_PROMPT",
    "code_agent_node",
    "comms_agent_node",
    "shop_agent_node",
]
