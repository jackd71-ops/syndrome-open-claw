#!/usr/bin/env python3
"""
Quick Currys test: patchright + Xvfb headed mode (same as live scraper).
Tests a small sample with SKU search only to verify Cloudflare bypass.
"""
import sqlite3, time, random, json, re, sys, os, subprocess
from datetime import datetime
from contextlib import contextmanager

DB_PATH      = "/opt/openclaw/data/analytics/prices.db"
RESULTS_PATH = "/opt/openclaw/logs/currys_batch_test.json"
LOG_PATH     = "/opt/openclaw/logs/currys_batch_test.log"
CURRYS_SEARCH = "https://www.currys.co.uk/search?q={query}&prefixSearch=false"
SAMPLE_SIZE  = int(sys.argv[1]) if len(sys.argv) > 1 else 10

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

@contextmanager
def virtual_display():
    display = f":{random.randint(50, 200)}"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1366x768x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    os.environ["DISPLAY"] = display
    time.sleep(1)
    try:
        yield display
    finally:
        proc.terminate()
        proc.wait()
        os.environ.pop("DISPLAY", None)

def get_sample():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT r.product_id, r.currys_sku, p.ean, p.model_no, p.manufacturer
        FROM retailer_ids r
        JOIN products p ON r.product_id = p.product_id
        WHERE r.currys_sku IS NOT NULL AND r.currys_sku != ''
          AND p.eol = 0
          AND p.ean IS NOT NULL AND p.ean != ''
        ORDER BY RANDOM()
        LIMIT ?
    """, (SAMPLE_SIZE,)).fetchall()
    con.close()
    return rows

def extract_price(html):
    m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', html)
    if m: return m.group(1)
    m = re.search(r'£\s*([\d,]+\.?\d{0,2})', html)
    if m: return m.group(1).replace(",", "")
    return None

def classify(html):
    lower = html.lower()
    if "you have been blocked" in lower or "checking your browser" in lower or "cf-error" in lower:
        return "BLOCKED"
    if len(html) < 5000:
        return "BLOCKED"
    if "no results found" in lower or "we can't find a match" in lower or "0 results" in lower:
        return "NO_RESULTS"
    price = extract_price(html)
    if price:
        return f"PRICE:{price}"
    return f"PAGE_OK_NO_PRICE({len(html)}b)"

def run():
    from patchright.sync_api import sync_playwright as patchright_playwright

    log(f"Loading {SAMPLE_SIZE} products from DB...")
    products = get_sample()
    log(f"Got {len(products)} products — using patchright + Xvfb headed mode")

    results = []

    with virtual_display() as disp:
        log(f"Xvfb started on {disp}")
        with patchright_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="en-GB",
                timezone_id="Europe/London",
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "DNT": "1",
                }
            )
            page = context.new_page()

            for i, (product_id, currys_sku, ean, model_no, manufacturer) in enumerate(products, 1):
                log(f"[{i}/{len(products)}] {manufacturer} {model_no} | SKU={currys_sku} EAN={ean}")

                # SKU search
                delay = random.uniform(5, 10)
                log(f"  SKU (delay {delay:.1f}s)...")
                time.sleep(delay)
                try:
                    page.goto(CURRYS_SEARCH.format(query=currys_sku), wait_until="domcontentloaded", timeout=25000)
                    time.sleep(random.uniform(5, 9))
                    sku_html = page.content()
                    sku_result = classify(sku_html)
                except Exception as e:
                    sku_result = f"ERROR:{str(e)[:60]}"
                log(f"  SKU -> {sku_result}")

                # EAN search
                delay = random.uniform(5, 10)
                log(f"  EAN (delay {delay:.1f}s)...")
                time.sleep(delay)
                try:
                    page.goto(CURRYS_SEARCH.format(query=ean), wait_until="domcontentloaded", timeout=25000)
                    time.sleep(random.uniform(5, 9))
                    ean_html = page.content()
                    ean_result = classify(ean_html)
                except Exception as e:
                    ean_result = f"ERROR:{str(e)[:60]}"
                log(f"  EAN -> {ean_result}")

                results.append({
                    "product_id": product_id, "model_no": model_no,
                    "manufacturer": manufacturer, "currys_sku": currys_sku, "ean": ean,
                    "sku_result": sku_result, "ean_result": ean_result,
                })
                with open(RESULTS_PATH, "w") as f:
                    json.dump(results, f, indent=2)

            browser.close()

    # Summary
    n = len(results)
    sku_prices = [r for r in results if r["sku_result"].startswith("PRICE:")]
    ean_prices = [r for r in results if r["ean_result"].startswith("PRICE:")]
    sku_blocked = sum(1 for r in results if "BLOCKED" in r["sku_result"])
    ean_blocked = sum(1 for r in results if "BLOCKED" in r["ean_result"])
    sku_nores   = sum(1 for r in results if "NO_RESULTS" in r["sku_result"])
    ean_nores   = sum(1 for r in results if "NO_RESULTS" in r["ean_result"])

    log("")
    log("=" * 60)
    log(f"RESULTS ({n} products tested — patchright + Xvfb headed)")
    log("=" * 60)
    log(f"{'Outcome':<24} {'SKU':>6} {'EAN':>6}")
    log(f"{'-'*38}")
    log(f"{'Price found':<24} {len(sku_prices):>6} {len(ean_prices):>6}")
    log(f"{'Blocked':<24} {sku_blocked:>6} {ean_blocked:>6}")
    log(f"{'No results (listed)':<24} {sku_nores:>6} {ean_nores:>6}")
    log(f"{'-'*38}")
    log(f"{'Hit rate':<24} {len(sku_prices)/n*100:>5.1f}% {len(ean_prices)/n*100:>5.1f}%")
    if sku_prices or ean_prices:
        log("")
        log("Prices found:")
        for r in results:
            if r["sku_result"].startswith("PRICE:") or r["ean_result"].startswith("PRICE:"):
                log(f"  {r['manufacturer']} {r['model_no']} ({r['currys_sku']}): SKU={r['sku_result']} EAN={r['ean_result']}")
    log(f"Full results: {RESULTS_PATH}")

if __name__ == "__main__":
    run()
