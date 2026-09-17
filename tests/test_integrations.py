import json
from unittest.mock import MagicMock, patch

import requests

from jarvis.integrations.messaging import WhatsAppClient, to_whatsapp_id
from jarvis.integrations.shopify_client import ShopifyClient
from jarvis.tools import send_whatsapp_message


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

    with patch("jarvis.integrations.messaging.requests.post", return_value=mock_response) as posted:
        out = json.loads(WhatsAppClient().send_text("+5215512345678", "hola taller"))
        assert out["ok"] is True
        assert out["channel"] == "whatsapp-web"
        args, kwargs = posted.call_args
        assert args[0] == "http://127.0.0.1:3000/send"
        assert kwargs["json"] == {"to": "5215512345678@c.us", "message": "hola taller"}

    with patch("requests.post", return_value=mock_response) as tool_posted:
        send_whatsapp_message.invoke({"to": "+5215512345678", "body": "hola taller"})
        assert tool_posted.call_args.args[0] == "http://127.0.0.1:3000/send"
        assert tool_posted.call_args.kwargs["json"]["message"] == "hola taller"


def test_whatsapp_send_demo_when_bridge_down():
    with patch("jarvis.integrations.messaging.requests.post", side_effect=requests.ConnectionError("down")):
        out = json.loads(WhatsAppClient().send_text("15550001111", "ping"))
    assert out["mode"] == "demo"
    assert out["channel"] == "whatsapp-web"
    assert out["to"] == "15550001111@c.us"
