#!/usr/bin/env python3
"""Test direct product page access for CCL, Scan, AWD-IT."""
import time
from playwright.sync_api import sync_playwright

TESTS = [
    ("CCL",    "https://www.cclonline.com/h610m-k-ddr4-gigabyte-h610m-k-ddr4-matx-motherboard-for-intel-lga1700-cpus-441030/"),
    ("Scan",   "https://www.scan.co.uk/products/gigabyte-h610m-k-ddr4-intel-h610-s-lga1700-ddr4-sata3-pcie-40-1x-m2-gbe-usb-32-gen1-matx"),
    ("AWD-IT", "https://www.awd-it.co.uk/gigabyte-h610m-k-ddr4-micro-atx-motherboard-lga-1700.html"),
]

def probe(page, name, url):
    print(f"\n{'='*65}\n  {name}\n  {url}\n{'='*65}")
    resp = page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(6)
    print(f"  Status: {resp.status if resp else 'N/A'} | URL: {page.url[:80]}")
    page.screenshot(path=f"/opt/openclaw/data/stic/probe_direct2_{name}.png")

    body = page.inner_text("body")[:200].lower().replace('\n',' ')
    print(f"  Body: {body[:150]}")

    # All elements with £ price text
    pound_els = page.evaluate("""() => {
        const results = [];
        for (const el of document.querySelectorAll('*')) {
            const direct = [...el.childNodes]
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent).join('').trim();
            if (/£\\s*\\d+\\.\\d{2}/.test(direct) && direct.length < 60) {
                results.push({
                    tag: el.tagName,
                    cls: (el.className||'').toString().substring(0,80),
                    id:  (el.id||'').substring(0,30),
                    text: direct.substring(0,50)
                });
                if (results.length >= 8) break;
            }
        }
        return results;
    }""")

    if pound_els:
        print(f"\n  ✅ £ price elements found:")
        for e in pound_els:
            ident = f"#{e['id']}" if e['id'] else f".{e['cls'][:60]}"
            print(f"    <{e['tag']}> {ident}")
            print(f"         {e['text']!r}")
    else:
        print("  ❌ No £ price elements")
        # Broader sweep
        broad = page.evaluate("""() => {
            return [...document.querySelectorAll('*')]
                .filter(e => (e.innerText||'').includes('£') && (e.innerText||'').length < 80)
                .slice(0,8)
                .map(e => ({
                    tag: e.tagName,
                    cls: (e.className||'').toString().substring(0,80),
                    text: e.innerText.trim().substring(0,60)
                }));
        }""")
        for e in broad:
            print(f"    <{e['tag']}> .{e['cls'][:60]} → {e['text']!r}")

    # JSON-LD
    json_ld = page.evaluate("""() => {
        return [...document.querySelectorAll('script[type="application/ld+json"]')]
            .map(s => s.textContent)
            .filter(t => /price/i.test(t))
            .map(t => t.substring(0,400));
    }""")
    if json_ld:
        print(f"\n  JSON-LD:")
        for j in json_ld[:1]:
            print(f"    {j[:300]}")

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
