"""ShipStation v2 API — auto label purchase, Josh notification, Shopify fulfillment."""
import requests
from datetime import datetime, timezone
from flask import current_app
from flask_mail import Message

SHIPSTATION_BASE = "https://api.shipstation.com/v2"

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
SHOPIFY_LOCATION_ID = 67632070811


def _headers():
    key = current_app.config.get("SHIPSTATION_API_KEY", "")
    return {"API-Key": key, "Content-Type": "application/json"}


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
    if "noisemaker" in name or "nmd" in name:
        return WH_TEMPE, CARRIER_USPS, "usps_priority_mail", [
            {"package_code": PKG_USPS_MEDIUM_FR, "weight": {"value": 3, "unit": "pound"}}
        ]
    if "fusion" in name and "charge" in name:
        return WH_TEMPE, CARRIER_UPS, "ups_ground", [
            {"package_code": PKG_FUSION_CHARGE, "weight": {"value": 6, "unit": "pound"}}
        ]
    if "intake" in name and "filter" not in name:
        # Intake kit ships as two packages: pipes box + filter box
        return WH_TEMPE, CARRIER_UPS, "ups_ground", [
            {"package_code": PKG_INTAKE_PIPES, "weight": {"value": 6, "unit": "pound"}},
            {"package_code": PKG_FILTER, "weight": {"value": 2, "unit": "pound"}},
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


def email_label_to_josh(order_number, kit_name, recipient_name, label_data):
    """Email 4×6 label PDF and order details to Josh."""
    from app import mail
    tracking = label_data.get("tracking_number", "N/A")
    carrier = (label_data.get("carrier_code") or "").upper()
    service = (label_data.get("service_code") or "").replace("_", " ").title()

    msg = Message(
        subject=f"[SHIP] Order #{order_number} — {kit_name}",
        recipients=[JOSH_EMAIL],
        body=(
            f"Order to ship:\n\n"
            f"Order #:  {order_number}\n"
            f"Product:  {kit_name}\n"
            f"Ship To:  {recipient_name}\n"
            f"Service:  {carrier} {service}\n"
            f"Tracking: {tracking}\n\n"
            f"Label attached — print at 4×6.\n\n"
            f"— EPP Inventory"
        )
    )
    pdf = _fetch_label_pdf(label_data)
    if pdf:
        msg.attach(f"label_{order_number}.pdf", "application/pdf", pdf)
    mail.send(msg)
    return tracking


def fulfill_shopify_order(shopify_order_id, tracking_number, carrier_code):
    """Push tracking to Shopify and trigger customer notification email."""
    token = current_app.config.get("SHOPIFY_TOKEN", "")
    store = current_app.config.get("SHOPIFY_STORE", "edf236-3.myshopify.com")
    if not token:
        return None
    company_map = {"ups": "UPS", "usps": "USPS", "fedex": "FedEx"}
    company = company_map.get((carrier_code or "").lower(), (carrier_code or "").upper())

    # Get fulfillment order ID first
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

    payload = {
        "fulfillment": {
            "notify_customer": True,
            "tracking_info": {
                "number": tracking_number,
                "company": company,
            },
            "line_items_by_fulfillment_order": [
                {"fulfillment_order_id": fo_id} for fo_id in open_fos
            ],
        }
    }
    r = requests.post(
        f"https://{store}/admin/api/2024-01/fulfillments.json",
        json=payload,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=15
    )
    return r.json()


def auto_ship_order(order_number, shopify_order_id, kit_name, qty, ship_to, order_total=0):
    """
    Full auto-ship: buy label → email Josh → fulfill Shopify order.
    order_total: full order value — triggers adult signature if >= $750.
    Returns result dict with tracking_number and status flags.
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

    # 2. Email Josh
    try:
        email_label_to_josh(order_number, kit_name, ship_to.get("name", ""), label)
        result["josh_notified"] = True
    except Exception as e:
        current_app.logger.error(f"Josh email error for #{order_number}: {e}")
        result["josh_notified"] = False
        result["email_error"] = str(e)

    # 3. Fulfill Shopify order
    try:
        fulfill_result = fulfill_shopify_order(shopify_order_id, tracking, label.get("carrier_code", ""))
        result["shopify_fulfilled"] = "fulfillment" in (fulfill_result or {})
        if not result["shopify_fulfilled"]:
            result["fulfillment_response"] = fulfill_result
    except Exception as e:
        current_app.logger.error(f"Shopify fulfillment error for #{order_number}: {e}")
        result["shopify_fulfilled"] = False
        result["fulfillment_error"] = str(e)

    result["status"] = "ok"
    return result
