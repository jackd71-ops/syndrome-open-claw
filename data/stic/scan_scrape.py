#!/usr/bin/env python3
"""
Standalone Scan price scraper using patchright (headed via Xvfb).
Called as subprocess by retailer_scraper.py.
Usage: xvfb-run --auto-servernum python3 scan_scrape.py <url>
Prints: price as float, OUT_OF_STOCK, or NOT_FOUND

OOS states on Scan:
  - Notify Me : div.notify-when-in-stock present (no stock, no ETA)
  - Pre-order  : span.out.stock or div[class*="preOrder"] present
Both are treated as OUT_OF_STOCK.

Price is scoped to the main product's .rightColumn / .priceAvailability
to avoid picking up prices from related-product cards below.
"""
import sys, re, time
from patchright.sync_api import sync_playwright

def parse_price(text):
    if not text:
        return None
    matches = re.findall(r'£([\d,]+\.?\d*)', text.replace(',', ''))
    for m in matches:
        try:
            v = float(m.replace(',', ''))
            if v > 0:
                return v
        except ValueError:
            pass
    return None

if len(sys.argv) < 2:
    print("NOT_FOUND")
    sys.exit(0)

url = sys.argv[1]

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            locale='en-GB', timezone_id='Europe/London',
        )
        page = context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        time.sleep(4)

        # Dismiss cookie banner if present
        try:
            page.click('button:has-text("Accept All")', timeout=3000)
            time.sleep(1)
        except Exception:
            pass

        result = page.evaluate("""() => {
            // ── OOS detection ────────────────────────────────────────────────
            // Notify Me state: no stock, no ETA
            if (document.querySelector('.notify-when-in-stock, span.notify-me')) {
                return {oos: true, price: null, state: 'notify'};
            }
            // Pre-order / out-of-stock class on Scan
            if (document.querySelector('span.out.stock, div[class*="preOrder"]')) {
                return {oos: true, price: null, state: 'preorder'};
            }

            // ── Price: scoped to main product area only ───────────────────────
            // Try rightColumn (main product detail column) first
            const mainArea = document.querySelector('.rightColumn') ||
                             document.querySelector('.priceAvailability') ||
                             document.querySelector('.priceWishlistBuy');

            if (mainArea) {
                const spans = [...mainArea.querySelectorAll('span[class*="price"]')];
                for (const s of spans) {
                    const t = s.innerText.trim();
                    if (t.includes('£') && !t.includes('£0.00')) {
                        const matches = [...t.matchAll(/£([\d,]+\.?\d*)/g)];
                        if (matches.length) {
                            const v = parseFloat(matches[matches.length-1][1].replace(',',''));
                            if (v > 0) return {oos: false, price: v, state: 'in_stock'};
                        }
                    }
                }
            }

            // Fallback: first span.price on page that's in a product-price context
            const allPrices = [...document.querySelectorAll('span[class*="price"]')]
                .filter(e => e.innerText.includes('£') && !e.innerText.includes('£0.00'));
            for (const el of allPrices) {
                const matches = [...el.innerText.matchAll(/£([\d,]+\.?\d*)/g)];
                if (matches.length) {
                    const v = parseFloat(matches[matches.length-1][1].replace(',',''));
                    if (v > 0) return {oos: false, price: v, state: 'fallback'};
                }
            }

            return {oos: false, price: null, state: 'not_found'};
        }""")

        if result and result.get('oos'):
            print("OUT_OF_STOCK")
        else:
            price = result.get('price') if result else None
            print(float(price) if price else "NOT_FOUND")

        browser.close()

except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
