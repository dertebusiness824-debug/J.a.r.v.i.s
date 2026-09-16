from jarvis.llms import OfflineChatModel, RouteDecision
from jarvis.memory import InMemoryRetriever, get_memory


def test_memory_retrieves_specialist_fact():
    memory = get_memory()
    ctx = memory.retrieve("Shopify pedidos inventario")
    assert "Shopify" in ctx or "shop" in ctx.lower()


def test_in_memory_ranks_relevant_doc():
    store = InMemoryRetriever()
    store.add_texts(["alpha beta", "shopify inventario pedidos", "zzzz"])
    hit = store.retrieve("inventario shopify", k=1)
    assert "shopify" in hit.lower()


def test_offline_router_code_comms_shop_general():
    model = OfflineChatModel(role="planner")
    router = model.with_structured_output(RouteDecision)
    assert router.invoke("Refactoriza el script Python del sandbox").next_agent == "code_agent"
    assert router.invoke("Manda un WhatsApp de confirmación").next_agent == "comms_agent"
    assert router.invoke("Lista el inventario de Shopify").next_agent == "shop_agent"
    assert router.invoke("¿Cuánto es 3+4?").next_agent == "general"
