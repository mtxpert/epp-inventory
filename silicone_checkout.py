"""
Full automated checkout on siliconeintakes.com using only requests.

Flow:
1. Login via HTTP
2. Add items to cart
3. Load checkout_payment.php — extract Braintree client token
4. POST CC details to Braintree client API → get payment nonce
5. POST nonce + form fields to ec_process.php → order confirmed
"""
import re
import json
import base64
import requests as req
from flask import current_app

SI_PRODUCT_IDS = {
    'CLAMP-150': 106,
    'CLAMP-175': 107,
    'CLAMP-200': 100,
    'CLAMP-250': 102,
    'CLAMP-275': 103,
    'CLAMP-300': 104,
}

BASE = 'https://www.siliconeintakes.com'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def place_clamp_order(lines):
    """
    Place a full order on siliconeintakes.com — login, cart, tokenize CC, submit.
    Returns {'ok': True, 'order_number': '...'} or {'ok': False, 'error': '...'}
    """
    cfg = current_app.config
    si_user  = cfg.get('SI_USERNAME', '')
    si_pass  = cfg.get('SI_PASSWORD', '')
    cc_num   = cfg.get('SI_CC_NUMBER', '')
    cc_cvv   = cfg.get('SI_CC_CVV', '')
    cc_exp   = cfg.get('SI_CC_EXPIRY', '')   # MM/YY

    if not si_user or not si_pass or not cc_num:
        return {'ok': False, 'error': 'SI credentials or CC not configured'}

    unmapped = [l['part_number'] for l in lines if l['part_number'] not in SI_PRODUCT_IDS]
    if unmapped:
        return {'ok': False, 'error': f'No product ID for: {", ".join(unmapped)}'}

    s = req.Session()
    s.headers.update(HEADERS)

    # ── 1. Login ──────────────────────────────────────────────────────────────
    s.get(f'{BASE}/', timeout=15)   # accept cookies
    r = s.post(f'{BASE}/account.php',
               data={'login_email_address': si_user, 'login_password': si_pass, 'action': 'process'},
               allow_redirects=True, timeout=20)
    if 'logout' not in r.text.lower() and 'my account' not in r.text.lower():
        return {'ok': False, 'error': 'Login failed — check SI_USERNAME/SI_PASSWORD'}

    # ── 2. Clear cart and add items ───────────────────────────────────────────
    s.get(f'{BASE}/shopping_cart.php?action=remove_all', timeout=15)
    for line in lines:
        pid = SI_PRODUCT_IDS[line['part_number']]
        s.post(f'{BASE}/shopping_cart.php?action=add_product',
               data={'products_id': pid, 'cart_quantity': line['qty']},
               allow_redirects=True, timeout=15)

    # ── 3. Load payment page, extract Braintree token ────────────────────────
    pay_page = s.get(f'{BASE}/checkout_payment.php', allow_redirects=True, timeout=20)
    bt_token = _extract_bt_token(pay_page.text)
    if not bt_token:
        return {'ok': False, 'error': 'Could not find Braintree authorization token on payment page'}

    # ── 4. Tokenize CC via Braintree client API ───────────────────────────────
    nonce = _braintree_tokenize(bt_token, cc_num, cc_exp, cc_cvv)
    if not nonce:
        return {'ok': False, 'error': 'Braintree tokenization failed — check CC details'}

    # ── 5. Extract shipping option and submit order ───────────────────────────
    shipping_val = _extract_first_shipping(pay_page.text)

    form_data = {
        'payment': 'braintree_jh_creditcard',
        'btjh_credit_card_nonce': nonce,
        'accept_terms': 'on',
        'user_clicked_complete_order': 'Place Order',
    }
    if shipping_val:
        form_data['shipping'] = shipping_val

    order_r = s.post(f'{BASE}/ec_process.php', data=form_data,
                     allow_redirects=True, timeout=30)

    order_num = _extract_order_number(order_r.text, order_r.url)
    if 'checkout_success' in order_r.url or order_num:
        current_app.logger.info(f"SI order placed: {order_num}, url={order_r.url}")
        return {'ok': True, 'order_number': order_num or 'confirmed', 'url': order_r.url}

    # Check for error message on page
    err = _extract_error(order_r.text)
    current_app.logger.error(f"SI checkout failed. URL={order_r.url} Error={err}")
    return {'ok': False, 'error': err or f'Order not confirmed — ended at {order_r.url}'}


def load_cart(lines):
    """
    Login + add items to cart, return checkout URL (fallback if full auto-order fails).
    """
    cfg = current_app.config
    si_user = cfg.get('SI_USERNAME', '')
    si_pass = cfg.get('SI_PASSWORD', '')

    if not si_user or not si_pass:
        return {'ok': False, 'error': 'SI_USERNAME / SI_PASSWORD not configured'}

    unmapped = [l['part_number'] for l in lines if l['part_number'] not in SI_PRODUCT_IDS]
    if unmapped:
        return {'ok': False, 'error': f'No product ID for: {", ".join(unmapped)}'}

    s = req.Session()
    s.headers.update(HEADERS)
    s.get(f'{BASE}/', timeout=15)
    r = s.post(f'{BASE}/account.php',
               data={'login_email_address': si_user, 'login_password': si_pass, 'action': 'process'},
               allow_redirects=True, timeout=20)
    if 'logout' not in r.text.lower() and 'my account' not in r.text.lower():
        return {'ok': False, 'error': 'Login failed'}

    s.get(f'{BASE}/shopping_cart.php?action=remove_all', timeout=15)
    added = []
    for line in lines:
        pid = SI_PRODUCT_IDS[line['part_number']]
        r = s.post(f'{BASE}/shopping_cart.php?action=add_product',
                   data={'products_id': pid, 'cart_quantity': line['qty']},
                   allow_redirects=True, timeout=15)
        if r.status_code == 200:
            added.append({'part': line['part_number'], 'qty': line['qty']})

    os_csid = s.cookies.get('osCsid', '')
    checkout_url = f'{BASE}/checkout_shipping.php?osCsid={os_csid}'
    return {'ok': True, 'checkout_url': checkout_url, 'added': added}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_bt_token(html):
    """Pull Braintree authorization token from payment page HTML."""
    m = re.search(r'authorization:\s*["\']([A-Za-z0-9+/=_\-]{40,})["\']', html)
    return m.group(1) if m else None


def _extract_first_shipping(html):
    """Get the value of the first shipping radio button."""
    m = re.search(r'<input[^>]+name=["\']shipping["\'][^>]+value=["\']([^"\']+)["\']', html)
    return m.group(1) if m else None


def _braintree_tokenize(auth_token, cc_number, cc_expiry, cc_cvv):
    """
    Call Braintree's client API to tokenize a credit card.
    Returns the single-use payment nonce, or None on failure.
    """
    try:
        # Decode client token to get merchant ID and client API URL
        padded = auth_token + '=' * (4 - len(auth_token) % 4)
        token_data = json.loads(base64.b64decode(padded))
        client_api_url = token_data.get('clientApiUrl', '')
        fingerprint = token_data.get('authorizationFingerprint', '')
        if not client_api_url or not fingerprint:
            return None

        # Normalize expiry to MM/YYYY
        parts = cc_expiry.split('/')
        month = parts[0].zfill(2)
        year = parts[1] if len(parts) > 1 else ''
        if len(year) == 2:
            year = '20' + year
        expiry = f"{month}/{year}"

        url = f"{client_api_url}/v1/payment_methods/credit_cards"
        payload = {
            "creditCard": {
                "number": cc_number,
                "expirationDate": expiry,
                "cvv": cc_cvv,
            },
            "authorizationFingerprint": fingerprint,
            "_meta": {
                "source": "client",
                "integration": "dropin2",
                "sessionId": "epp-auto-order",
            }
        }
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Braintree-Version': '2018-05-10',
            'Authorization': f'Bearer {fingerprint}',
            'User-Agent': 'Braintree-Python-HTTP/1.0',
        }
        r = req.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code in (200, 201):
            data = r.json()
            cards = data.get('creditCards', [])
            if cards:
                return cards[0].get('nonce')
            # dropin2 format
            pm = data.get('paymentMethod', {})
            return pm.get('nonce')
    except Exception as e:
        try:
            from flask import current_app
            current_app.logger.error(f"Braintree tokenize error: {e}")
        except Exception:
            pass
    return None


def _extract_order_number(html, url):
    m = re.search(r'order_id=(\d+)', url)
    if m:
        return m.group(1)
    for pattern in [r'[Oo]rder\s*#?\s*(\d{4,})', r'[Oo]rder\s*[Nn]umber[:\s]+(\d+)',
                    r'order[_\s]?number["\s:]+(\d+)']:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None


def _extract_error(html):
    m = re.search(r'class=["\'](?:messageStackError|error)["\'][^>]*>(.*?)</[^>]+>', html, re.DOTALL)
    if m:
        return re.sub(r'<[^>]+>', '', m.group(1)).strip()[:200]
    return None
