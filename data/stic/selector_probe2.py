#!/usr/bin/env python3
"""Deep DOM probe for retailers that returned no price matches."""
import time, random
from playwright.sync_api import sync_playwright

RETAILERS = [
    ("Scan",       "https://www.scan.co.uk/search?q=GTX+1650"),
    ("Box",        "https://www.box.co.uk/search?q=GTX+1650"),
    ("CCL Online", "https://www.cclonline.com/search/?q=GTX+1650"),
    ("Very",       "https://www.very.co.uk/e/q/GTX-1650.end"),
    ("Argos",      "https://www.argos.co.uk/search/GTX%201650/"),  # wider query
]

def deep_probe(page, name, url):
    print(f"\n{'='*65}")
    print(f"  {name}  —  {url}")
    print('='*65)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(6, 10))

        shot = f"/opt/openclaw/data/stic/probe2_{name.replace(' ','_')}.png"
        page.screenshot(path=shot)
        print(f"  Screenshot: {shot}")

        body_snippet = page.inner_text("body")[:300].replace('\n', ' ')
        print(f"  Body start: {body_snippet}")

        # 1. Dump ALL elements that contain a £ sign and are short
        pound_els = page.evaluate("""() => {
            const results = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const text = el.innerText || '';
                if (text.includes('£') && text.length < 50 && text.length > 2) {
                    const cls = (el.className || '').toString().substring(0, 100);
                    const tag = el.tagName;
                    const id  = (el.id || '').substring(0, 40);
                    results.push({tag, cls, id, text: text.trim().substring(0, 40)});
                    if (results.length >= 20) break;
                }
            }
            return results;
        }""")
        if pound_els:
            print(f"\n  Elements containing £:")
            for e in pound_els:
                ident = f"#{e['id']}" if e['id'] else f".{e['cls'][:60]}"
                print(f"    <{e['tag']}> {ident}")
                print(f"         text: {e['text']!r}")
        else:
            print("  ❌ No £ elements found — page may be JS-rendered or blocked")

        # 2. Check for JSON-LD price data
        json_ld = page.evaluate("""() => {
            const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
            return scripts.map(s => s.textContent.substring(0, 300));
        }""")
        if json_ld:
            print(f"\n  JSON-LD data ({len(json_ld)} blocks):")
            for j in json_ld[:3]:
                if 'price' in j.lower() or 'offer' in j.lower():
                    print(f"    {j[:200]}")

        # 3. Check for React/Next data
        react_data = page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__') || document.getElementById('__NUXT__');
            if (el) return el.textContent.substring(0, 500);
            return null;
        }""")
        if react_data and 'price' in react_data.lower():
            print(f"\n  __NEXT_DATA__ contains price data — React-rendered")
            print(f"  {react_data[:200]}")

    except Exception as e:
        print(f"  ERROR: {e}")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-GB",
        timezone_id="Europe/London",
    )
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
        window.chrome = { runtime: {} };
    """)
    page = context.new_page()
    for name, url in RETAILERS:
        deep_probe(page, name, url)
        time.sleep(random.uniform(4, 8))
    browser.close()
