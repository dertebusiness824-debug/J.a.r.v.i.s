import json
from unittest.mock import MagicMock, patch

from jarvis.integrations.messaging import WhatsAppClient, to_whatsapp_id
from jarvis.integrations.shopify_client import ShopifyClient


def test_shopify_demo_catalog():
    client = ShopifyClient()
    assert client.configured is False
    products = json.loads(client.list_products(first=1))
    assert products["mode"] == "demo"
    assert products["products"][0]["title"] == "Auriculares Jarvis"
    inventory = json.loads(client.inventory_summary())
    assert inventory["totalInventory"] == 57


def test_whatsapp_extract_inbound():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "52", "id": "1", "text": {"body": "hola"}},
                                {"from": "52", "id": "2", "type": "image"},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    msgs = WhatsAppClient.extract_inbound(payload)
    assert msgs == [{"from": "52", "body": "hola", "id": "1"}]


def test_to_whatsapp_id_normalizes_e164():
    assert to_whatsapp_id("+5215512345678") == "5215512345678@c.us"
    assert to_whatsapp_id("5215512345678@c.us") == "5215512345678@c.us"


def test_whatsapp_send_posts_to_local_bridge():
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"ok": True}

    with patch("jarvis.integrations.messaging.httpx.Client") as client_cls:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        instance.post.return_value = mock_response
        client_cls.return_value = instance

        out = json.loads(WhatsAppClient().send_text("+5215512345678", "hola taller"))
        assert out["ok"] is True
        assert out["channel"] == "whatsapp-web"
        args, kwargs = instance.post.call_args
        assert args[0] == "http://127.0.0.1:3000/send"
        assert kwargs["json"] == {"to": "5215512345678@c.us", "message": "hola taller"}


def test_whatsapp_send_demo_when_bridge_down():
    out = json.loads(WhatsAppClient().send_text("15550001111", "ping"))
    assert out["mode"] == "demo"
    assert out["channel"] == "whatsapp-web"
    assert out["to"] == "15550001111@c.us"
