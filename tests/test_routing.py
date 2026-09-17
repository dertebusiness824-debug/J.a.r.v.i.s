from jarvis.agents.research_agent import RESEARCH_TOOLS
from jarvis.llms import OfflineChatModel, Plan, RouteDecision, _guess_username
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
    assert router.invoke("Ejecuta git status").next_agent == "code_agent"
    assert router.invoke("Manda un WhatsApp de confirmación").next_agent == "comms_agent"
    assert router.invoke("Envía un SMS por Zadarma").next_agent == "comms_agent"
    assert router.invoke("Lista el inventario de Shopify").next_agent == "shop_agent"
    assert router.invoke("Investiga a Ada Lovelace en Google").next_agent == "research_agent"
    assert router.invoke("Recopilar información pública de Acme Corp").next_agent == "research_agent"
    assert router.invoke("Buscar en Google el perfil de LinkedIn de Ana Pérez").next_agent == "research_agent"
    assert router.invoke("¿Cuánto es 3+4?").next_agent == "general"
    assert router.invoke("Consigue los correos públicos de acme.com").next_agent == "research_agent"
    assert router.invoke("Comprueba el username ada en github").next_agent == "research_agent"


def _osint_tool_call(query: str) -> dict:
    executor = OfflineChatModel(role="executor").bind_tools(RESEARCH_TOOLS)
    message = executor.invoke([{"role": "user", "content": query}])
    assert message.tool_calls, f"sin tool call para {query}"
    return message.tool_calls[0]


def test_offline_executor_sends_domain_to_find_public_emails():
    call = _osint_tool_call("Consigue los correos públicos de acme.com")
    assert call["name"] == "find_public_emails"
    assert call["args"] == {"domain": "acme.com"}


def test_offline_executor_falls_back_to_find_contact_info_without_domain():
    call = _osint_tool_call("Necesito el correo de Ada Lovelace")
    assert call["name"] == "find_contact_info"


def test_offline_planner_closes_on_the_executor_draft():
    """Si el ejecutor ya redactó sin herramientas, planificar otra vuelta no aporta."""
    from langchain_core.messages import AIMessage, HumanMessage

    planner = OfflineChatModel(role="planner").with_structured_output(Plan)
    conversation = [
        HumanMessage(content="Ejecuta en la terminal el comando sleep 6"),
        AIMessage(content="Sin herramientas aplicables. Tarea marcada para cierre."),
    ]
    plan = planner.invoke(conversation)
    assert plan.is_complete is True
    assert plan.final_answer == "Sin herramientas aplicables. Tarea marcada para cierre."
    # Sin borrador, el planificador sigue pidiendo trabajo.
    assert planner.invoke([HumanMessage(content="Lista los archivos del sandbox")]).is_complete is False


def test_guess_username_prefers_handle():
    assert _guess_username("Comprueba el username ada en github") == "ada"
    assert _guess_username("busca el alias @torvalds") == "torvalds"
