#!/usr/bin/env python3
"""Probe Box and Very with networkidle wait for JS-rendered prices."""
import time, random
from playwright.sync_api import sync_playwright

def probe_site(page, name, url, wait="networkidle"):
    print(f"\n{'='*65}")
    print(f"  {name}  —  {url}")
    print('='*65)
    try:
        page.goto(url, wait_until=wait, timeout=30000)
        time.sleep(8)

        shot = f"/opt/openclaw/data/stic/probe3_{name.replace(' ','_')}.png"
        page.screenshot(path=shot)
        print(f"  Screenshot: {shot}")

        pound_els = page.evaluate("""() => {
            const results = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const direct = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
                if (direct.includes('£') && direct.length < 60 && direct.trim().length > 2) {
                    const cls = (el.className || '').toString().substring(0, 100);
                    const tag = el.tagName;
                    results.push({tag, cls, text: direct.trim().substring(0, 50)});
                    if (results.length >= 20) break;
                }
            }
            return results;
        }""")

        if pound_els:
            print(f"\n  Direct-text £ elements:")
            for e in pound_els:
                print(f"    <{e['tag']}> class={e['cls']!r}")
                print(f"         text: {e['text']!r}")
        else:
            print("  ❌ Still no £ elements with direct text")
            # Broader sweep
            broad = page.evaluate("""() => {
                const els = [...document.querySelectorAll('*')].filter(e => {
                    const t = (e.innerText || '').trim();
                    return t.includes('£') && t.length < 60;
                });
                return els.slice(0, 15).map(e => ({
                    tag: e.tagName,
                    cls: (e.className||'').toString().substring(0, 100),
                    text: e.innerText.trim().substring(0, 50)
                }));
            }""")
            if broad:
                print(f"  Broader sweep:")
                for e in broad:
                    print(f"    <{e['tag']}> .{e['cls'][:70]}")
                    print(f"         text: {e['text']!r}")

        # JSON-LD
        json_ld = page.evaluate("""() => {
            return [...document.querySelectorAll('script[type="application/ld+json"]')]
                   .map(s => s.textContent.substring(0, 400))
                   .filter(t => t.toLowerCase().includes('price') || t.toLowerCase().includes('offer'));
        }""")
        if json_ld:
            print(f"\n  JSON-LD with price:")
            for j in json_ld[:2]:
                print(f"    {j[:300]}")

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

    probe_site(page, "Box",        "https://www.box.co.uk/search?q=GTX+1650")
    time.sleep(5)
    probe_site(page, "Very",       "https://www.very.co.uk/e/q/GTX-1650.end")
    time.sleep(5)
    probe_site(page, "Overclockers_detail", "https://www.overclockers.co.uk/search?sSearch=GTX+1650")

    browser.close()
