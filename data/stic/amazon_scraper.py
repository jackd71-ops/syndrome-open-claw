#!/usr/bin/env python3
"""
Standalone Amazon UK price scraper for OpenClaw.
Runs independently from the main retailer scraper — twice daily (10:00 and 15:00 UK).
Scrapes all active products that have an ASIN in a single pass.

All timing, fingerprinting and scheduling is tunable here without touching
the main retailer scraper.

Usage:
  python3 amazon_scraper.py              # full run (auto-detects am/pm slot from hour)
  python3 amazon_scraper.py --slot am    # force am slot label
  python3 amazon_scraper.py --slot pm    # force pm slot label
  python3 amazon_scraper.py --test       # first 20 ASINs only, no Telegram
"""

import argparse
import fcntl
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright as patchright_playwright

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH       = "/opt/stic-scraper/analytics/prices.db"
LOG_PATH      = "/opt/stic-scraper/logs/amazon.log"
SECRETS_PATH  = "/opt/stic-scraper/secrets.json"
LOCK_PATH     = "/opt/stic-scraper/data/amazon_scraper.lock"
PROGRESS_PATH = "/opt/stic-scraper/data/amazon_progress_{date}_{slot}.json"

# ── Amazon-specific timing ─────────────────────────────────────────────────────
# These are fully independent of DELAY_MIN/MAX in retailer_scraper.py — tune freely.
DELAY_MIN            = 6    # seconds between products (general inter-product gap)
DELAY_MAX            = 14
PAGE_SLEEP_MIN       = 4    # seconds to wait after each Amazon page.goto()
PAGE_SLEEP_MAX       = 7
WARMUP_SLEEP         = 4    # seconds to wait after homepage warm-up visit
LONG_PAUSE_MIN       = 30   # seconds for the periodic longer pause
LONG_PAUSE_MAX       = 90
LONG_PAUSE_EVERY_MIN = 20   # products between long pauses (lower bound)
LONG_PAUSE_EVERY_MAX = 35   # products between long pauses (upper bound)

# ── Fingerprint rotation ───────────────────────────────────────────────────────
# A random UA + viewport is chosen once per run — each twice-daily session looks
# different to Amazon's bot detection.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
    {"width": 1536, "height": 864},
]

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── Secrets ───────────────────────────────────────────────────────────────────
def get_secrets():
    with open(SECRETS_PATH) as f:
        return json.load(f)


# ── Process lock ──────────────────────────────────────────────────────────────
@contextmanager
def scraper_lock():
    """Prevent concurrent amazon_scraper instances from running."""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        yield
    except IOError:
        print("[Lock] Amazon scraper already running — exiting.")
        sys.exit(0)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


# ── Virtual display ───────────────────────────────────────────────────────────
@contextmanager
def virtual_display():
    """Start a temporary Xvfb display for headed patchright sessions."""
    display = f":{random.randint(50, 200)}"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24"],
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


# ── Progress tracking ─────────────────────────────────────────────────────────
def load_progress(date_str, slot):
    """Load completed product IDs for this date+slot (crash-recovery)."""
    path = PROGRESS_PATH.format(date=date_str, slot=slot)
    if Path(path).exists():
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_progress(date_str, slot, completed):
    with open(PROGRESS_PATH.format(date=date_str, slot=slot), "w") as f:
        json.dump(list(completed), f)


# ── Load products with ASINs ──────────────────────────────────────────────────
def load_amazon_products(limit=None):
    """Return all active products that have an ASIN, ordered by product_id."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    q = (
        "SELECT p.product_id, p.description, p.model_no, p.manufacturer, "
        "p.product_group, p.msrp, r.amazon_asin "
        "FROM products p "
        "JOIN retailer_ids r ON p.product_id = r.product_id "
        "WHERE p.eol = 0 AND r.amazon_asin IS NOT NULL AND r.amazon_asin != '' "
        "ORDER BY p.product_id"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q).fetchall()
    con.close()
    return [
        {
            "product_id":    str(row["product_id"]),
            "description":   row["description"] or "",
            "model_no":      row["model_no"] or "",
            "manufacturer":  row["manufacturer"] or "",
            "product_group": row["product_group"],
            "msrp":          row["msrp"],
            "asin":          row["amazon_asin"],
        }
        for row in rows
    ]


# ── Price parser ──────────────────────────────────────────────────────────────
def parse_price(text):
    if not text:
        return None
    text = text.replace(",", "").replace("\xa0", "").strip()
    m = re.search(r'£\s*(\d+\.\d{2})', text)
    if m:
        try:
            v = float(m.group(1))
            return v if v > 0.5 else None
        except ValueError:
            pass
    return None


# ── Amazon scraper ────────────────────────────────────────────────────────────
amazon_warmed_up = False


def warm_up_amazon(page):
    global amazon_warmed_up
    if amazon_warmed_up:
        return
    log("  [Amazon] Warming up cookies via homepage...")
    page.goto("https://www.amazon.co.uk", wait_until="domcontentloaded", timeout=20000)
    time.sleep(WARMUP_SLEEP)
    amazon_warmed_up = True


def scrape_amazon(page, asin):
    """Scrape a single ASIN. Returns (price, seller_type) where seller_type is
    None (Amazon direct), 'FBA' (3rd-party FBA), or 'USED' (used-only listing)."""
    warm_up_amazon(page)
    url = f"https://www.amazon.co.uk/dp/{asin}"
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(random.uniform(PAGE_SLEEP_MIN, PAGE_SLEEP_MAX))

    result = page.evaluate("""() => {
        // ── Used/condition detection ─────────────────────────────────────────
        const conditionSelectors = [
            '#apex_desktop_qualityTierMessage',
            '#apex_desktop_itemInformation',
            '.a-section.a-spacing-none .a-color-secondary',
            '#buyNewSection',
            '#usedBuySection',
            '.olp-used-price',
            '#apex_offerDisplay_desktop .a-color-secondary',
        ];
        const usedPattern = /\\bused\\b|like new|very good|\\bgood\\b|acceptable|refurbished/i;
        for (const sel of conditionSelectors) {
            const el = document.querySelector(sel);
            if (el && usedPattern.test(el.textContent)) {
                return {price: null, seller_type: 'USED'};
            }
        }

        const basketBtn = document.querySelector('#add-to-cart-button, #submit\\.buy-now');
        if (basketBtn && /used/i.test(basketBtn.textContent)) {
            return {price: null, seller_type: 'USED'};
        }

        // ── "See all buying options" detection ───────────────────────────────
        // When Amazon itself isn't selling, the page shows a "See all buying options"
        // button instead of "Add to Cart". The corePrice div still shows the best
        // marketplace offer, which we must NOT report as Amazon direct.
        const seeAllBtn = document.querySelector(
            '#buybox-see-all-buying-choices, #buybox-see-all-buying-choices-announce, ' +
            'a[href*="offer-listing"], #olpLinkWidget_feature_div a, ' +
            '.a-box-group #buyNewSection a'
        );
        const addToCartBtn = document.querySelector('#add-to-cart-button, #submit\\.buy-now');
        // If there's a "see all" link but no Add-to-Cart, Amazon isn't the seller
        if (seeAllBtn && !addToCartBtn) {
            return {price: null, seller_type: 'FBA'};
        }

        // ── Seller type detection (FBA vs Amazon direct) ─────────────────────
        // Amazon direct = explicitly "sold by amazon.co.uk"
        // Default to FBA (not null) when seller is unknown — prevents misclassifying
        // marketplace offers as Amazon direct.
        let seller_type = 'FBA';   // assume marketplace unless confirmed Amazon direct
        const merchantSelectors = [
            '#merchant-info',
            '#tabular-buybox-truncate-0',
            '#sellerProfileTriggerId',
            '#SSOFpopoverLink',
            '.tabular-buybox-text',
        ];
        for (const sel of merchantSelectors) {
            const el = document.querySelector(sel);
            if (el) {
                const text = el.textContent.toLowerCase();
                if (text.includes('sold by amazon.co.uk') ||
                    (text.includes('sold by amazon') &&
                     !text.includes('sold by amazon eu') &&
                     !text.includes('sold by amazon warehouse'))) {
                    seller_type = null;   // confirmed Amazon direct
                }
                break;
            }
        }
        // If no merchant element found at all and no Add-to-Cart button, no direct offer
        if (seller_type === 'FBA' && !addToCartBtn) {
            return {price: null, seller_type: 'FBA'};
        }

        // ── Price extraction — buybox only ───────────────────────────────────
        // Only use specific buybox selectors. The broad '.a-price .a-offscreen'
        // fallback is intentionally omitted — it matches prices in carousels,
        // recommendations and comparison widgets, not the actual offer price.
        const priceSelectors = [
            '#corePrice_feature_div .a-price .a-offscreen',
            '#apex_offerDisplay_desktop .a-price .a-offscreen',
            '#buybox .a-price .a-offscreen',
        ];
        for (const sel of priceSelectors) {
            const el = document.querySelector(sel);
            if (el && el.textContent.trim()) {
                return {price: el.textContent.trim(), seller_type: seller_type};
            }
        }
        return {price: null, seller_type: null};
    }""")

    if not result or not isinstance(result, dict):
        return None, None

    seller_type = result.get('seller_type')
    if seller_type == 'USED':
        return None, 'USED'

    raw_price = result.get('price')
    if raw_price:
        p = parse_price(raw_price)
        if p:
            return p, seller_type

    return None, None


# ── DB write ──────────────────────────────────────────────────────────────────
def write_to_db(date_str, product, price, seller_type, in_stock):
    """Write a single Amazon price result to retailer_prices."""
    try:
        d, m, y    = date_str.split("-")
        iso_date   = f"{y}-{m}-{d}"
        product_id = int(product["product_id"])
        msrp       = product.get("msrp")
        below_msrp = None
        if price is not None and msrp is not None:
            # Flag only if price is more than 3% below MSRP — minor discounting is normal
            below_msrp = 1 if price < msrp * 0.97 else 0

        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """INSERT OR IGNORE INTO retailer_prices
               (date, product_id, model_no, description, manufacturer,
                product_group, msrp, retailer, price, below_msrp, in_stock, seller_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iso_date, product_id, product["model_no"], product["description"],
             product["manufacturer"], product["product_group"], msrp,
             "Amazon", price, below_msrp, in_stock, seller_type),
        )
        db.commit()
        db.close()
    except Exception as e:
        log(f"  DB write error (non-fatal): {e}")


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    token = get_secrets().get("TELEGRAM_TOKEN", "")
    if not token:
        return
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": "1163684840", "text": message, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        log("Telegram sent.")
    except Exception as e:
        log(f"Telegram error: {e}")


# ── Main run ──────────────────────────────────────────────────────────────────
def run(date_str, slot, test_mode=False):
    limit    = 20 if test_mode else None
    products = load_amazon_products(limit=limit)
    completed = load_progress(date_str, slot)

    log(f"\n{'='*65}")
    log(f"AMAZON SCRAPER — {date_str} [{slot.upper()}]{'  [TEST MODE]' if test_mode else ''}")
    log(f"Products with ASIN: {len(products)} | Already done this slot: {len(completed)}")
    log(f"{'='*65}\n")

    # Pick fingerprint for this session
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    log(f"Session fingerprint: {vp['width']}×{vp['height']} | UA: ...{ua[40:80]}...")

    found         = 0
    fba           = 0
    oos           = 0
    used          = 0
    errors        = 0
    products_done = 0
    since_pause   = 0
    long_pause_every = random.randint(LONG_PAUSE_EVERY_MIN, LONG_PAUSE_EVERY_MAX)

    with virtual_display():
        with patchright_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                viewport=vp,
                user_agent=ua,
                locale="en-GB",
                timezone_id="Europe/London",
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "DNT": "1",
                }
            )
            page = context.new_page()

            for product in products:
                pid   = product["product_id"]
                model = product["model_no"]
                asin  = product["asin"]
                msrp  = product.get("msrp")

                if pid in completed:
                    log(f"Skipping {pid} ({model}) — already done this slot.")
                    continue

                msrp_str = f"£{msrp:.2f}" if msrp else "no MSRP"
                log(f"\n[{pid}] {model} | ASIN: {asin} ({msrp_str})")

                page.mouse.move(random.randint(200, 1100), random.randint(150, 600))

                try:
                    price, seller_type = scrape_amazon(page, asin)
                except Exception as e:
                    log(f"    [Amazon] ERROR: {e}")
                    price, seller_type = None, None
                    errors += 1

                if seller_type == 'USED':
                    log(f"    [Amazon] used only")
                    write_to_db(date_str, product, None, 'USED', 0)
                    used += 1
                elif price is not None:
                    fba_note = " [FBA]" if seller_type == 'FBA' else ""
                    below    = " ⚠️ BELOW MSRP" if msrp and price < msrp * 0.97 else ""
                    log(f"    [Amazon] £{price:.2f}{fba_note}{below}")
                    write_to_db(date_str, product, price, seller_type, 1)
                    found += 1
                    if seller_type == 'FBA':
                        fba += 1
                else:
                    log(f"    [Amazon] OOS / no price")
                    write_to_db(date_str, product, None, None, 0)
                    oos += 1

                products_done += 1
                completed.add(pid)
                save_progress(date_str, slot, completed)

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

                since_pause += 1
                if since_pause >= long_pause_every:
                    pause = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
                    log(f"Long pause: {pause:.0f}s")
                    time.sleep(pause)
                    since_pause = 0
                    long_pause_every = random.randint(LONG_PAUSE_EVERY_MIN, LONG_PAUSE_EVERY_MAX)

            browser.close()

    log(
        f"\nAmazon [{slot.upper()}] complete — Products={products_done}, "
        f"Found={found} (FBA={fba}), OOS={oos}, Used={used}, Errors={errors}"
    )

    if not test_mode:
        send_telegram(
            f"🛒 <b>Amazon Scraper — {slot.upper()}</b>  {date_str}\n\n"
            f"✔️ Scraped: {products_done}\n"
            f"💰 Price found: {found}  ({fba} FBA / {found - fba} direct)\n"
            f"📭 OOS / no price: {oos}\n"
            f"🔄 Used-only: {used}"
        )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone Amazon UK price scraper")
    parser.add_argument(
        "--slot", choices=["am", "pm"],
        help="Run slot label for progress tracking (default: auto-detect from hour)"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Scrape first 20 ASINs only — no Telegram notification"
    )
    args = parser.parse_args()

    date_str = datetime.now().strftime("%d-%m-%Y")
    slot     = args.slot or ("am" if datetime.now().hour < 13 else "pm")

    with scraper_lock():
        run(date_str, slot, test_mode=args.test)
