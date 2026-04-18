#!/usr/bin/env python3
"""
Standalone Very price scraper using patchright (headed via Xvfb).
Called as subprocess by retailer_scraper.py.
Usage: xvfb-run --auto-servernum python3 very_scrape.py <url>
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
        # Warm up via homepage to establish session cookies
        page.goto('https://www.very.co.uk/', wait_until='domcontentloaded', timeout=30000)
        time.sleep(3)
        resp = page.goto(url,
                         wait_until='domcontentloaded', timeout=30000)
        time.sleep(4)

        if resp.status == 403 or 'Access Denied' in page.title():
            print("NOT_FOUND", file=sys.stderr)
            print("Access denied", file=sys.stderr)
            sys.exit(1)

        # Try JSON-LD first (most reliable)
        jld = page.evaluate("""() => {
            const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
            return scripts.map(s => s.textContent).join('|');
        }""")
        prices = re.findall(r'"price"\s*:\s*"?([\d.]+)"?', jld)
        if prices:
            print(float(prices[0]))
            browser.close()
            sys.exit(0)

        # Fallback: DOM price elements
        dom_prices = page.evaluate("""() => {
            const els = [...document.querySelectorAll('[class*="price"]')];
            return els.map(e => e.innerText.trim()).filter(t => t.startsWith('£') && t.length < 10);
        }""")
        for text in dom_prices:
            price = parse_price(text.replace('£', ''))
            if price:
                print(price)
                browser.close()
                sys.exit(0)

        print("NOT_FOUND")
        browser.close()

except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
