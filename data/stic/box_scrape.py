#!/usr/bin/env python3
"""
Standalone Box price scraper using patchright (headed via Xvfb).
Called as subprocess by retailer_scraper.py.
Usage: xvfb-run --auto-servernum python3 box_scrape.py <url>
Prints: price as float, or NOT_FOUND

Box uses Angular SSR — price loads client-side ~8s after domcontentloaded.
Price element: <p class="text-[24px] md:text-[30px]...">£XXX.XX</p>
"""
import sys, re, time
from patchright.sync_api import sync_playwright

def parse_price(text):
    if not text:
        return None
    m = re.search(r'£\s*([\d,]+\.?\d*)', text.replace(',', ''))
    if m:
        try:
            v = float(m.group(1).replace(',', ''))
            return v if v > 0.5 else None
        except ValueError:
            pass
    return None

if len(sys.argv) < 2:
    print("NOT_FOUND")
    sys.exit(0)

url = sys.argv[1]

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(
            viewport={'width': 1366, 'height': 768},
            locale='en-GB', timezone_id='Europe/London',
        )
        page = context.new_page()

        # Warm up via homepage for cookies/fingerprint trust
        page.goto('https://box.co.uk', wait_until='domcontentloaded', timeout=30000)
        time.sleep(3)

        # Dismiss cookie banner via JS (OneTrust overlay blocks clicks)
        try:
            page.evaluate("""() => {
                const b = [...document.querySelectorAll('button')]
                    .find(b => /allow all|accept all/i.test(b.textContent));
                if (b) b.click();
            }""")
            time.sleep(1)
        except Exception:
            pass

        # Navigate to product page, wait for Angular hydration
        resp = page.goto(url, wait_until='networkidle', timeout=40000)
        if resp and resp.status == 404:
            print("NOT_FOUND")
            browser.close()
            sys.exit(0)

        time.sleep(8)  # Angular client-side price hydration delay

        # Extract price via text node walker — price is in a <p> with text-[24px] class
        result = page.evaluate("""() => {
            // OOS check: no Add to Basket button, or "currently unavailable" text
            const btns = [...document.querySelectorAll('button')];
            const hasAddToBasket = btns.some(b => /add to basket|add to cart/i.test(b.textContent));
            const isUnavailable  = document.body.innerText.includes('currently unavailable');
            if (!hasAddToBasket || isUnavailable) {
                return {oos: true, price: null};
            }

            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                const t = node.textContent.trim();
                if (/^£\\s*[1-9][\\d,]*\\.\\d{2}$/.test(t) && node.parentElement) {
                    const cls = (node.parentElement.className || '').toString();
                    if (cls.includes('text-[24px]') || cls.includes('text-[30px]')) {
                        return {oos: false, price: t};
                    }
                }
            }
            // Fallback: first exact £X.XX text node anywhere
            const walker2 = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (node = walker2.nextNode()) {
                const t = node.textContent.trim();
                if (/^£\\s*[1-9][\\d,]*\\.\\d{2}$/.test(t)) {
                    return {oos: false, price: t};
                }
            }
            return {oos: false, price: null};
        }""")

        if result and result.get('oos'):
            print("NOT_FOUND")
        else:
            price = parse_price(result.get('price')) if result else None
            print(price if price is not None else "NOT_FOUND")
        browser.close()

except Exception as e:
    print("NOT_FOUND", file=sys.stderr)
    print(str(e), file=sys.stderr)
    sys.exit(1)
