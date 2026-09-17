from jarvis.agents import SPECIALIST_NODES
from jarvis.agents.code_agent import TOOLS as CODE_TOOLS
from jarvis.agents.comms_agent import TOOLS as COMMS_TOOLS
from jarvis.agents.research_agent import TOOLS as RESEARCH_TOOLS
from jarvis.agents.shop_agent import TOOLS as SHOP_TOOLS


def test_specialists_are_first_class_nodes():
    assert set(SPECIALIST_NODES) == {
        "code_agent",
        "comms_agent",
        "shop_agent",
        "research_agent",
        "general",
    }


def test_code_agent_owns_filesystem_tools():
    names = {t.name for t in CODE_TOOLS}
    assert {"read_file", "write_file", "list_directory", "run_terminal"} <= names


def test_comms_agent_owns_messaging_tools():
    names = {t.name for t in COMMS_TOOLS}
    assert {"send_whatsapp_message", "send_zadarma_sms"} <= names


def test_shop_agent_owns_shopify_tools():
    names = {t.name for t in SHOP_TOOLS}
    assert {"shopify_list_products", "shopify_list_orders", "shopify_inventory_summary"} <= names


def test_research_agent_owns_osint_tools():
    names = {t.name for t in RESEARCH_TOOLS}
    assert {"web_search", "extract_social_profiles", "find_public_emails"} <= names
