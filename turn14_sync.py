"""Turn14 Distribution API — inventory, pricing, and dropship integration."""
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import current_app

BASE_URL = "https://api.turn14.com"

# Lowering kit item mapping: mfr_part_number -> Turn14 item_id + metadata
LOWERING_KIT_ITEMS = {
    "2352":  {"id": "373110", "t14_pn": "bel2352",  "brand": "Belltech",       "name": "Drop Spindle Set"},
    "16001": {"id": "363810", "t14_pn": "bel16001", "brand": "Belltech",       "name": "Coilover Kit w/ Shocks"},
    "6443":  {"id": "167373", "t14_pn": "bel6443",  "brand": "Belltech",       "name": "FLIP Kit"},
    "6569":  {"id": "827706", "t14_pn": "umi6569",  "brand": "UMI Performance","name": "Viking Shock Absorbers"},
}


def _get_token():
    """Fetch a fresh OAuth2 Bearer token."""
    client_id = current_app.config.get("TURN14_CLIENT_ID", "")
    client_secret = current_app.config.get("TURN14_CLIENT_SECRET", "")
    r = requests.post(
        f"{BASE_URL}/v1/token",
        json={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data["expires_in"]


class Turn14Client:
    """Reusable client that caches the OAuth token until expiry."""

    def __init__(self):
        self._token = None
        self._expires_at = None

    def _headers(self):
        now = datetime.now(timezone.utc)
        if not self._token or not self._expires_at or now >= self._expires_at:
            token, expires_in = _get_token()
            self._token = token
            self._expires_at = now + timedelta(seconds=expires_in - 60)
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def get_pricing(self, item_id):
        """Return pricing dict: {can_purchase, has_map, map_price, retail_price, our_cost}."""
        r = requests.get(f"{BASE_URL}/v1/pricing/{item_id}", headers=self._headers(), timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return {}
        a = data[0].get("attributes", {})
        pricelists = {pl["name"]: pl["price"] for pl in a.get("pricelists", [])}
        return {
            "can_purchase": a.get("can_purchase"),
            "has_map": a.get("has_map"),
            "map_price": pricelists.get("MAP"),
            "retail_price": pricelists.get("Retail"),
            "our_cost": a.get("purchase_cost"),
        }

    def get_inventory(self, item_id):
        """Return inventory dict: {total_wh, mfr_stock, mfr_esd, warehouses}."""
        r = requests.get(f"{BASE_URL}/v1/inventory/{item_id}", headers=self._headers(), timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return {}
        a = data[0].get("attributes", {})
        inv = a.get("inventory", {})
        # inventory is a dict of {warehouse_code: qty} or list depending on response
        if isinstance(inv, dict):
            warehouses = {k: v for k, v in inv.items() if isinstance(v, int)}
            total_wh = sum(warehouses.values())
        else:
            warehouses = {}
            total_wh = 0
        mfr = a.get("manufacturer", {})
        return {
            "total_wh": total_wh,
            "warehouses": warehouses,
            "mfr_stock": mfr.get("stock", 0),
            "mfr_esd": mfr.get("esd"),
        }

    def get_shipping_quote(self, items, recipient, environment="production"):
        """
        Get shipping options for a customer address.
        items: list of {"item_id": str, "qty": int}
        recipient: {"name", "address", "address_2", "city", "state", "zip", "country", "phone_number", "is_shop_address"}
        Returns (quote_id, shipments) where shipments is a list of {location, type, items, shipping: [...sorted by cost]}
        """
        payload = {
            "data": {
                "environment": environment,
                "locations": [{
                    "location": "default",
                    "combine_in_out_stock": True,
                    "items": [{"item_identifier": i["item_id"], "item_identifier_type": "item_id", "quantity": str(i["qty"])} for i in items],
                    "shipping": {"shipping_code": 3},
                }],
                "acknowledge_prop_65": True,
                "recipient": recipient,
            }
        }
        r = requests.post(f"{BASE_URL}/v1/quote", json=payload, headers=self._headers(), timeout=20)
        r.raise_for_status()
        data = r.json()["data"]
        quote_id = data["id"]
        shipments = []
        for shipment in data["attributes"].get("shipment", []):
            shipping_opts = shipment.get("shipping", [])
            if isinstance(shipping_opts, list):
                shipments.append({
                    "location": shipment.get("location"),
                    "type": shipment.get("type"),
                    "items": shipment.get("items", []),
                    "shipping": sorted(shipping_opts, key=lambda x: x["cost"]),
                })
        return quote_id, shipments

    def place_order(self, po_number, quote_id, shipping_ids, environment="production"):
        """
        Place a dropship order using a quote result.
        quote_id: int returned by get_shipping_quote()
        shipping_ids: list of int shipping_quote_ids (one per shipment)
        Returns the Turn14 order response.
        """
        payload = {
            "data": {
                "environment": environment,
                "quote_id": quote_id,
                "po_number": po_number,
                "acknowledge_prop_65": True,
                "acknowledge_epa": True,
                "shipping": [{"shipping_id": sid} for sid in shipping_ids],
            }
        }
        r = requests.post(f"{BASE_URL}/v1/order/from_quote", json=payload, headers=self._headers(), timeout=20)
        r.raise_for_status()
        return r.json()


# Module-level singleton — reused across scheduled jobs to avoid re-authing
_client = None

def get_client():
    global _client
    if _client is None:
        _client = Turn14Client()
    return _client


def sync_lowering_kit_inventory():
    """
    Pull current pricing + inventory for all 4 lowering kit parts.
    Returns summary dict for display/logging.
    """
    client = get_client()
    results = {}
    for mfr_pn, meta in LOWERING_KIT_ITEMS.items():
        try:
            pricing = client.get_pricing(meta["id"])
            inventory = client.get_inventory(meta["id"])
            results[mfr_pn] = {
                "name": meta["name"],
                "brand": meta["brand"],
                "t14_id": meta["id"],
                "in_stock": (inventory.get("total_wh", 0) + inventory.get("mfr_stock", 0)) > 0,
                "wh_qty": inventory.get("total_wh", 0),
                "mfr_qty": inventory.get("mfr_stock", 0),
                "mfr_esd": inventory.get("mfr_esd"),
                "map_price": pricing.get("map_price"),
                "retail_price": pricing.get("retail_price"),
                "our_cost": pricing.get("our_cost"),
                "can_purchase": pricing.get("can_purchase"),
            }
        except Exception as e:
            current_app.logger.error(f"Turn14 sync error for {mfr_pn}: {e}")
            results[mfr_pn] = {"error": str(e)}
    return results
