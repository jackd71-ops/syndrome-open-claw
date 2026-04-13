#!/usr/bin/env python3
"""Find exact price selectors for CCL and AWD-IT direct product pages."""
import time, json
from playwright.sync_api import sync_playwright

TESTS = [
    ("CCL",    "https://www.cclonline.com/h610m-k-ddr4-gigabyte-h610m-k-ddr4-matx-motherboard-for-intel-lga1700-cpus-441030/"),
    ("AWD-IT", "https://www.awd-it.co.uk/gigabyte-h610m-k-ddr4-micro-atx-motherboard-lga-1700.html"),
]

def probe(page, name, url):
    print(f"\n{'='*65}\n  {name}\n{'='*65}")
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(6)

    if name == "CCL":
        # Find the specific product price element (not related products)
        result = page.evaluate("""() => {
            // Try common price container IDs/classes
            const candidates = [
                document.querySelector('#product-price'),
                document.querySelector('.product-price'),
                document.querySelector('[itemprop="price"]'),
                document.querySelector('.price-box'),
                document.querySelector('#our_price_display'),
                document.querySelector('.our_price_display'),
                document.querySelector('[data-price]'),
            ];
            // Also dump all elements with £ and short text with their full selector path
            const all = [];
            for (const el of document.querySelectorAll('*')) {
                const t = (el.innerText||'').trim();
                if (/^£\\s*\\d+\\.\\d{2}$/.test(t)) {
                    const path = [];
                    let cur = el;
                    while (cur && cur !== document.body && path.length < 5) {
                        let seg = cur.tagName.toLowerCase();
                        if (cur.id) seg += '#' + cur.id;
                        else if (cur.className) seg += '.' + cur.className.toString().trim().split(/\\s+/)[0];
                        path.unshift(seg);
                        cur = cur.parentElement;
                    }
                    all.push({text: t, path: path.join(' > ')});
                    if (all.length >= 10) break;
                }
            }
            // JSON-LD price
            let jsonPrice = null;
            for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d.offers) { jsonPrice = d.offers.price || d.offers.lowPrice; break; }
                    if (d['@graph']) {
                        for (const item of d['@graph']) {
                            if (item.offers) { jsonPrice = item.offers.price; break; }
                        }
                    }
                } catch(e) {}
            }
            return {candidates: candidates.map(c => c ? {tag:c.tagName, cls:(c.className||'').toString().substring(0,60), text:c.innerText.trim().substring(0,30)} : null), all, jsonPrice};
        }""")
        print(f"  Exact £X.XX elements:")
        for e in result['all']:
            print(f"    {e['text']:12s} → {e['path']}")
        print(f"  JSON-LD price: {result['jsonPrice']}")

    elif name == "AWD-IT":
        result = page.evaluate("""() => {
            // Try JSON-LD first (cleanest)
            for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d['@type'] === 'Product' && d.offers) {
                        return {method: 'json-ld', price: d.offers.price, currency: d.offers.priceCurrency, availability: d.offers.availability};
                    }
                } catch(e) {}
            }
            // Fallback: find the main product price (not related)
            const mainPrice = document.querySelector('.product-info-price .price, #product-price-1 .price, [data-price-type="finalPrice"] .price');
            if (mainPrice) return {method: 'css', selector: '.product-info-price .price', text: mainPrice.innerText.trim()};
            // Broader
            const all = [];
            for (const el of document.querySelectorAll('*')) {
                const t = (el.innerText||'').trim();
                if (/^£\\s*\\d+\\.\\d{2}$/.test(t)) {
                    const path = [];
                    let cur = el;
                    while (cur && cur !== document.body && path.length < 6) {
                        let seg = cur.tagName.toLowerCase();
                        if (cur.id) seg += '#' + cur.id;
                        else if (cur.className) seg += '.' + cur.className.toString().trim().split(/\\s+/)[0];
                        path.unshift(seg);
                        cur = cur.parentElement;
                    }
                    all.push({text: t, path: path.join(' > ')});
                    if (all.length >= 6) break;
                }
            }
            return {method: 'sweep', all};
        }""")
        print(f"  Result: {json.dumps(result, indent=2)}")

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
        time.sleep(4)
    browser.close()
