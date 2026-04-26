#!/usr/bin/env python3
"""
Standalone Argos price scraper using patchright (headed via Xvfb).
Called as subprocess by retailer_scraper.py.
Usage: xvfb-run --auto-servernum python3 argos_scrape.py "WL 3063295"
Prints: price as float, or NOT_FOUND

Argos uses Akamai Bot Manager. Headed patchright on residential IP bypasses it.
Product URL: https://www.argos.co.uk/product/{numeric_id}/
"""
import sys, re, time, random
from patchright.sync_api import sync_playwright

def parse_price(text):
    if not text:
        return None
    m = re.search(r'£?\s*([\d,]+\.?\d*)', text.replace(',', ''))
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return None

if len(sys.argv) < 2:
    print("NOT_FOUND")
    sys.exit(0)

raw_sku = sys.argv[1].strip()
# Extract numeric product ID: "WL 3063295" → "3063295"
m = re.search(r'(\d{6,8})', raw_sku)
if not m:
    print("NOT_FOUND")
    sys.exit(0)

product_id = m.group(1)
url = f"https://www.argos.co.uk/product/{product_id}/"

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-GB",
            timezone_id="Europe/London",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "DNT": "1",
            }
        )
        page = context.new_page()

        # Warm up via homepage to establish session/cookies
        page.goto("https://www.argos.co.uk/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(2, 4))

        # Accept cookies if banner present
        try:
            page.click('button:has-text("Accept all cookies")', timeout=4000)
            time.sleep(1)
        except Exception:
            pass

        # Navigate to product page
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status == 404:
            print("NOT_FOUND")
            browser.close()
            sys.exit(0)

        time.sleep(random.uniform(3, 5))

        # Try JSON-LD first
        price = page.evaluate("""() => {
            for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d.offers && d.offers.price) return parseFloat(d.offers.price);
                    if (d.offers && d.offers.lowPrice) return parseFloat(d.offers.lowPrice);
                    if (Array.isArray(d.offers)) {
                        for (const o of d.offers) {
                            if (o.price) return parseFloat(o.price);
                        }
                    }
                } catch(e) {}
            }
            return null;
        }""")

        if price and price > 0:
            print(float(price))
            browser.close()
            sys.exit(0)

        # Fallback: DOM selectors
        for sel in [
            "[data-test='product-price']",
            "[class*='ProductPrice']",
            "[class*='product-price']",
            "strong[data-test='transaction-price']",
            "span[aria-label*='£']",
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text()
                    p = parse_price(text)
                    if p and p > 0:
                        print(p)
                        browser.close()
                        sys.exit(0)
            except Exception:
                pass

        # Last resort: any £X.XX text in the page
        prices_found = page.evaluate("""() => {
            const els = [...document.querySelectorAll('*')];
            const prices = [];
            for (const el of els) {
                if (el.children.length > 0) continue;
                const t = el.innerText || '';
                const m = t.match(/^£\\s*(\\d+\\.\\d{2})$/);
                if (m) prices.push(parseFloat(m[1]));
            }
            return prices;
        }""")
        if prices_found:
            print(float(prices_found[0]))
            browser.close()
            sys.exit(0)

        print("NOT_FOUND")
        browser.close()

except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
