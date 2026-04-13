#!/usr/bin/env python3
"""Test Scan LN code URL formats and API endpoints."""
import time, json
from playwright.sync_api import sync_playwright

LN = "LN147124"
FULL_URL = "https://www.scan.co.uk/products/gigabyte-h610m-k-ddr4-intel-h610-s-lga1700-ddr4-sata3-pcie-40-1x-m2-gbe-usb-32-gen1-matx"

TESTS = [
    # Direct LN URL formats
    ("LN_direct",    f"https://www.scan.co.uk/products/{LN}"),
    ("LN_shop",      f"https://www.scan.co.uk/shop/product/detail/{LN}"),
    # Full slug URL (user-provided)
    ("full_slug",    FULL_URL),
    # Potential API/JSON endpoints
    ("api_ln",       f"https://www.scan.co.uk/api/products/{LN}"),
    ("api_price",    f"https://www.scan.co.uk/api/price?ln={LN}"),
]

def probe(page, name, url):
    print(f"\n{'='*65}\n  {name}\n  {url}\n{'='*65}")
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(5)
        status = resp.status if resp else "N/A"
        final  = page.url
        print(f"  Status: {status} | Final: {final[:80]}")

        body = page.inner_text("body")[:300].replace('\n', ' ')
        print(f"  Body: {body[:200]}")

        page.screenshot(path=f"/opt/openclaw/data/stic/probe_scan_{name}.png")

        # Check for price elements
        if status == 200 and "cloudflare" not in body.lower() and "just a moment" not in body.lower():
            prices = page.evaluate("""() => {
                return [...document.querySelectorAll('*')]
                    .filter(e => {
                        const t = (e.innerText||'').trim();
                        return /£\\s*\\d+\\.\\d{2}/.test(t) && t.length < 60;
                    })
                    .slice(0, 5)
                    .map(e => ({
                        tag: e.tagName,
                        cls: (e.className||'').toString().substring(0,60),
                        text: e.innerText.trim().substring(0,40)
                    }));
            }""")
            if prices:
                print(f"  ✅ Price elements:")
                for p in prices:
                    print(f"    <{p['tag']}> .{p['cls'][:50]} → {p['text']!r}")
            else:
                print("  ⚠️  Page loaded but no price found")

            # JSON-LD
            ld = page.evaluate("""() => {
                return [...document.querySelectorAll('script[type="application/ld+json"]')]
                    .map(s => s.textContent)
                    .filter(t => /price/i.test(t))
                    .map(t => t.substring(0, 300));
            }""")
            if ld:
                print(f"  JSON-LD: {ld[0][:200]}")

        # Check content-type for JSON API responses
        content_type = page.evaluate("() => document.contentType || ''")
        if 'json' in content_type.lower():
            try:
                data = json.loads(page.inner_text("body"))
                print(f"  JSON response: {str(data)[:200]}")
            except Exception:
                pass

    except Exception as e:
        print(f"  ERROR: {e}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True,
        args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width":1366,"height":768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-GB", timezone_id="Europe/London",
        extra_http_headers={"Accept-Language":"en-GB,en;q=0.9","DNT":"1"}
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        Object.defineProperty(navigator,'languages',{get:()=>['en-GB','en']});
        Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
        window.chrome={runtime:{}};
    """)
    page = ctx.new_page()
    for name, url in TESTS:
        probe(page, name, url)
        time.sleep(3)
    browser.close()
