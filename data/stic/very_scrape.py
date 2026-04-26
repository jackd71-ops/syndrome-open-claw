#!/usr/bin/env python3
"""
Standalone Very price scraper using patchright (headed via Xvfb).
Called as subprocess by retailer_scraper.py.
Usage: xvfb-run --auto-servernum python3 very_scrape.py <url>
Prints: price as float, or NOT_FOUND

Strategy: Very product pages are protected by Akamai Bot Manager.
Instead of navigating directly to the product page, we search Very's
own search box using the numeric product ID extracted from the URL.
The search results page shows prices and is not blocked by Akamai.

Example URL: https://www.very.co.uk/powercolor-rx-9070-xt-oc-16gb-red-devil/1601129347.prd
We search for "1601129347" — Very returns a single matching result with price.
"""
import sys, re, time, random
from patchright.sync_api import sync_playwright

def parse_price(text):
    if not text:
        return None
    m = re.search(r'£\s*([\d,]+\.?\d*)', text.replace(',', ''))
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return None

if len(sys.argv) < 2:
    print("NOT_FOUND")
    sys.exit(0)

url = sys.argv[1].strip()

# Extract numeric product ID from URL: .../1601129347.prd → "1601129347"
m = re.search(r'/(\d{8,12})\.prd', url)
if not m:
    print("NOT_FOUND")
    sys.exit(0)

product_id = m.group(1)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            locale='en-GB',
            timezone_id='Europe/London',
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-GB,en;q=0.9',
                'DNT': '1',
            }
        )
        page = context.new_page()

        # Warm up via homepage, accept cookies
        page.goto('https://www.very.co.uk/', wait_until='domcontentloaded', timeout=30000)
        time.sleep(random.uniform(4, 6))
        page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button')]
                .find(b => /accept all|allow all|accept cookies/i.test(b.textContent));
            if (btn) btn.click();
        }""")
        time.sleep(random.uniform(1, 2))

        # Search by numeric product ID — returns single matching result with price
        page.fill('#header-searchInput', product_id)
        time.sleep(random.uniform(0.5, 1.0))
        page.keyboard.press('Enter')
        time.sleep(random.uniform(4, 6))

        title = page.title()
        if 'access denied' in title.lower():
            print("NOT_FOUND", file=sys.stderr)
            print("Access denied on search results", file=sys.stderr)
            browser.close()
            sys.exit(1)

        # Extract price from the first search result card
        price_data = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*=".prd"]')];
            for (const a of links) {
                const card = a.closest('li, article, [class*="card"]') || a.parentElement;
                if (!card) continue;
                const priceEl = card.querySelector('[class*="price"], [class*="Price"]');
                if (priceEl) {
                    const t = priceEl.innerText.trim();
                    const m = t.match(/£\\s*([\\d,]+\\.?\\d*)/);
                    if (m) return parseFloat(m[1].replace(',', ''));
                }
            }
            // Fallback: any £X price on page
            const allText = document.body.innerText;
            const prices = [...allText.matchAll(/£\\s*([1-9][\\d,]*\\.\\d{2})/g)]
                .map(m => parseFloat(m[1].replace(',', '')))
                .filter(p => p > 5 && p < 5000);
            return prices.length ? prices[0] : null;
        }""")

        if price_data and price_data > 0:
            print(float(price_data))
            browser.close()
            sys.exit(0)

        print("NOT_FOUND")
        browser.close()

except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
