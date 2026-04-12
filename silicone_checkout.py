"""
Automated checkout on siliconeintakes.com using Playwright.
Called from the reorder approval flow to place clamp orders without human intervention.
"""
import re
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


def place_clamp_order(lines):
    """
    Place an order on siliconeintakes.com for the given lines.

    lines: list of dicts — [{part_number, qty, unit_cost}, ...]

    Returns: {'ok': True, 'order_number': '...', 'total': ...}
         or: {'ok': False, 'error': '...'}
    """
    cfg = current_app.config

    si_user  = cfg.get('SI_USERNAME', '')
    si_pass  = cfg.get('SI_PASSWORD', '')
    cc_name  = cfg.get('SI_CC_NAME', '')
    cc_num   = cfg.get('SI_CC_NUMBER', '')
    cc_cvv   = cfg.get('SI_CC_CVV', '')
    cc_exp   = cfg.get('SI_CC_EXPIRY', '')   # MM/YY
    cc_zip   = cfg.get('SI_CC_ZIP', '')
    ship_first = cfg.get('SI_SHIP_FIRST', 'Joshua')
    ship_last  = cfg.get('SI_SHIP_LAST', 'Durmaj')
    ship_addr  = cfg.get('SI_SHIP_ADDRESS', '910 S Hohokam')
    ship_addr2 = cfg.get('SI_SHIP_ADDRESS2', '#118')
    ship_city  = cfg.get('SI_SHIP_CITY', 'Tempe')
    ship_state = cfg.get('SI_SHIP_STATE', 'AZ')
    ship_zip   = cfg.get('SI_SHIP_ZIP', '85281')

    if not si_user or not si_pass or not cc_num:
        return {'ok': False, 'error': 'SI credentials or CC not configured in env vars'}

    # Check all lines are mappable
    unmapped = [l['part_number'] for l in lines if l['part_number'] not in SI_PRODUCT_IDS]
    if unmapped:
        return {'ok': False, 'error': f'No product ID for: {", ".join(unmapped)}'}

    # ── Step 1: Login + add to cart via requests ──────────────────────────────
    session = req.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

    login_r = session.post(
        'https://www.siliconeintakes.com/account.php',
        data={'login_email_address': si_user, 'login_password': si_pass, 'action': 'process'},
        allow_redirects=True, timeout=20
    )
    if 'logout' not in login_r.text.lower() and 'my account' not in login_r.text.lower():
        return {'ok': False, 'error': 'siliconeintakes.com login failed'}

    # Clear cart first
    session.get('https://www.siliconeintakes.com/shopping_cart.php?action=remove_all', timeout=15)

    for line in lines:
        pid = SI_PRODUCT_IDS[line['part_number']]
        r = session.post(
            'https://www.siliconeintakes.com/shopping_cart.php?action=add_product',
            data={'products_id': pid, 'cart_quantity': line['qty']},
            allow_redirects=True, timeout=15
        )
        if r.status_code != 200:
            current_app.logger.warning(f"Cart add failed for {line['part_number']}: {r.status_code}")

    os_csid = session.cookies.get('osCsid', '')
    if not os_csid:
        return {'ok': False, 'error': 'No session cookie after login'}

    # ── Step 2: Complete checkout with Playwright (handles Braintree iframes) ──
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {'ok': False, 'error': 'Playwright not installed on this server'}

    cc_month, cc_year = (cc_exp.split('/') + [''])[:2]
    # Normalize year to 4-digit if needed
    if len(cc_year) == 2:
        cc_year = '20' + cc_year

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            # Transfer session cookie
            context.add_cookies([{
                'name': 'osCsid',
                'value': os_csid,
                'domain': 'www.siliconeintakes.com',
                'path': '/',
            }])
            page = context.new_page()
            page.set_default_timeout(30000)

            # ── Checkout step 1: shipping ──────────────────────────────────────
            page.goto('https://www.siliconeintakes.com/checkout_shipping.php', wait_until='domcontentloaded')

            # Fill shipping address if form is present
            if page.locator('input[name="firstname"]').count():
                page.fill('input[name="firstname"]', ship_first)
                page.fill('input[name="lastname"]', ship_last)
                page.fill('input[name="street_address"]', ship_addr)
                if page.locator('input[name="suburb"]').count():
                    page.fill('input[name="suburb"]', ship_addr2)
                page.fill('input[name="city"]', ship_city)
                page.fill('input[name="postcode"]', ship_zip)
                # State dropdown
                if page.locator('select[name="state"]').count():
                    page.select_option('select[name="state"]', label=ship_state)
                elif page.locator('input[name="state"]').count():
                    page.fill('input[name="state"]', ship_state)
                # Country — US
                if page.locator('select[name="country"]').count():
                    try:
                        page.select_option('select[name="country"]', label='United States')
                    except Exception:
                        pass

            # Select cheapest shipping method
            ship_radios = page.locator('input[type="radio"][name="shipping"]')
            if ship_radios.count() > 0:
                ship_radios.first.click()

            # Continue button
            cont = page.locator('input[type="submit"], button[type="submit"]')
            if cont.count():
                cont.first.click()
                page.wait_for_load_state('domcontentloaded')

            # ── Checkout step 2: payment ───────────────────────────────────────
            if 'checkout_payment' in page.url or page.locator('#braintree-hosted-field-number').count() == 0:
                page.goto('https://www.siliconeintakes.com/checkout_payment.php', wait_until='domcontentloaded')

            page.wait_for_timeout(2000)  # Let Braintree iframes load

            # Braintree hosted fields are iframes — switch into each one
            # Card number iframe
            _fill_braintree_iframe(page, 'braintree-hosted-field-number', cc_num)
            # Expiry iframe
            _fill_braintree_iframe(page, 'braintree-hosted-field-expirationDate',
                                   f"{cc_month.zfill(2)}/{cc_year[-2:]}")
            # CVV iframe
            _fill_braintree_iframe(page, 'braintree-hosted-field-cvv', cc_cvv)

            # Cardholder name (may be a regular input outside the iframe)
            for sel in ['input[name="cc_owner"]', 'input[id*="cardholder"]', 'input[placeholder*="Name"]']:
                if page.locator(sel).count():
                    page.fill(sel, cc_name)
                    break

            # Billing ZIP
            for sel in ['input[name="billing_zip"]', 'input[name="postcode"]',
                        '#braintree-postal-code', 'input[placeholder*="ZIP"]']:
                el = page.locator(sel)
                if el.count() and not _is_in_iframe(page, sel):
                    el.fill(cc_zip)
                    break
            # Also try Braintree postal code iframe
            _fill_braintree_iframe(page, 'braintree-hosted-field-postalCode', cc_zip)

            # Submit payment form
            pay_btn = page.locator('input[name="submit"], button[type="submit"], input[type="submit"]')
            if pay_btn.count():
                pay_btn.first.click()
                page.wait_for_load_state('domcontentloaded', timeout=45000)

            page.wait_for_timeout(3000)

            # ── Checkout step 3: confirmation ─────────────────────────────────
            if 'checkout_confirmation' in page.url:
                conf_btn = page.locator('input[value*="Confirm"], button:has-text("Confirm"), input[type="submit"]')
                if conf_btn.count():
                    conf_btn.first.click()
                    page.wait_for_load_state('domcontentloaded', timeout=45000)
                    page.wait_for_timeout(3000)

            # ── Extract order number from success page ─────────────────────────
            final_url = page.url
            page_text = page.content()
            order_num = _extract_order_number(page_text, final_url)

            browser.close()

            if 'checkout_success' in final_url or order_num:
                return {'ok': True, 'order_number': order_num or 'confirmed', 'url': final_url}
            else:
                # Grab error text for debugging
                err_el = page.locator('.messageStackError, .error, #messageStack').first
                err_text = err_el.inner_text() if err_el.count() else 'Unknown — check order history on siliconeintakes.com'
                current_app.logger.error(f"SI checkout ended at {final_url}. Page snippet: {page_text[:500]}")
                return {'ok': False, 'error': err_text, 'url': final_url}

    except Exception as e:
        current_app.logger.error(f"SI checkout Playwright error: {e}")
        return {'ok': False, 'error': str(e)}


def _fill_braintree_iframe(page, frame_id, value):
    """Fill a Braintree hosted field iframe."""
    try:
        iframe = page.frame_locator(f'#{frame_id}')
        inp = iframe.locator('input')
        if inp.count():
            inp.fill(value)
            return True
    except Exception:
        pass
    return False


def _is_in_iframe(page, selector):
    """Rough check — just returns False (used to avoid double-filling)."""
    return False


def _extract_order_number(html, url):
    """Try to pull an order number out of the success page."""
    # URL param: checkout_success.php?order_id=12345
    m = re.search(r'order_id=(\d+)', url)
    if m:
        return m.group(1)
    # Common patterns in page text
    for pattern in [r'[Oo]rder\s*#?\s*(\d{4,})', r'[Oo]rder\s*[Nn]umber[:\s]+(\d+)']:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None
