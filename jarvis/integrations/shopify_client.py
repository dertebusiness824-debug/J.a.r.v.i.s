"""Cliente GraphQL de Shopify Admin API (productos, inventario, pedidos)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from jarvis.config import get_settings

PRODUCTS_QUERY = """
query Products($first: Int!) {
  products(first: $first) {
    edges {
      node {
        id
        title
        handle
        status
        totalInventory
      }
    }
  }
}
"""

ORDERS_QUERY = """
query Orders($first: Int!, $query: String) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
    edges {
      node {
        id
        name
        displayFinancialStatus
        displayFulfillmentStatus
        createdAt
        totalPriceSet { shopMoney { amount currencyCode } }
      }
    }
  }
}
"""

INVENTORY_QUERY = """
query Inventory($id: ID!) {
  product(id: $id) {
    id
    title
    totalInventory
    variants(first: 50) {
      edges {
        node {
          id
          title
          sku
          inventoryQuantity
        }
      }
    }
  }
}
"""

INVENTORY_OVERVIEW_QUERY = """
query InventoryOverview($first: Int!) {
  products(first: $first) {
    edges {
      node {
        id
        title
        totalInventory
      }
    }
  }
}
"""

_DEMO_CATALOG = {
    "products": [
        {"id": "gid://shopify/Product/1", "title": "Auriculares Jarvis", "totalInventory": 42, "status": "ACTIVE"},
        {"id": "gid://shopify/Product/2", "title": "Dock USB-C", "totalInventory": 15, "status": "ACTIVE"},
    ],
    "orders": [
        {
            "id": "gid://shopify/Order/1001",
            "name": "#1001",
            "displayFinancialStatus": "PAID",
            "total": "89.00 USD",
        }
    ],
}


class ShopifyClient:
    """Cliente GraphQL. Sin credenciales opera en modo demo (datos sintéticos)."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.shopify_store_url and self.settings.shopify_access_token)

    @property
    def graphql_url(self) -> str:
        store = (self.settings.shopify_store_url or "").rstrip("/")
        if store.startswith("http"):
            base = store
        else:
            base = f"https://{store}"
        return f"{base}/admin/api/{self.settings.shopify_api_version}/graphql.json"

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            return {"data": {"demo": True, "note": "Shopify no configurado; usando catálogo sintético."}}
        headers = {
            "X-Shopify-Access-Token": self.settings.shopify_access_token or "",
            "Content-Type": "application/json",
        }
        payload = {"query": query, "variables": variables or {}}
        with httpx.Client(timeout=20.0) as client:
            response = client.post(self.graphql_url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    def list_products(self, first: int = 10) -> str:
        if not self.configured:
            return json.dumps(
                {"mode": "demo", "products": _DEMO_CATALOG["products"][:first]},
                ensure_ascii=False,
            )
        data = self.graphql(PRODUCTS_QUERY, {"first": first})
        return json.dumps(data, ensure_ascii=False)

    def list_orders(self, first: int = 10, query: str | None = None) -> str:
        if not self.configured:
            return json.dumps(
                {"mode": "demo", "orders": _DEMO_CATALOG["orders"][:first], "query": query},
                ensure_ascii=False,
            )
        data = self.graphql(ORDERS_QUERY, {"first": first, "query": query})
        return json.dumps(data, ensure_ascii=False)

    def inventory_summary(self, product_id: str | None = None) -> str:
        if not self.configured:
            if product_id:
                match = next((p for p in _DEMO_CATALOG["products"] if p["id"] == product_id), None)
                return json.dumps({"mode": "demo", "product": match or "no encontrado"}, ensure_ascii=False)
            total = sum(int(p["totalInventory"]) for p in _DEMO_CATALOG["products"])
            return json.dumps(
                {"mode": "demo", "totalInventory": total, "products": _DEMO_CATALOG["products"]},
                ensure_ascii=False,
            )
        if product_id:
            data = self.graphql(INVENTORY_QUERY, {"id": product_id})
        else:
            data = self.graphql(INVENTORY_OVERVIEW_QUERY, {"first": 25})
        return json.dumps(data, ensure_ascii=False)
