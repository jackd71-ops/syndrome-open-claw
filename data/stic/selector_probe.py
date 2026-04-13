#!/usr/bin/env python3
"""
Probe each retailer's search results page for a known product,
dump price-related element HTML to identify correct selectors.
"""
import time, random, json
from playwright.sync_api import sync_playwright

QUERY   = "ASUS PRIME A520M-K"
QUERY2  = "GTX 1650"           # more mainstream, likely stocked at all retailers

RETAILERS = [
    ("Amazon UK",    "https://www.amazon.co.uk/s?k=ASUS+PRIME+A520M-K"),
    ("Currys",       "https://www.currys.co.uk/search?q=ASUS+PRIME+A520M-K"),
    ("Argos",        "https://www.argos.co.uk/search/ASUS%20PRIME%20A520M-K/"),
    ("Scan",         "https://www.scan.co.uk/search?q=ASUS+PRIME+A520M-K"),
    ("Overclockers", "https://www.overclockers.co.uk/search?sSearch=ASUS+PRIME+A520M-K"),
    ("Box",          "https://www.box.co.uk/search?q=ASUS+PRIME+A520M-K"),
    ("CCL Online",   "https://www.cclonline.com/search/?q=ASUS+PRIME+A520M-K"),
    ("AWD-IT",       "https://www.awd-it.co.uk/catalogsearch/result/?q=ASUS+PRIME+A520M-K"),
    ("Very",         "https://www.very.co.uk/e/q/ASUS-PRIME-A520M-K.end"),
]

# Broad sweep of candidate selectors to probe
PRICE_CANDIDATES = [
    # Generic
    ".price", ".Price", ".product-price", ".productPrice",
    ".our-price", ".ourPrice", ".sale-price",
    # Schema / microdata
    "[itemprop='price']", "[itemprop='offers']",
    # Data attrs
    "[data-price]", "[data-testid*='price']", "[data-cy*='price']",
    # Amazon
    ".a-price", ".a-price .a-offscreen", ".a-price-whole",
    # Currys
    "[class*='Price']", "[class*='price']",
    # Argos
    ".price__cost", "[class*='PriceText']", "[class*='price-text']",
    # Scan
    ".price.scanPricePromotion", ".price.scanPrice",
    # Overclockers
    ".our_price_display", "[class*='product-price']",
    # Box
    "[class*='ProductPrice']", "[class*='product_price']",
    # CCL
    ".regular-price", ".special-price", "[class*='price-box']",
    # AWD-IT
    "[class*='price']", ".price-box",
    # Very
    "[class*='ProductPrice']", "[data-cs-override-id*='price']",
]

def probe(page, name, url):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  {url}")
    print('='*60)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(random.uniform(4, 7))

        # Screenshot
        shot = f"/opt/openclaw/data/stic/probe_{name.replace(' ','_').replace('/','')}.png"
        page.screenshot(path=shot, full_page=False)
        print(f"  Screenshot: {shot}")

        # Check for CF / bot wall
        body = page.inner_text("body")[:200].lower()
        if any(x in body for x in ["just a moment", "checking your browser", "cloudflare", "access denied", "robot"]):
            print(f"  ⚠️  LIKELY BOT WALL — page text: {body[:100]}")

        # Probe each candidate selector
        hits = []
        for sel in PRICE_CANDIDATES:
            try:
                els = page.query_selector_all(sel)
                if els:
                    texts = []
                    for el in els[:3]:
                        t = el.inner_text().strip().replace("\n", " ")[:60]
                        if t:
                            texts.append(t)
                    if texts:
                        hits.append((sel, texts))
            except Exception:
                pass

        if hits:
            print(f"\n  Selector hits:")
            for sel, texts in hits:
                print(f"    {sel!r:50s} → {texts}")
        else:
            print(f"  ❌ No price selectors matched — dumping body classes...")
            # Dump all elements with 'price' in their class
            result = page.evaluate("""() => {
                const els = [...document.querySelectorAll('*')].filter(e => {
                    const c = (e.className || '').toString().toLowerCase();
                    return c.includes('price') && e.innerText && e.innerText.trim().length > 0 && e.innerText.trim().length < 30;
                });
                return els.slice(0, 20).map(e => ({
                    tag: e.tagName,
                    cls: e.className.toString().substring(0, 80),
                    txt: e.innerText.trim().substring(0, 40)
                }));
            }""")
            for r in result:
                print(f"    <{r['tag']}> class={r['cls']!r} text={r['txt']!r}")

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
        probe(page, name, url)
        time.sleep(random.uniform(3, 6))
    browser.close()
