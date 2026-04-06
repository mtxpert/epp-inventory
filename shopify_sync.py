"""Shopify webhook handling and order sync."""
import hashlib
import hmac
import base64
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from flask import current_app
from models import db, Component, Kit, KitComponent, ShopifyOrder, InventoryLog

# Tial BOV variant IDs → label map
TIAL_VARIANT_IDS = {
    '43898646757531': 'Tial Q - Black',
    '43898646823067': 'Tial Q - Red',
    '43898646954139': 'Tial Q - Blue',
    '43898647117979': 'Tial Q - Purple',
    '43898647183515': 'Tial Q - Silver',
}

FULLRACE_EMAIL = 'sales@full-race.com'
JOSH_EMAIL = 'Durmajdesigns@gmail.com'


def verify_webhook(data, hmac_header, secret):
    """Verify Shopify webhook HMAC signature."""
    if not secret or not hmac_header:
        return False
    digest = hmac.new(secret.encode('utf-8'), data, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(computed, hmac_header)


def process_order(order_data):
    """Process a Shopify order — deduct inventory for matched kits."""
    order_id = str(order_data.get('id', ''))
    order_number = str(order_data.get('order_number', order_data.get('name', '')))
    total_price = str(order_data.get('total_price', ''))

    existing = ShopifyOrder.query.filter_by(shopify_order_id=order_id).first()
    if existing and existing.processed:
        return {'status': 'already_processed', 'order': order_number}

    if not existing:
        existing = ShopifyOrder(
            shopify_order_id=order_id,
            order_number=order_number,
            total_price=total_price,
            line_items_json=json.dumps(order_data.get('line_items', []))
        )
        db.session.add(existing)

    deductions = []
    line_items = order_data.get('line_items', [])

    for item in line_items:
        product_id = str(item.get('product_id', ''))
        variant_title = (item.get('variant_title') or '').lower()
        qty_ordered = item.get('quantity', 1)

        kits = Kit.query.filter_by(shopify_id=product_id).all()
        if not kits:
            continue

        matched_kit = None
        if len(kits) == 1:
            matched_kit = kits[0]
        else:
            for kit in kits:
                if kit.shopify_variant and kit.shopify_variant.lower() in variant_title:
                    matched_kit = kit
                    break
            if not matched_kit:
                matched_kit = kits[0]

        for kc in matched_kit.components:
            total_deduct = kc.quantity * qty_ordered
            kc.component.qty -= total_deduct
            log = InventoryLog(
                component_id=kc.component_id,
                qty_change=-total_deduct,
                reason=f"Order #{order_number} - {matched_kit.name}",
                order_id=order_id
            )
            db.session.add(log)
            deductions.append({
                'part': kc.component.part_number,
                'name': kc.component.name,
                'deducted': total_deduct,
                'remaining': kc.component.qty
            })

    existing.processed = True
    existing.processed_at = datetime.now(timezone.utc)
    db.session.commit()

    # Send Tial BOV supplier email if needed
    for item in line_items:
        vid = str(item.get('variant_id', ''))
        if vid in TIAL_VARIANT_IDS:
            # Get label from line item properties first, fall back to map
            props = {p['name']: p['value'] for p in item.get('properties', [])}
            bov_label = props.get('BOV', TIAL_VARIANT_IDS[vid])
            send_tial_bov_email(order_number, bov_label)

    return {
        'status': 'processed',
        'order': order_number,
        'deductions': deductions
    }


def _smtp_send(to_addrs, subject, body, cc=None):
    """Send email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    username = current_app.config.get('MAIL_USERNAME', '')
    password = current_app.config.get('MAIL_PASSWORD', '')
    sender = current_app.config.get('MAIL_DEFAULT_SENDER', username)
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = sender
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    msg['To'] = ', '.join(to_addrs)
    all_recipients = list(to_addrs)
    if cc:
        if isinstance(cc, str):
            cc = [cc]
        msg['Cc'] = ', '.join(cc)
        all_recipients += cc
    with smtplib.SMTP('smtp.forwardemail.net', 2525, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.login(username, password)
        s.sendmail(sender, all_recipients, msg.as_string())


def send_tial_bov_email(order_number, bov_label):
    """Email FullRace when a Tial BOV is ordered."""
    subject = f"New Order - {bov_label}"
    body = (
        f"Hey guys need another Tial BoV {bov_label} with 10psi spring please. "
        f"Josh will pick it up once you have it.\n\n"
        f"EPP Order #{order_number}"
    )
    try:
        _smtp_send(FULLRACE_EMAIL, subject, body, cc=JOSH_EMAIL)
        current_app.logger.info(f"Tial BOV email sent for order #{order_number}: {bov_label}")
    except Exception as e:
        current_app.logger.error(f"Failed to send Tial BOV email: {e}")


def get_low_stock_components():
    """Return components below their reorder threshold."""
    components = Component.query.all()
    return [c for c in components if c.qty <= c.reorder_threshold]


def sync_recent_orders(hours=6):
    """Pull recent UNFULFILLED orders from Shopify and process unprocessed ones.

    Scoped to unfulfilled only (fulfillment_status=null) to minimise API call volume.
    Includes exponential backoff on 429 responses.
    Runs every 6h (changed from 2h) as a backstop for missed webhooks — webhooks
    are the primary fulfillment trigger.

    ROLLBACK: restore from backup-graphql-migration-YYYYMMDD/shopify_sync.py
    """
    token = current_app.config.get('SHOPIFY_TOKEN')
    store = current_app.config.get('SHOPIFY_STORE')
    if not token or not store:
        return {'error': 'Shopify not configured'}

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    url = f"https://{store}/admin/api/2024-01/orders.json"
    headers = {"X-Shopify-Access-Token": token}
    params = {
        'status': 'any',
        'fulfillment_status': 'unfulfilled',  # skip already-fulfilled orders
        'created_at_min': since,
        'limit': 50
    }

    try:
        for attempt in range(4):
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                current_app.logger.warning(f"Shopify sync 429, retrying in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        if 'application/json' not in r.headers.get('Content-Type', ''):
            return {'error': f'Shopify returned non-JSON ({r.status_code}). Check SHOPIFY_TOKEN env var.'}
        orders = r.json().get('orders', [])
    except Exception as e:
        return {'error': str(e)}

    results = []
    for order in orders:
        if order.get('financial_status') in ('paid', 'partially_paid', None):
            try:
                result = process_order(order)
                results.append(result)
                # Auto-ship newly processed orders (same as webhook path)
                if result.get('status') == 'processed' and result.get('deductions'):
                    try:
                        from shipstation import auto_ship_from_order_data
                        auto_ship_from_order_data(order)
                    except Exception as ship_err:
                        current_app.logger.error(f"Auto-ship error for #{result.get('order')}: {ship_err}")
            except Exception as e:
                order_num = order.get('order_number') or order.get('name', '?')
                current_app.logger.error(f"Error processing order #{order_num}: {e}")
                results.append({'status': 'error', 'order': str(order_num), 'error': str(e)})

    try:
        low_stock = get_low_stock_components()
        if low_stock:
            send_low_stock_alert(low_stock)
    except Exception as e:
        current_app.logger.error(f"Error in low stock check: {e}")
        low_stock = []

    return {'synced': len(results), 'results': results, 'low_stock_count': len(low_stock)}


def send_low_stock_alert(components):
    """Send email alert for low stock items."""
    recipients = current_app.config.get('ALERT_RECIPIENTS', '').split(',')
    recipients = [r.strip() for r in recipients if r.strip()]
    if not recipients:
        return

    critical = [c for c in components if c.qty <= 0]
    warning = [c for c in components if 0 < c.qty <= c.reorder_threshold]

    body = "EPP Inventory Alert\n" + "=" * 40 + "\n\n"

    if critical:
        body += "CRITICAL - OUT OF STOCK:\n"
        for c in critical:
            body += f"  {c.part_number}: {c.name} — QTY: {c.qty}\n"
        body += "\n"

    if warning:
        body += "LOW STOCK WARNING:\n"
        for c in warning:
            body += f"  {c.part_number}: {c.name} — QTY: {c.qty} (reorder at {c.reorder_threshold})\n"
        body += "\n"

    body += f"\nTotal alerts: {len(components)}\n"
    body += "View inventory: " + current_app.config.get('APP_URL', 'https://epp-inventory.onrender.com') + "\n"

    try:
        _smtp_send(recipients, f"[EPP] {'CRITICAL: ' if critical else ''}Low Stock Alert — {len(components)} items", body)
        current_app.logger.info(f"Low stock alert sent to {recipients}")
    except Exception as e:
        current_app.logger.error(f"Failed to send alert: {e}")
