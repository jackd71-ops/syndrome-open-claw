#!/usr/bin/env python3
"""Test Amazon dp/ product pages with full browser headers + longer waits."""
import time
from playwright.sync_api import sync_playwright

ASINS = ["B0BXFBN121", "B08HHMCCXV", "B0863KK2BP"]

def probe(page, asin):
    url = f"https://www.amazon.co.uk/dp/{asin}"
    print(f"\n{'='*60}\n  {asin}  {url}\n{'='*60}")

    # First visit Amazon homepage to get cookies
    page.goto("https://www.amazon.co.uk", wait_until="domcontentloaded", timeout=20000)
    time.sleep(3)

    # Now visit product page
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(6)

    final_url = page.url
    print(f"  Final URL: {final_url}")

    page.screenshot(path=f"/opt/openclaw/data/stic/probe_dp_{asin}.png")

    body = page.inner_text("body")[:300].lower().replace('\n',' ')
    print(f"  Body: {body[:200]}")

    # Try various price selectors
    selectors = [
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
        "#price_inside_buybox",
        ".priceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "[data-feature-name='priceInsideBuyBox'] .a-offscreen",
        ".reinventPricePriceToPayMargin .a-offscreen",
        ".aok-offscreen",
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if txt and '£' in txt:
                    print(f"  ✅ {sel!r:55s} → {txt!r}")
        except Exception:
            pass

    # JSON-LD check
    price_ld = page.evaluate("""() => {
        const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
        for (const s of scripts) {
            try {
                const d = JSON.parse(s.textContent);
                if (d.offers) return d.offers;
                if (d['@graph']) return d['@graph'].filter(x => x.offers);
            } catch(e) {}
        }
        return null;
    }""")
    if price_ld:
        print(f"  JSON-LD offers: {str(price_ld)[:200]}")

    # Meta og:price
    og_price = page.evaluate("""() => {
        const el = document.querySelector('meta[property="og:price:amount"], meta[name="twitter:data1"]');
        return el ? el.content : null;
    }""")
    if og_price:
        print(f"  og:price = {og_price!r}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True,
        args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width":1366,"height":768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-GB", timezone_id="Europe/London",
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        Object.defineProperty(navigator,'languages',{get:()=>['en-GB','en']});
        Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
        window.chrome={runtime:{}};
    """)
    page = ctx.new_page()
    for asin in ASINS:
        probe(page, asin)
        time.sleep(6)
    browser.close()
