"""ShipStation v2 API — auto label purchase, Josh notification, Shopify fulfillment."""
import time
import requests
from datetime import datetime, timezone
from flask import current_app
from flask_mail import Message

SHIPSTATION_BASE = "https://api.shipstation.com/v2"
SHIPSTATION_V1_BASE = "https://ssapi.shipstation.com"

# Warehouse IDs
WH_TEMPE = "se-1762487"
WH_GA    = "se-1762490"

# Carrier IDs
CARRIER_UPS  = "se-5182259"
CARRIER_USPS = "se-5182258"

# Custom package codes (created via API)
PKG_HOT_PIPES     = "custom_hot-pipes"      # 24×13×7 in
PKG_FUSION_CHARGE = "custom_fusion-charge"  # 28×17×7 in
PKG_INTAKE_PIPES  = "custom_intake-pipes"   # 24×15×11 in
PKG_FILTER        = "custom_filter"         # 9×9×9 in

# USPS flat rate package codes
PKG_USPS_SMALL_FR = "small_flat_rate_box"
PKG_USPS_MEDIUM_FR = "medium_flat_rate_box"
PKG_USPS_PADDED   = "flat_rate_padded_envelope"

JOSH_EMAIL = "Durmajdesigns@gmail.com"
MIKE_EMAIL = "info@ecopowerparts.com"

# Kits that ship from Mike (GA warehouse) — email goes to Mike only
MIKE_SHIPS = ("raptor",)
SHOPIFY_LOCATION_ID = 67632070811


def _headers():
    key = current_app.config.get("SHIPSTATION_API_KEY", "")
    return {"API-Key": key, "Content-Type": "application/json"}


def _v1_auth():
    import base64
    key = current_app.config.get("SHIPSTATION_V1_KEY", "")
    secret = current_app.config.get("SHIPSTATION_V1_SECRET", "")
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def _find_shipstation_order_id(order_number):
    """Look up ShipStation v1 order ID by order number."""
    r = requests.get(
        f"{SHIPSTATION_V1_BASE}/orders",
        params={"orderNumber": str(order_number)},
        headers=_v1_auth(),
        timeout=15
    )
    if not r.ok:
        return None
    orders = r.json().get("orders", [])
    return orders[0]["orderId"] if orders else None


def mark_shipped_v1(order_number, tracking_number, carrier_code, ship_date=None):
    """Mark a ShipStation order as shipped via v1 API so the order list shows 'Shipped'."""
    if not ship_date:
        ship_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    order_id = _find_shipstation_order_id(order_number)
    if not order_id:
        current_app.logger.warning(f"ShipStation v1: order #{order_number} not found, skipping markasshipped")
        return None
    payload = {
        "orderId": order_id,
        "carrierCode": (carrier_code or "").lower(),
        "shipDate": ship_date,
        "trackingNumber": tracking_number,
        "notifyCustomer": False,
        "notifySalesChannel": True,
    }
    r = requests.post(
        f"{SHIPSTATION_V1_BASE}/orders/markasshipped",
        json=payload,
        headers=_v1_auth(),
        timeout=15
    )
    if r.ok:
        return r.json()
    current_app.logger.error(f"ShipStation v1 markasshipped failed for #{order_number}: {r.status_code} {r.text}")
    return None


def _kit_shipping_config(kit_name, qty=1):
    """Map kit name → (warehouse_id, carrier_id, service_code, packages_list)."""
    name = kit_name.lower()

    if "raptor" in name:
        return WH_GA, CARRIER_USPS, "usps_priority_mail", [
            {"package_code": PKG_USPS_PADDED, "weight": {"value": 2, "unit": "pound"}}
        ]
    if "clamp" in name:
        pkg = PKG_USPS_SMALL_FR if qty <= 20 else PKG_USPS_MEDIUM_FR
        weight = max(1, round(qty * 0.3))
        return WH_TEMPE, CARRIER_USPS, "usps_priority_mail", [
            {"package_code": pkg, "weight": {"value": weight, "unit": "pound"}}
        ]
    if ("noisemaker" in name or "nmd" in name) and "upgrade" not in name:
        return WH_TEMPE, CARRIER_USPS, "usps_priority_mail", [
            {"package_code": PKG_USPS_MEDIUM_FR, "weight": {"value": 3, "unit": "pound"}}
        ]
    if ("noisemaker" in name or "nmd" in name) and "upgrade" in name:
        # NMD upgrade includes full hot pipes — ships UPS like hot pipes
        return WH_TEMPE, CARRIER_UPS, "ups_ground", [
            {"package_code": PKG_HOT_PIPES, "weight": {"value": 6, "unit": "pound"}}
        ]
    if "fusion" in name and "charge" in name:
        return WH_TEMPE, CARRIER_UPS, "ups_ground", [
            {"package_code": PKG_FUSION_CHARGE, "weight": {"value": 6, "unit": "pound"}}
        ]
    if "intake" in name and "filter" not in name:
        # Intake kit fits in hot-pipes box (24×13×7)
        return WH_TEMPE, CARRIER_UPS, "ups_ground", [
            {"package_code": PKG_HOT_PIPES, "weight": {"value": 6, "unit": "pound"}},
        ]
    if "filter" in name:
        return WH_TEMPE, CARRIER_UPS, "ups_ground", [
            {"package_code": PKG_FILTER, "weight": {"value": 2, "unit": "pound"}}
        ]
    # Default: hot pipes box (SHO, Explorer, Fusion Intake, all other pipe kits)
    return WH_TEMPE, CARRIER_UPS, "ups_ground", [
        {"package_code": PKG_HOT_PIPES, "weight": {"value": 6, "unit": "pound"}}
    ]


def create_label(order_number, kit_name, qty, ship_to, order_total=0):
    """
    Purchase a shipping label via ShipStation v2.
    ship_to: {name, address_line1, city_locality, state_province, postal_code, country_code}
    order_total: float — orders >= $750 get adult signature required (PayPal seller protection).
    Returns the full label response dict.
    """
    wh_id, carrier_id, service_code, packages = _kit_shipping_config(kit_name, qty)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Warn if balance is getting low (< $50)
    try:
        bal = get_balance()
        if bal:
            balance_val = float(bal.get("balance", bal.get("amount", 999)) or 999)
            if balance_val < 50:
                current_app.logger.warning(f"ShipStation balance low: ${balance_val:.2f} — top up soon")
    except Exception:
        pass

    shipment = {
        "carrier_id": carrier_id,
        "service_code": service_code,
        "ship_date": today,
        "warehouse_id": wh_id,
        "ship_to": ship_to,
        "packages": packages,
    }
    if float(order_total or 0) >= 750:
        shipment["confirmation"] = "adult_signature"

    # APO/FPO/DPO addresses require customs declaration even for domestic USPS
    apo_states = {"AP", "AE", "AA"}
    if ship_to.get("state_province", "").upper() in apo_states:
        shipment["customs"] = {
            "contents": "merchandise",
            "non_delivery": "return_to_sender",
            "customs_items": [
                {
                    "description": kit_name[:50],
                    "quantity": qty,
                    "value": {"amount": round(float(order_total or 0), 2), "currency": "USD"},
                    "country_of_origin": "US",
                    "harmonized_tariff_code": "8544.42"
                }
            ]
        }

    payload = {
        "label_format": "pdf",
        "label_layout": "4x6",
        "shipment": shipment,
    }
    r = requests.post(
        f"{SHIPSTATION_BASE}/labels",
        json=payload,
        headers=_headers(),
        timeout=30
    )
    r.raise_for_status()
    return r.json()


def get_balance():
    """Return ShipStation account balance dict or None on error."""
    try:
        r = requests.get(
            f"{SHIPSTATION_BASE}/accounts/list",
            headers=_headers(),
            timeout=10
        )
        if r.ok:
            data = r.json()
            # v2 returns list; grab first account's balance
            accounts = data if isinstance(data, list) else data.get("accounts", [])
            if accounts:
                return accounts[0]
    except Exception:
        pass
    return None


def void_label(label_id):
    """
    Void a ShipStation label and get a credit refund.
    Returns API response dict or raises on error.
    """
    r = requests.put(
        f"{SHIPSTATION_BASE}/labels/{label_id}/void",
        json={},
        headers=_headers(),
        timeout=15
    )
    r.raise_for_status()
    return r.json()


def _fetch_label_pdf(label_data):
    """Download label PDF bytes from label_download URL."""
    downloads = label_data.get("label_download", {})
    url = downloads.get("pdf") or downloads.get("href")
    if not url:
        return None
    try:
        r = requests.get(url, headers={"API-Key": current_app.config.get("SHIPSTATION_API_KEY", "")}, timeout=20)
        if r.ok:
            return r.content
    except Exception:
        pass
    return None


def email_label_to_josh(order_number, kit_name, recipient_name, label_data, line_items=None, ship_to=None):
    """Email 4×6 label PDF and order details to Josh (or Mike for Raptor kits)."""
    import re
    from app import mail
    tracking = label_data.get("tracking_number", "N/A")
    carrier = (label_data.get("carrier_code") or "").upper()
    service = (label_data.get("service_code") or "").replace("_", " ").title()

    # Build items block — only show items relevant to this kit, skip internal addon lines
    # Addon lines have no SKU and their title is just a price/color option (e.g. "White (+$150.00)")
    SKIP_PROPS = {"_io_parent_order_group", "_io_field_name"}
    options_lines = []
    for item in (line_items or []):
        title = item.get("title", "")
        sku = item.get("sku") or ""
        props = [p for p in (item.get("properties") or []) if p.get("name") not in SKIP_PROPS]

        # Skip pure addon lines: no SKU, title is just a price modifier, no meaningful props
        if not sku and re.match(r'^[^a-zA-Z]*\(\+?\$[\d.,]+\)', title.strip()) and not props:
            continue

        clean_title = re.sub(r'\s*\(\+?\$[\d.,]+\)', '', title).strip()
        variant = item.get("variant_title") or ""
        line = f"  • {clean_title}"
        if variant:
            line += f" — {variant}"
        for p in props:
            if p.get("name") and p.get("value"):
                line += f"\n      {p['name']}: {p['value']}"
        options_lines.append(line)

    options_block = "\n".join(options_lines) if options_lines else f"  • {kit_name}"

    # Build shipping address block
    addr_block = ""
    if ship_to:
        parts = [ship_to.get("address_line1", "")]
        if ship_to.get("address_line2"):
            parts.append(ship_to["address_line2"])
        parts.append(f"{ship_to.get('city_locality','')}, {ship_to.get('state_province','')} {ship_to.get('postal_code','')}")
        addr_block = "\n          ".join(p for p in parts if p.strip())

    # Raptor kits ship from Mike's GA warehouse — email Mike only
    kit_lower = kit_name.lower()
    if any(k in kit_lower for k in MIKE_SHIPS):
        to_emails = [MIKE_EMAIL]
        cc_emails = []
    else:
        to_emails = [JOSH_EMAIL]
        cc_emails = [MIKE_EMAIL]

    msg = Message(
        subject=f"[SHIP] Order #{order_number} — {kit_name}",
        recipients=to_emails,
        cc=cc_emails,
        body=(
            f"Order to ship:\n\n"
            f"Order #:  {order_number}\n"
            f"Ship To:  {recipient_name}\n"
            + (f"          {addr_block}\n" if addr_block else "")
            + f"Service:  {carrier} {service}\n"
            f"Tracking: {tracking}\n\n"
            f"Items:\n{options_block}\n\n"
            f"Label attached — print at 4×6.\n\n"
            f"— EPP Inventory"
        )
    )
    pdf = _fetch_label_pdf(label_data)
    if pdf:
        msg.attach(f"label_{order_number}.pdf", "application/pdf", pdf)
    mail.send(msg)
    return tracking


def fulfill_shopify_order(shopify_order_id, tracking_numbers, carrier_code):
    """Push tracking to Shopify and trigger customer notification email.
    tracking_numbers: single string or list of tracking numbers.
    For multiple tracking numbers, creates the fulfillment with the first tracking
    then updates via GraphQL to attach all numbers as separate lines.
    """
    token = current_app.config.get("SHOPIFY_TOKEN", "")
    store = current_app.config.get("SHOPIFY_STORE", "edf236-3.myshopify.com")
    if not token:
        return None
    company_map = {"ups": "UPS", "usps": "USPS", "fedex": "FedEx"}
    company = company_map.get((carrier_code or "").lower(), (carrier_code or "").upper())

    # Normalize to list
    if isinstance(tracking_numbers, list):
        tracking_list = [t for t in tracking_numbers if t]
    else:
        tracking_list = [t.strip() for t in (tracking_numbers or "").split(",") if t.strip()]
    if not tracking_list:
        return {"error": "no tracking numbers provided"}

    # Get open fulfillment order IDs
    fo_r = requests.get(
        f"https://{store}/admin/api/2024-01/orders/{shopify_order_id}/fulfillment_orders.json",
        headers={"X-Shopify-Access-Token": token},
        timeout=15
    )
    fo_r.raise_for_status()
    fulfillment_orders = fo_r.json().get("fulfillment_orders", [])
    open_fos = [fo["id"] for fo in fulfillment_orders if fo["status"] == "open"]
    if not open_fos:
        return {"error": "no open fulfillment orders"}

    time.sleep(0.6)  # stay under Shopify's 2 calls/sec limit
    # Create fulfillment with first tracking number (REST API)
    payload = {
        "fulfillment": {
            "notify_customer": True,
            "tracking_info": {"number": tracking_list[0], "company": company},
            "line_items_by_fulfillment_order": [
                {"fulfillment_order_id": fid} for fid in open_fos
            ],
        }
    }
    r = requests.post(
        f"https://{store}/admin/api/2024-01/fulfillments.json",
        json=payload,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=15
    )
    result = r.json()

    # If multiple tracking numbers, update via GraphQL to attach all as separate lines
    time.sleep(0.6)  # stay under Shopify's 2 calls/sec limit
    if len(tracking_list) > 1:
        fulfillment_id = (result.get("fulfillment") or {}).get("id")
        if fulfillment_id:
            ups_base = "https://www.ups.com/WebTracking?trackNums="
            usps_base = "https://tools.usps.com/go/TrackConfirmAction?tLabels="
            url_base = ups_base if company == "UPS" else usps_base
            gql_mutation = """
mutation FulfillmentTrackingInfoUpdate(
  $fulfillmentId: ID!
  $trackingInfoInput: FulfillmentTrackingInput!
) {
  fulfillmentTrackingInfoUpdate(
    fulfillmentId: $fulfillmentId
    trackingInfoInput: $trackingInfoInput
    notifyCustomer: false
  ) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""
            variables = {
                "fulfillmentId": f"gid://shopify/Fulfillment/{fulfillment_id}",
                "trackingInfoInput": {
                    "company": company,
                    "numbers": tracking_list,
                    "urls": [f"{url_base}{t}" for t in tracking_list],
                }
            }
            gql_r = requests.post(
                f"https://{store}/admin/api/2024-01/graphql.json",
                json={"query": gql_mutation, "variables": variables},
                headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                timeout=15
            )
            gql_data = gql_r.json()
            errors = (gql_data.get("data", {})
                              .get("fulfillmentTrackingInfoUpdate", {})
                              .get("userErrors", []))
            if errors:
                current_app.logger.error(f"GraphQL tracking update errors: {errors}")

    return result


def buy_label_and_notify_josh(order_number, kit_name, qty, ship_to, order_total=0, line_items=None):
    """
    Buy a ShipStation label and email Josh. Does NOT fulfill Shopify.
    Call fulfill_shopify_order() separately after all labels are bought.
    Returns result dict with tracking_number, carrier, label_id.
    """
    result = {"order_number": order_number, "kit_name": kit_name}

    # 1. Buy label
    try:
        label = create_label(order_number, kit_name, qty, ship_to, order_total=order_total)
        tracking = label.get("tracking_number")
        result["tracking_number"] = tracking
        result["carrier"] = label.get("carrier_code")
        result["label_id"] = label.get("label_id")
        current_app.logger.info(f"Label purchased for #{order_number}: {tracking}")
    except Exception as e:
        current_app.logger.error(f"ShipStation label error for #{order_number}: {e}")
        result["error"] = f"Label creation failed: {e}"
        return result

    # 2. Email Josh with full address
    try:
        email_label_to_josh(order_number, kit_name, ship_to.get("name", ""), label,
                            line_items=line_items, ship_to=ship_to)
        result["josh_notified"] = True
    except Exception as e:
        current_app.logger.error(f"Josh email error for #{order_number}: {e}")
        result["josh_notified"] = False
        result["email_error"] = str(e)

    result["status"] = "ok"
    return result


def auto_ship_from_order_data(order_data):
    """
    Full auto-ship triggered from a raw Shopify order dict (webhook or sync).
    Buys a label per kit line item, emails Josh/Mike, fulfills Shopify once with all tracking numbers.
    """
    from models import Kit
    order_number = str(order_data.get('order_number', ''))
    shopify_order_id = str(order_data.get('id', ''))
    sa = order_data.get('shipping_address') or order_data.get('billing_address') or {}
    ship_to = {
        'name': sa.get('name', ''),
        'address_line1': sa.get('address1', ''),
        'address_line2': sa.get('address2', '') or '',
        'city_locality': sa.get('city', ''),
        'state_province': sa.get('province_code', sa.get('province', '')),
        'postal_code': sa.get('zip', ''),
        'country_code': sa.get('country_code', 'US'),
        'phone': sa.get('phone', '') or '4805550000',
    }
    order_total = float(order_data.get('total_price', 0) or 0)
    all_line_items = order_data.get('line_items', [])

    label_results = []
    for item in all_line_items:
        product_id = str(item.get('product_id', ''))
        qty = item.get('quantity', 1)
        variant_title = (item.get('variant_title') or '').lower()
        kits = Kit.query.filter_by(shopify_id=product_id).all()
        if not kits:
            continue
        matched_kit = kits[0]
        if len(kits) > 1:
            for k in kits:
                if k.shopify_variant and k.shopify_variant.lower() in variant_title:
                    matched_kit = k
                    break
        r = buy_label_and_notify_josh(order_number, matched_kit.name, qty, ship_to,
                                      order_total=order_total, line_items=all_line_items)
        label_results.append(r)
        if r.get('error'):
            current_app.logger.error(f"Label error for {matched_kit.name} on #{order_number}: {r['error']}")

    trackings = [r['tracking_number'] for r in label_results if r.get('tracking_number')]
    carrier = next((r.get('carrier', '') for r in label_results if r.get('carrier')), '')
    if trackings:
        try:
            fulfill_shopify_order(shopify_order_id, trackings, carrier)
        except Exception as e:
            current_app.logger.error(f"Shopify fulfillment error for #{order_number}: {e}")
        try:
            mark_shipped_v1(order_number, ', '.join(trackings), carrier)
        except Exception as e:
            current_app.logger.error(f"ShipStation markasshipped error for #{order_number}: {e}")

    return label_results


def auto_ship_order(order_number, shopify_order_id, kit_name, qty, ship_to, order_total=0, line_items=None):
    """
    Full auto-ship for a single-kit order: buy label → email Josh → fulfill Shopify.
    For multi-kit orders use buy_label_and_notify_josh() per kit, then fulfill_shopify_order() once.
    """
    result = buy_label_and_notify_josh(order_number, kit_name, qty, ship_to,
                                       order_total=order_total, line_items=line_items)
    if result.get("error"):
        return result

    tracking = result.get("tracking_number")
    carrier = result.get("carrier", "")

    # Fulfill Shopify order
    try:
        fulfill_result = fulfill_shopify_order(shopify_order_id, tracking, carrier)
        result["shopify_fulfilled"] = "fulfillment" in (fulfill_result or {})
        if not result["shopify_fulfilled"]:
            result["fulfillment_response"] = fulfill_result
    except Exception as e:
        current_app.logger.error(f"Shopify fulfillment error for #{order_number}: {e}")
        result["shopify_fulfilled"] = False
        result["fulfillment_error"] = str(e)

    # Mark ShipStation order as shipped (v1) so order list shows "Shipped"
    try:
        v1_result = mark_shipped_v1(order_number, tracking, carrier)
        result["shipstation_marked_shipped"] = bool(v1_result)
    except Exception as e:
        current_app.logger.error(f"ShipStation v1 markasshipped error for #{order_number}: {e}")
        result["shipstation_marked_shipped"] = False

    return result

