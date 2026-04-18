#!/usr/bin/env python3
"""
Standalone Scan price scraper using patchright (headed via Xvfb).
Called as subprocess by retailer_scraper.py.
Usage: xvfb-run --auto-servernum python3 scan_scrape.py <url>
Prints: price as float, or NOT_FOUND
"""
import sys, re, time
from patchright.sync_api import sync_playwright

def parse_price(text):
    if not text:
        return None
    m = re.search(r'[\d,]+\.?\d*', text.replace(',', ''))
    return float(m.group()) if m else None

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

        # Extract price — span[class*="price"], skip £0.00 placeholders
        prices = page.evaluate("""() => {
            const els = [...document.querySelectorAll('span[class*="price"]')];
            return els.map(e => e.innerText.trim()).filter(t => t.includes('£') && !t.includes('£0.00'));
        }""")

        price = None
        for text in prices:
            # Take the last £X.XX value in text (handles "Was: £116.99£109.99...")
            matches = re.findall(r'£([\d,]+\.?\d*)', text)
            if matches:
                price = parse_price(matches[-1])
                break

        print(price if price is not None else "NOT_FOUND")
        browser.close()

except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
