#!/usr/bin/env python3
"""Test whether CCL and AWD-IT search can auto-discover product URLs."""
import time
from playwright.sync_api import sync_playwright

TESTS = [
    ("CCL_model",   "https://www.cclonline.com/search/?q=H610M+K+DDR4"),
    ("CCL_exact",   "https://www.cclonline.com/search/?q=GIGABYTE+H610M+K+DDR4"),
    ("AWDIT_model", "https://www.awd-it.co.uk/catalogsearch/result/?q=H610M+K+DDR4"),
    ("AWDIT_exact", "https://www.awd-it.co.uk/catalogsearch/result/?q=GIGABYTE+H610M+K+DDR4"),
]

def probe(page, name, url):
    print(f"\n{'='*65}\n  {name}\n  {url}\n{'='*65}")
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(5)
    print(f"  Final URL: {page.url[:80]}")
    page.screenshot(path=f"/opt/openclaw/data/stic/probe_disc_{name}.png")

    if "ccl" in url:
        results = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('a[href*="/"]')].filter(a => {
                const h = a.href || '';
                return h.includes('cclonline.com') && !h.includes('/search') &&
                       !h.includes('/category') && a.innerText && a.innerText.trim().length > 5;
            });
            return cards.slice(0, 5).map(a => ({
                href:  a.href,
                text:  a.innerText.trim().substring(0, 80)
            }));
        }""")
        print(f"  Product links found: {len(results)}")
        for r in results:
            print(f"    {r['text'][:60]}")
            print(f"    → {r['href'][:80]}")

    elif "awd-it" in url:
        results = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('.product-item-info, .product-item')];
            return cards.slice(0, 5).map(c => ({
                title: (c.querySelector('.product-item-link, a')?.innerText || '').trim().substring(0, 80),
                href:  c.querySelector('.product-item-link, a')?.href || '',
            }));
        }""")
        print(f"  Product cards found: {len(results)}")
        for r in results:
            print(f"    {r['title'][:60]}")
            print(f"    → {r['href'][:80]}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True,
        args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width":1366,"height":768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-GB", timezone_id="Europe/London",
    )
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = ctx.new_page()
    for name, url in TESTS:
        probe(page, name, url)
        time.sleep(3)
    browser.close()
