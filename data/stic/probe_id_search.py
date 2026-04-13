#!/usr/bin/env python3
"""Test searching by product ID (ASIN / Currys SKU) instead of model number."""
import time, re
from playwright.sync_api import sync_playwright

TESTS = [
    # Amazon: search by ASIN → should return exactly 1 result
    ("Amazon_ASIN",   "https://www.amazon.co.uk/s?k=B0BXFBN121"),
    ("Amazon_ASIN2",  "https://www.amazon.co.uk/s?k=B08HHMCCXV"),
    # Currys: search by numeric SKU → should return exactly 1 result
    ("Currys_SKU",    "https://www.currys.co.uk/search?q=690644"),
    ("Currys_SKU2",   "https://www.currys.co.uk/search?q=581323"),
]

def probe(page, name, url):
    print(f"\n{'='*65}\n  {name}\n  {url}\n{'='*65}")
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(5)
    print(f"  Final URL: {page.url}")

    page.screenshot(path=f"/opt/openclaw/data/stic/probe_idsearch_{name}.png")

    # For Amazon: get title + price pairs
    if "amazon" in url:
        pairs = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('[data-asin]:not([data-asin=""])')];
            return cards.slice(0, 3).map(c => ({
                asin:  c.getAttribute('data-asin'),
                title: (c.querySelector('h2 span') || c.querySelector('[data-cy="title-recipe"] span'))
                           ?.innerText?.trim()?.substring(0,80) || '',
                price: c.querySelector('.a-price .a-offscreen')?.innerText?.trim() || '',
                sponsored: !!c.querySelector('.s-sponsored-label-info-icon')
            }));
        }""")
        for p in pairs:
            print(f"  ASIN={p['asin']} sponsored={p['sponsored']}")
            print(f"    title: {p['title']}")
            print(f"    price: {p['price']}")

    # For Currys: get title + price
    elif "currys" in url:
        pairs = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('article, li.product, [data-testid="product-card"]')];
            if (!cards.length) {
                // fallback
                return [{
                    title: document.querySelector('h1, h2, h3')?.innerText?.trim()?.substring(0,80) || '',
                    price: document.querySelector('.product-price, [class*="Price"]')?.innerText?.trim()?.substring(0,30) || ''
                }];
            }
            return cards.slice(0,3).map(c => ({
                title: c.querySelector('h3,h2,[data-testid="product-name"]')?.innerText?.trim()?.substring(0,80) || '',
                price: c.querySelector('.product-price,[class*="Price"]')?.innerText?.trim()?.substring(0,30) || ''
            }));
        }""")
        for p in pairs:
            print(f"  title: {p['title']}")
            print(f"  price: {p['price']}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True,
        args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width":1366,"height":768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="en-GB", timezone_id="Europe/London",
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
