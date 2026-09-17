"""Agente de E-commerce: wrappers GraphQL de Shopify (productos, inventario, pedidos)."""

from jarvis.agents.base import make_specialist_node
from jarvis.prompts import SHOP_PROMPT
from jarvis.tools import SHOP_TOOLS

NAME = "shop_agent"
TOOLS = SHOP_TOOLS
PROMPT = SHOP_PROMPT
shop_agent_node = make_specialist_node(NAME, PROMPT)
