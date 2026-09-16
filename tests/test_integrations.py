import json

from jarvis.integrations.messaging import WhatsAppClient
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
