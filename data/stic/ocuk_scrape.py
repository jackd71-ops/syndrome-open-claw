#!/usr/bin/env python3
"""
Standalone OCUK price scraper using camoufox.
Called as subprocess by retailer_scraper.py.
Usage: python3 ocuk_scrape.py GRA-GIG-07639
Prints: price as float, or NOT_FOUND
"""
import sys
import re
from camoufox.sync_api import Camoufox

def parse_price(text):
    if not text:
        return None
    m = re.search(r'[\d,]+\.?\d*', text.replace(',', ''))
    return float(m.group()) if m else None

if len(sys.argv) < 2:
    print("NOT_FOUND")
    sys.exit(0)

code = sys.argv[1]

try:
    with Camoufox(headless=True, geoip=True, humanize=True) as browser:
        page = browser.new_page()
        page.goto(f'https://www.overclockers.co.uk/?query={code}',
                  wait_until='domcontentloaded', timeout=25000)
        try:
            page.wait_for_selector(f'[data-sku="{code}"]', timeout=8000)
        except Exception:
            print("NOT_FOUND")
            sys.exit(0)
        result = page.evaluate(f"""() => {{
            const el = document.querySelector('[data-sku="{code}"]');
            if (!el) return null;
            const container = el.closest('li, article') || el.parentElement.parentElement;
            const price = container.querySelector('span.price__amount:not(.price__amount--original)');
            return price ? price.innerText.trim() : null;
        }}""")
        price = parse_price(result)
        print(price if price is not None else "NOT_FOUND")
except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
