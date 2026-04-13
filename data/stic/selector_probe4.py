#!/usr/bin/env python3
"""Probe CCL and AWD-IT with products they actually stock, plus Amazon title validation."""
import time
from playwright.sync_api import sync_playwright

TESTS = [
    ("CCL_stocked",  "https://www.cclonline.com/search/?q=GTX+1650"),
    ("CCL_mb",       "https://www.cclonline.com/search/?q=MSI+PRO+H610M-G+DDR4"),
    ("AWDIT_stocked","https://www.awd-it.co.uk/catalogsearch/result/?q=ASUS+PRIME+B550M-K"),
    ("Amazon_check", "https://www.amazon.co.uk/s?k=ASUS+PRIME+A520M-K"),
]

def probe(page, name, url):
    print(f"\n{'='*65}\n  {name}\n{'='*65}")
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(6)
    page.screenshot(path=f"/opt/openclaw/data/stic/probe4_{name}.png")

    # Dump first 5 product titles + prices together
    if "amazon" in url:
        items = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('[data-component-type="s-search-result"]')];
            return cards.slice(0, 5).map(c => {
                const title = (c.querySelector('h2 span') || c.querySelector('[data-cy="title-recipe"]'))?.innerText?.trim() || '';
                const price = c.querySelector('.a-price .a-offscreen')?.innerText?.trim() || '';
                return {title: title.substring(0, 80), price};
            });
        }""")
        print("  Amazon result pairs (title + price):")
        for i in items:
            print(f"    {i['price']:10s}  {i['title']}")

    elif "cclonline" in url:
        items = page.evaluate("""() => {
            const els = [...document.querySelectorAll('*')].filter(e => {
                const t = (e.innerText || '').trim();
                return t.includes('£') && t.length < 60;
            });
            return els.slice(0, 10).map(e => ({
                tag: e.tagName,
                cls: (e.className||'').toString().substring(0,80),
                text: e.innerText.trim().substring(0,50)
            }));
        }""")
        print("  CCL £ elements:")
        for e in items:
            print(f"    <{e['tag']}> .{e['cls'][:60]}  →  {e['text']!r}")

    elif "awd-it" in url:
        items = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('.product-item')];
            return cards.slice(0, 5).map(c => {
                const title = c.querySelector('.product-item-link')?.innerText?.trim() || c.querySelector('a')?.innerText?.trim().substring(0,80) || '';
                const price = c.querySelector('.price')?.innerText?.trim() || '';
                return {title: title.substring(0, 80), price};
            });
        }""")
        print("  AWD-IT result pairs:")
        for i in items:
            print(f"    {i['price']:12s}  {i['title']}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-GB", timezone_id="Europe/London",
    )
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = ctx.new_page()
    for name, url in TESTS:
        probe(page, name, url)
        time.sleep(4)
    browser.close()
