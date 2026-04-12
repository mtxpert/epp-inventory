"""
Cart loading for siliconeintakes.com using requests.
Logs in, adds items to cart, returns a checkout URL the user opens to confirm payment.
"""
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


def load_cart(lines):
    """
    Log in to siliconeintakes.com, clear cart, add all lines, return checkout URL.

    lines: [{part_number, qty, unit_cost}, ...]
    Returns: {'ok': True, 'checkout_url': '...', 'added': [...]}
          or {'ok': False, 'error': '...'}
    """
    cfg = current_app.config
    si_user = cfg.get('SI_USERNAME', '')
    si_pass = cfg.get('SI_PASSWORD', '')

    if not si_user or not si_pass:
        return {'ok': False, 'error': 'SI_USERNAME / SI_PASSWORD not configured'}

    unmapped = [l['part_number'] for l in lines if l['part_number'] not in SI_PRODUCT_IDS]
    if unmapped:
        return {'ok': False, 'error': f'No product ID for: {", ".join(unmapped)}'}

    session = req.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

    # Login
    r = session.post(
        'https://www.siliconeintakes.com/account.php',
        data={'login_email_address': si_user, 'login_password': si_pass, 'action': 'process'},
        allow_redirects=True, timeout=20
    )
    if 'logout' not in r.text.lower() and 'my account' not in r.text.lower():
        return {'ok': False, 'error': 'siliconeintakes.com login failed'}

    # Clear existing cart
    session.get('https://www.siliconeintakes.com/shopping_cart.php?action=remove_all', timeout=15)

    # Add each line
    added = []
    for line in lines:
        pid = SI_PRODUCT_IDS[line['part_number']]
        r = session.post(
            'https://www.siliconeintakes.com/shopping_cart.php?action=add_product',
            data={'products_id': pid, 'cart_quantity': line['qty']},
            allow_redirects=True, timeout=15
        )
        if r.status_code == 200:
            added.append({'part': line['part_number'], 'qty': line['qty']})
        else:
            current_app.logger.warning(f"Cart add failed for {line['part_number']}: {r.status_code}")

    os_csid = session.cookies.get('osCsid', '')
    checkout_url = f'https://www.siliconeintakes.com/checkout_shipping.php?osCsid={os_csid}'

    return {'ok': True, 'checkout_url': checkout_url, 'added': added, 'session_id': os_csid}
