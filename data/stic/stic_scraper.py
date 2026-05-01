#!/usr/bin/env python3
"""
STIC (Stock In The Channel) price scraper for OpenClaw.
Searches each product by model number, scrapes distributor prices/stock,
writes results directly to SQLite DB (no Excel output).

Usage:
  python3 stic_scraper.py --batch 1   # products 1-260 (9:30am run)
  python3 stic_scraper.py --batch 2   # products 261-520 (1:00pm run)
  python3 stic_scraper.py --test      # first 20 products only
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Paths ────────────────────────────────────────────────────────────────────
CACHE_PATH    = "/opt/stic-scraper/data/url_cache.json"
PROGRESS_PATH = "/opt/stic-scraper/data/progress_{date}.json"
SESSION_PATH  = "/opt/stic-scraper/data/session.json"
LOG_PATH      = "/opt/stic-scraper/logs/stic.log"

# ── STIC config ───────────────────────────────────────────────────────────────
STIC_BASE     = "https://www.stockinthechannel.co.uk"
STIC_LOGIN    = "https://www.stockinthechannel.co.uk/Account/Login"
STIC_SEARCH   = "https://www.stockinthechannel.co.uk/Search?q={query}"

# Credentials — stored in secrets.json
SECRETS_PATH  = "/opt/stic-scraper/secrets.json"

# OneDrive destination (rclone remote path)
ONEDRIVE_DEST = "onedrive:Documents/STIC"

# Telegram
TELEGRAM_CHAT_ID = "1163684840"

# ── Target distributors (must match column headers in template exactly) ───────
DISTRIBUTORS = [
    "TD Synnex UK",
    "VIP",
    "Westcoast",
    "Target",
    "M2M Direct",
    "Ingram Micro",
]

# Partial match aliases — STIC may show slightly different names
DISTRIBUTOR_ALIASES = {
    "TD Synnex UK":  ["td synnex", "tdsynnex", "synnex"],
    "VIP":           ["vip", "vip computers", "vip distribution"],
    "Westcoast":     ["westcoast", "west coast"],
    "Target":        ["target", "target components"],
    "M2M Direct":    ["m2m", "m2m direct"],
    "Ingram Micro":  ["ingram micro", "ingram", "ingram micro uk"],
}

# ── Timing ────────────────────────────────────────────────────────────────────
DELAY_MIN         = 4
DELAY_MAX         = 12
LONG_PAUSE_EVERY  = random.randint(30, 50)   # products between long pauses
LONG_PAUSE_MIN    = 20
LONG_PAUSE_MAX    = 60

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── Secrets ───────────────────────────────────────────────────────────────────
def get_credentials():
    with open(SECRETS_PATH) as f:
        secrets = json.load(f)
    return secrets.get("STIC_USERNAME"), secrets.get("STIC_PASSWORD")

# ── URL cache ─────────────────────────────────────────────────────────────────
def load_cache() -> dict:
    if Path(CACHE_PATH).exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}

def save_cache(cache: dict):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

# ── Progress tracking ─────────────────────────────────────────────────────────
def load_progress(date_str: str) -> set:
    path = PROGRESS_PATH.format(date=date_str)
    if Path(path).exists():
        with open(path) as f:
            return set(json.load(f))
    return set()

def save_progress(date_str: str, completed: set):
    path = PROGRESS_PATH.format(date=date_str)
    with open(path, "w") as f:
        json.dump(list(completed), f)

# ── DB helpers ───────────────────────────────────────────────────────────────
def _db_products_conn():
    import sqlite3
    db = sqlite3.connect(_DB_PATH, timeout=30)   # wait up to 30s if DB locked by another scraper
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")         # WAL: concurrent reads never block writers
    return db

def _db_write_with_retry(fn, retries=5, base_delay=0.5):
    """Call fn() which performs a DB write.  Retries on OperationalError (locked)
    with exponential back-off.  Raises after retries are exhausted."""
    import sqlite3, time, random
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == retries - 1:
                raise
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.3)
            log(f"  DB locked (attempt {attempt+1}/{retries}) — retrying in {wait:.1f}s")
            time.sleep(wait)

def get_stic_url(product_id) -> str | None:
    """Return the cached STIC product detail URL for this product, or None."""
    db = _db_products_conn()
    row = db.execute("SELECT stic_url FROM products WHERE product_id=?", (int(product_id),)).fetchone()
    db.close()
    return row["stic_url"] if row and row["stic_url"] else None

def save_stic_url(product_id, url: str):
    """Persist a confirmed STIC product detail URL to the products table."""
    def _write():
        db = _db_products_conn()
        db.execute("UPDATE products SET stic_url=? WHERE product_id=?", (url, int(product_id)))
        db.commit()
        db.close()
    _db_write_with_retry(_write)

def clear_stic_url(product_id):
    """Remove a confirmed-bad STIC URL so the product appears in Missing Results."""
    def _write():
        db = _db_products_conn()
        db.execute("UPDATE products SET stic_url=NULL WHERE product_id=?", (int(product_id),))
        db.commit()
        db.close()
    _db_write_with_retry(_write)

def count_products() -> int:
    """Count active (non-EOL) products in the products DB table."""
    db = _db_products_conn()
    row = db.execute("SELECT COUNT(*) AS c FROM products WHERE eol=0 AND stic_exclude=0").fetchone()
    db.close()
    return row["c"] if row else 0

def batch_ranges(total: int) -> list[tuple[int, int]]:
    """Split total products into 3 roughly equal ranges."""
    size = total // 3
    remainder = total % 3
    ranges = []
    start = 1
    for i in range(3):
        extra = 1 if i < remainder else 0
        end = start + size + extra - 1
        ranges.append((start, end))
        start = end + 1
    return ranges

def _row_to_product(row) -> dict:
    return {
        "product_id":    row["product_id"],
        "description":   row["description"] or "",
        "model_no":      row["model_no"] or "",
        "manufacturer":  row["manufacturer"] or "",
        "product_group": row["product_group"] or None,
        "chipset":       row["chipset"] or None,
        "ean":           row["ean"] or None,
    }

def read_products(start: int, end: int) -> list:
    """Read active products from DB, ordered by product_id. Row numbers are 1-based."""
    db = _db_products_conn()
    rows = db.execute("SELECT * FROM products WHERE eol=0 AND stic_exclude=0 ORDER BY product_id").fetchall()
    db.close()
    products = []
    for row_num, row in enumerate(rows, start=1):
        if row_num < start:
            continue
        if row_num > end:
            break
        p = _row_to_product(row)
        p["row_num"] = row_num
        products.append(p)
    return products

def read_products_for_group(manufacturer: str | None, product_group: str) -> list:
    """Read active products for a specific manufacturer + product_group combination.
    If manufacturer is None, returns all manufacturers for that product_group."""
    db = _db_products_conn()
    if manufacturer:
        rows = db.execute(
            "SELECT * FROM products WHERE eol=0 AND stic_exclude=0 AND manufacturer=? AND product_group=? ORDER BY product_id",
            (manufacturer, product_group)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM products WHERE eol=0 AND stic_exclude=0 AND product_group=? ORDER BY product_id",
            (product_group,)
        ).fetchall()
    db.close()
    return [_row_to_product(row) for row in rows]


# ── Group configuration ───────────────────────────────────────────────────────
# Each entry: (manufacturer_or_None, product_group, human_label)
# Order determines scrape sequence — GPUs first (smaller, faster), boards after.
SCRAPE_GROUPS = [
    ("PALIT",      "PROD_VIDEO", "Palit GPU"),
    ("POWERCOLOR", "PROD_VIDEO", "PowerColor GPU"),
    ("MSI",        "PROD_VIDEO", "MSI GPU"),
    ("ASUS",       "PROD_VIDEO", "ASUS GPU"),
    ("GIGABYTE",   "PROD_VIDEO", "Gigabyte GPU"),
    ("MSI",        "PROD_MBRD",  "MSI Motherboards"),
    ("GIGABYTE",   "PROD_MBRD",  "Gigabyte Motherboards"),
    ("ASUS",       "PROD_MBRD",  "ASUS Motherboards"),
    (None,         "PROD_MBRDS", "Server / Pro"),
    ("AMD Retail", "PROD_CPU",   "AMD Retail CPU"),
    ("AMD MPK",    "PROD_CPU",   "AMD MPK CPU"),
    ("Intel",      "PROD_CPU",   "Intel CPU"),
    ("Intel OEM",  "PROD_CPU",   "Intel OEM CPU"),
    (None,         "PROBE",      "Probe SKUs"),
]

GPU_GROUPS       = [g for g in SCRAPE_GROUPS if g[1] == "PROD_VIDEO"]
CPU_AMD_GROUPS   = [g for g in SCRAPE_GROUPS if g[1] == "PROD_CPU" and g[0] and "amd"   in g[0].lower()]
CPU_INTEL_GROUPS = [g for g in SCRAPE_GROUPS if g[1] == "PROD_CPU" and g[0] and "intel" in g[0].lower()]

# Gap between groups: random 2–5 minutes
GROUP_GAP_MIN = 120
GROUP_GAP_MAX = 300

# ── Data bleed detection ─────────────────────────────────────────────────────
def check_data_bleed(iso_date: str) -> list[dict]:
    """
    Find SKU pairs where 3+ distributors share identical non-null price+qty on
    the same date — strong signal that one SKU picked up another's scraped data.
    Returns list of dicts: {product_id, model_no, matched_to, matched_model, matching_rows}
    """
    import sqlite3
    try:
        db = sqlite3.connect(_DB_PATH)
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT a.product_id, a.model_no, b.product_id AS matched_to,
                   b.model_no AS matched_model, COUNT(*) AS matching_rows
            FROM stic_prices a
            JOIN stic_prices b
              ON b.date=a.date AND b.distributor=a.distributor
             AND b.product_id != a.product_id
             AND a.price IS NOT NULL AND b.price IS NOT NULL
             AND a.qty   IS NOT NULL AND b.qty   IS NOT NULL
             AND a.price = b.price   AND a.qty   = b.qty
            WHERE a.date=? AND a.product_id < b.product_id
            GROUP BY a.product_id, b.product_id
            HAVING matching_rows >= 3
            ORDER BY matching_rows DESC, a.product_id
        """, (iso_date,)).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log(f"  Data bleed check error: {e}")
        return []


# ── Write result to SQLite ────────────────────────────────────────────────────
_DB_PATH = "/opt/stic-scraper/analytics/prices.db"

_DIST_DB_NAME = {
    "TD Synnex UK": "TD Synnex",
    "VIP":          "VIP",
    "Westcoast":    "Westcoast",
    "Target":       "Target",
    "M2M Direct":   "M2M Direct",
    "Ingram Micro": "Ingram Micro",
}

def write_to_db(date_str: str, product: dict, distributor_data: dict, force: bool = False):
    """Write scraped distributor prices to SQLite. Never raises — logs on failure.
    If force=True, existing rows for this product+date are deleted first so the
    afternoon re-scrape overwrites morning data with fresh prices/stock."""
    import sqlite3
    # Convert DD-MM-YYYY to YYYY-MM-DD for DB consistency
    d, m, y = date_str.split("-")
    iso_date = f"{y}-{m}-{d}"

    product_id    = product["product_id"]
    model_no      = product["model_no"]
    manufacturer  = product["manufacturer"]
    product_group = product.get("product_group")
    chipset       = product.get("chipset")

    def _write():
        db = sqlite3.connect(_DB_PATH, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        if force:
            db.execute(
                "DELETE FROM stic_prices WHERE date=? AND product_id=?",
                (iso_date, product_id)
            )
        for dist_name in _DIST_DB_NAME:
            db_dist = _DIST_DB_NAME[dist_name]
            price, qty = distributor_data.get(dist_name, (None, None))
            db.execute(
                """INSERT OR IGNORE INTO stic_prices
                   (date, product_id, model_no, manufacturer, product_group,
                    chipset, distributor, price, qty)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (iso_date, product_id, model_no, manufacturer, product_group,
                 chipset, db_dist, price, qty),
            )
        db.commit()
        db.close()

    try:
        _db_write_with_retry(_write)
    except Exception as e:
        log(f"  DB write error (non-fatal): {e}")

# ── Browser: login ────────────────────────────────────────────────────────────
def login(page, username: str, password: str) -> bool:
    log("Logging into STIC...")
    page.goto(STIC_LOGIN, wait_until="domcontentloaded")
    time.sleep(random.uniform(2, 4))

    try:
        # Fill username
        page.wait_for_selector('input[type="email"]', timeout=10000)
        page.fill('input[type="email"]', username)
        time.sleep(random.uniform(0.8, 1.5))

        # Fill password
        page.fill('input[type="password"]', password)
        time.sleep(random.uniform(0.8, 1.5))

        # Screenshot pre-submit for debug
        page.screenshot(path="/opt/stic-scraper/data/pre_submit.png")

        # Submit by pressing Enter on password field — more reliable than button click
        page.press('input[type="password"]', "Enter")
        time.sleep(8)  # wait for redirect
        page.wait_for_load_state("domcontentloaded")
        time.sleep(random.uniform(2, 3))

        # Dump URL and key page indicators for diagnosis
        current_url = page.url
        content = page.content().lower()
        log(f"Post-login URL: {current_url}")
        log(f"Page has 'logout': {'logout' in content}")
        log(f"Page has 'sign out': {'sign out' in content}")
        log(f"Page has 'my account': {'my account' in content}")
        log(f"Page has 'register': {'register' in content}")

        # Save screenshot for inspection
        page.screenshot(path="/opt/stic-scraper/data/login_debug.png")
        log("Screenshot saved to /opt/stic-scraper/data/login_debug.png")

        if "logout" in content or "sign out" in content or "my account" in content:
            log("Login successful.")
            return True
        else:
            log(f"Login FAILED — still seeing guest content.")
            return False
    except Exception as e:
        log(f"Login error: {e}")
        return False

# ── Browser: search and scrape in one step ───────────────────────────────────
def search_and_scrape(page, model_no: str, cache: dict, product_id: str = None, manufacturer: str = "", ean: str = None) -> dict | None:
    """
    Search STIC by VIP SKU (product_id, e.g. 122408), validate by checking
    the model name (model_no, e.g. "MPG B650I EDGE WIFI") appears in the result card.

    Fallback: search by EAN, same model-name validation.
    Last resort: product detail page (existing behaviour).

    Returns dict: { "Distributor Name": (price_float_or_none, qty_int_or_none) }
    Returns None on page load failure, {} if no matching distributors found.
    """

    def _scrape_table_from_card(page_obj, search_query, log_label, trust_single=False):
        """
        Navigate to STIC search for search_query, find the card whose text contains
        model_no (case-insensitive), scrape its distributor table.
        Returns (match_type_str, raw_rows_list, product_url_or_none).
        match_type is 'model' or 'none'.  product_url is the detail page href from the card.
        trust_single=True: if exactly one product card exists and the manufacturer
        (stripped) appears in it, accept without requiring model-name match. Used for
        VIP-ID searches where OPN model numbers won't appear in STIC card text.
        """
        url = STIC_SEARCH.format(query=str(search_query).replace(" ", "+"))
        log(f"  Searching ({log_label}): {search_query}")
        page_obj.goto(url, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))
        page_obj.mouse.wheel(0, random.randint(100, 400))
        time.sleep(random.uniform(0.5, 1.5))

        log(f"  {log_label}: landed on {page_obj.url}")

        # STIC sometimes redirects single-result searches straight to the product
        # detail page.  The card-based scraper won't work there — detect and handle.
        if "/Product/" in page_obj.url:
            product_url = page_obj.url
            log(f"  {log_label}: redirected to product page — scraping directly: {product_url}")
            direct_rows = page_obj.evaluate("""
                () => {
                    const data = [];
                    const tables = document.querySelectorAll('table');
                    let targetTable = null;
                    for (const t of tables) {
                        const headerRow = t.querySelector('tr');
                        if (headerRow) {
                            const text = headerRow.innerText.toLowerCase();
                            if (text.includes('distributor') || text.includes('stock') || text.includes('price')) {
                                targetTable = t;
                                break;
                            }
                        }
                    }
                    if (!targetTable) return data;
                    targetTable.querySelectorAll('tr').forEach((row, idx) => {
                        if (idx === 0) return;
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 4) return;
                        let distName = cells[0].innerText.trim();
                        if (!distName) {
                            const img = cells[0].querySelector('img');
                            distName = img ? (img.alt || img.title || '') : '';
                        }
                        const priceText = cells[cells.length - 1].innerText.trim();
                        const stockText = cells.length >= 6
                            ? cells[3].innerText.trim()
                            : cells[cells.length - 2].innerText.trim();
                        if (distName) data.push({
                            distributor: distName,
                            stock: stockText,
                            price: priceText,
                            allCells: Array.from(cells).map(c => c.innerText.trim())
                        });
                    });
                    return data;
                }
            """)
            if direct_rows:
                return "model", direct_rows, product_url
            log(f"  {log_label}: product page had no distributor table.")
            return "none", [], product_url

        content = page_obj.content()
        if "0 Results Found" in content or "No results" in content.lower():
            log(f"  No results on STIC for: {search_query}")
            return "none", [], None

        # Strip variant suffixes so STIC card text "AMD" matches our "AMD MPK"
        _mfr_for_js = (manufacturer or "").replace(" MPK","").replace(" OEM","").replace(" Retail","").strip()
        res = page_obj.evaluate("""
            ([modelNo, mfr, trustSingle]) => {
                const modelLower = modelNo.toLowerCase();
                const mfrLower   = mfr ? mfr.toLowerCase() : null;
                const tables = document.querySelectorAll('table');
                if (!tables.length) return { match_type: 'none', rows: [] };

                // Check whether a card element contains our model name.
                // A line matches if it includes our model as a substring, BUT we reject
                // lines that contain variant markers (wifi/wi-fi) that our model doesn't
                // have — prevents "B650-PLUS" matching a "B650-PLUS WIFI" card, while
                // allowing "AM5 PROART X870E-CREATOR WIFI" to match "PROART X870E-CREATOR WIFI".
                // VARIANT_MARKERS: only 'wifi' needed — normaliseStr converts
                // 'wi-fi' and 'wi fi' to 'wifi' before any comparison.
                const VARIANT_MARKERS = ['wifi'];

                // ── normaliseStr ─────────────────────────────────────────────────────
                // 1. hyphens → spaces  (ROG-STRIX-B550-F-GAMING-WI-FI → ROG STRIX B550 F GAMING WI FI)
                // 2. wifi: "wi fi" / "wi-fi" (already spaces by step 1) → "wifi"
                //    so "WI-FI II" and "WIFI II" both normalise to "wifi ii"
                // 3. GPU model numbers: "RTX5070TI" and "RTX 5070 Ti" both → "rtx 5070 ti"
                // NOTE: no \\b word boundaries — Python converts \b → backspace inside
                // triple-quoted strings; omitting them is safe for our data.
                function normaliseStr(s) {
                    let n = s.replace(/-/g, ' ');
                    n = n.replace(/wi\\s*fi/gi, 'wifi');
                    n = n.replace(/(rtx|gtx|rx)\\s*(\\d{3,4})\\s*(ti|xtx|xt)?/gi,
                        (_, brand, num, suffix) => suffix
                            ? brand.toLowerCase() + ' ' + num + ' ' + suffix.toLowerCase()
                            : brand.toLowerCase() + ' ' + num);
                    return n.toLowerCase();
                }
                const modelNorm = normaliseStr(modelLower);
                const modelHasVariant = new Set(VARIANT_MARKERS.filter(v => modelNorm.includes(v)));

                // ── text-based line match ─────────────────────────────────────────────
                function lineMatchesModel(line) {
                    const lineNorm = normaliseStr(line);
                    if (!lineNorm.includes(modelNorm)) return false;
                    for (const marker of VARIANT_MARKERS) {
                        if (lineNorm.includes(marker) && !modelHasVariant.has(marker)) return false;
                    }
                    return true;
                }
                function cardTextMatchesModel(el) {
                    const cardText = (el.innerText || '').toLowerCase();
                    if (mfrLower && !cardText.includes(mfrLower)) return false;
                    const lines = cardText.split('\\n').map(l => l.trim());
                    return lines.some(l => lineMatchesModel(l));
                }

                // ── URL-slug match (fallback) ─────────────────────────────────────────
                // STIC product URLs contain /Product/PRIME-RTX5070TI-O16G/ — the exact
                // model-name slug from our DB.  normaliseStr handles both hyphenated and
                // space-separated variants, making this more reliable than text matching
                // when the DOM is too deep for text to reach the product-name element.
                function urlSlugMatchesModel(href) {
                    const m = href.match(/\\/Product\\/([^\\/\\?#]+)/i);
                    if (!m) return false;
                    const slugNorm = normaliseStr(m[1]);
                    return slugNorm === modelNorm
                        || slugNorm.includes(modelNorm)
                        || modelNorm.includes(slugNorm);
                }
                function cardUrlMatchesModel(el) {
                    // Manufacturer guard — cheap fast check first
                    if (mfrLower && !(el.innerText || '').toLowerCase().includes(mfrLower)) return false;
                    const links = el.querySelectorAll('a[href*="/Product/"]');
                    for (const link of links) {
                        if (urlSlugMatchesModel(link.href)) return true;
                    }
                    return false;
                }

                // ── main search loop (text match OR URL-slug match) ───────────────────
                for (const table of tables) {
                    let el = table.parentElement;
                    for (let i = 0; i < 20; i++) {   // increased from 12 → 20
                        if (!el) break;
                        const matched = cardTextMatchesModel(el) || cardUrlMatchesModel(el);
                        if (matched) {
                            const rows = [];
                            table.querySelectorAll('tr').forEach((row, idx) => {
                                if (idx === 0) return;
                                const cells = row.querySelectorAll('td');
                                if (cells.length < 2) return;
                                const distName = cells[0].innerText.trim();
                                const stockText = cells.length >= 3 ? cells[cells.length - 2].innerText.trim() : '';
                                const priceText = cells[cells.length - 1].innerText.trim();
                                if (distName) rows.push({
                                    distributor: distName,
                                    stock: stockText,
                                    price: priceText,
                                    allCells: Array.from(cells).map(c => c.innerText.trim())
                                });
                            });
                            if (rows.length) {
                                let productUrl = null;
                                const link = el.querySelector('a[href*="/Product/"]');
                                if (link) productUrl = link.href;
                                return { match_type: 'model', rows, productUrl };
                            }
                        }
                        el = el.parentElement;
                    }
                }

                // ── trustSingle fallback ─────────────────────────────────────────────
                // When searching by VIP product ID, OPN model numbers (100-000000xxx)
                // won't appear in STIC card text.  If there is exactly ONE product card
                // on the page and the manufacturer (stripped) is present in it, accept it.
                // This prevents false negatives on unambiguous ID-based searches.
                if (trustSingle) {
                    // Collect all distinct product cards (ancestor elements with a table)
                    const candidateCards = [];
                    for (const table of tables) {
                        let el = table.parentElement;
                        for (let i = 0; i < 20 && el; i++) {
                            const hasProductLink = el.querySelector('a[href*="/Product/"]');
                            if (hasProductLink) {
                                candidateCards.push({ el, table });
                                break;
                            }
                            el = el.parentElement;
                        }
                    }
                    if (candidateCards.length === 1) {
                        const { el, table } = candidateCards[0];
                        const cardText = (el.innerText || '').toLowerCase();
                        const mfrOk = !mfrLower || cardText.includes(mfrLower);
                        if (mfrOk) {
                            const rows = [];
                            table.querySelectorAll('tr').forEach((row, idx) => {
                                if (idx === 0) return;
                                const cells = row.querySelectorAll('td');
                                if (cells.length < 2) return;
                                const distName = cells[0].innerText.trim();
                                const stockText = cells.length >= 3 ? cells[cells.length - 2].innerText.trim() : '';
                                const priceText = cells[cells.length - 1].innerText.trim();
                                if (distName) rows.push({
                                    distributor: distName,
                                    stock: stockText,
                                    price: priceText,
                                    allCells: Array.from(cells).map(c => c.innerText.trim())
                                });
                            });
                            if (rows.length) {
                                const link = el.querySelector('a[href*="/Product/"]');
                                return { match_type: 'single_card', rows, productUrl: link ? link.href : null };
                            }
                        }
                    }
                }

                // ── no card matched — collect diagnostics ─────────────────────────────
                const debugTexts = [];
                const nearbyUrls = [];
                for (const table of tables) {
                    let el = table.parentElement;
                    // Grab text of the first non-trivial ancestor (for debug)
                    for (let i = 0; i < 8; i++) {
                        if (!el) break;
                        const t = (el.innerText || '').trim();
                        if (t.length > 10) {
                            debugTexts.push(t.substring(0, 150).replace(/\\n/g, ' | '));
                            break;
                        }
                        el = el.parentElement;
                    }
                    // Also search for any product URL within 20 levels of this table
                    let el2 = table.parentElement;
                    for (let i = 0; i < 20 && el2; i++) {
                        const link = el2.querySelector('a[href*="/Product/"]');
                        if (link) { nearbyUrls.push(link.href); break; }
                        el2 = el2.parentElement;
                    }
                    if (debugTexts.length >= 3) break;
                }
                return { match_type: 'none', rows: [], productUrl: null, debug: debugTexts, nearbyUrls };
            }
        """, [model_no, _mfr_for_js, trust_single])

        mt = res.get("match_type", "none") if isinstance(res, dict) else "none"
        # Treat single_card as a successful model match
        if mt == "single_card":
            log(f"  {log_label}: single-card trust match (OPN/tray model — manufacturer validated).")
            mt = "model"
        rr = res.get("rows", []) if isinstance(res, dict) else []
        pu = res.get("productUrl") if isinstance(res, dict) else None
        nearby_urls = []
        if mt == "none":
            debug = res.get("debug", []) if isinstance(res, dict) else []
            nearby_urls = list(dict.fromkeys(res.get("nearbyUrls", []))) if isinstance(res, dict) else []
            if debug:
                log(f"  {log_label}: no card matched — sample card texts:")
                for d in debug[:3]:
                    log(f"    · {d}")
            if nearby_urls:
                log(f"  {log_label}: product URLs found near tables: {nearby_urls[:3]}")
        return mt, rr, pu, nearby_urls

    try:
        # Load cached STIC URL for this product (if we've successfully found it before)
        cached_url = get_stic_url(product_id) if product_id else None
        found_url  = None   # URL confirmed this run — saved to DB on success

        def _check_url(card_url, step_label):
            """Compare card URL to cached URL. Logs result. Returns True if URL matches cache."""
            nonlocal found_url
            if not card_url:
                return False
            if cached_url:
                if card_url == cached_url:
                    log(f"  URL match confirmed ({step_label}): {card_url}")
                    found_url = card_url
                    return True
                else:
                    log(f"  URL mismatch ({step_label}): expected {cached_url} got {card_url} — running full validation")
                    found_url = card_url   # update cache on success later
                    return False
            else:
                found_url = card_url   # first time seen — save on success
                return False

        # ── Step 0: If a trusted URL is saved, navigate directly to it ───────
        # This runs BEFORE any search so a manually-corrected URL is always
        # honoured and can never be overwritten by a bad search result.
        result = []
        if cached_url:
            log(f"  Cached URL found — navigating directly: {cached_url}")
            try:
                page.goto(cached_url, wait_until="domcontentloaded")
                time.sleep(random.uniform(2, 3))
                direct_result = page.evaluate("""
                    () => {
                        const data = [];
                        const tables = document.querySelectorAll('table');
                        let targetTable = null;
                        for (const t of tables) {
                            const headerRow = t.querySelector('tr');
                            if (headerRow) {
                                const text = headerRow.innerText.toLowerCase();
                                if (text.includes('distributor') || text.includes('stock') || text.includes('price')) {
                                    targetTable = t;
                                    break;
                                }
                            }
                        }
                        if (!targetTable) return data;
                        const rows = targetTable.querySelectorAll('tr');
                        rows.forEach((row, idx) => {
                            if (idx === 0) return;
                            const cells = row.querySelectorAll('td');
                            if (cells.length < 4) return;
                            let distName = cells[0].innerText.trim();
                            if (!distName) {
                                const img = cells[0].querySelector('img');
                                distName = img ? (img.alt || img.title || '') : '';
                            }
                            // 6+ col layout: Distributor|Product|SKU|Stock|...|Price
                            // 4-col layout:  Distributor|Product|Stock|Price
                            // Price is always the LAST cell regardless of column count.
                            // Stock is cells[3] for 6+ col, cells[cells.length-2] for 4-col.
                            let stockText, priceText;
                            priceText = cells[cells.length - 1].innerText.trim();
                            if (cells.length >= 6) {
                                stockText = cells[3].innerText.trim();
                            } else {
                                stockText = cells[cells.length - 2].innerText.trim();
                            }
                            if (distName) data.push({
                                distributor: distName,
                                stock: stockText,
                                price: priceText,
                                allCells: Array.from(cells).map(c => c.innerText.trim())
                            });
                        });
                        return data;
                    }
                """)
                if direct_result:
                    page_text = page.inner_text("body").lower()
                    brand_ok  = (not manufacturer) or (manufacturer.lower() in page_text)
                    model_ok  = (not model_no)     or (model_no.lower() in page_text)
                    # Also accept if our VIP product_id appears on the page —
                    # STIC shows our SKU number in the distributor table so this
                    # confirms we're on the right product even when the name differs.
                    pid_ok    = bool(product_id) and (str(product_id) in page_text)

                    if brand_ok and model_ok:
                        log(f"  Direct URL scrape succeeded (brand + model validated).")
                        result = direct_result
                        found_url = cached_url
                    elif brand_ok and pid_ok:
                        log(f"  Direct URL scrape succeeded (brand + VIP product_id {product_id} found on page).")
                        result = direct_result
                        found_url = cached_url
                    elif brand_ok and not model_ok:
                        # Brand matches but model name doesn't — the cached URL was
                        # manually set or previously confirmed by the user, so trust
                        # it even if the name on STIC's page differs from our DB.
                        log(f"  Direct URL scrape succeeded (brand validated; model name '{model_no}' not found on page but URL is trusted).")
                        result = direct_result
                        found_url = cached_url
                    else:
                        failed = []
                        if not brand_ok: failed.append(f"brand '{manufacturer}'")
                        log(f"  VALIDATION FAILED on cached URL ({', '.join(failed)} not on page) — falling through to search.")
                        # NOTE: we intentionally do NOT clear the URL here.
                        # If the SKU was scraping fine before, the URL is evidence of
                        # what was being used — preserving it lets the user see the old
                        # URL in Missing Results and investigate what changed on STIC.
                        # Only the user should clear/correct a URL manually.
                else:
                    log(f"  Cached URL returned no table — falling through to search.")
            except Exception as e:
                log(f"  Error navigating to cached URL: {e} — falling through to search.")

        # ── Steps 1–3: search-based fallback (only if Step 0 didn't succeed) ──
        match_type = "none"
        raw_rows   = []
        card_url   = None

        if not result:
            # ── Step 1: search by manufacturer + VIP SKU, validate by model name ─
            # Prepending the manufacturer name narrows STIC's results to the right
            # brand so the card-matcher finds the correct product rather than an
            # unrelated page that happens to contain the SKU number.
            if not product_id:
                log(f"  WARNING: no product_id — cannot search by VIP SKU, skipping.")
                return {}

            # Strip tray/variant suffixes so STIC search sees e.g. "AMD 124197"
            # not "AMD MPK 124197" (STIC doesn't index those terms).
            _mfr_brand = (manufacturer or "").replace(" MPK","").replace(" OEM","").replace(" Retail","").strip()
            _is_opn_mfr = any(t in (manufacturer or "") for t in (" MPK", " OEM"))

            # For OPN-style manufacturers (MPK/OEM), try the model_no (OPN) as the
            # primary search — STIC indexes Threadripper PRO and enterprise tray CPUs
            # by their part number.  Consumer Ryzen tray falls back to VIP ID.
            if _is_opn_mfr and model_no:
                match_type, raw_rows, card_url, _ = _scrape_table_from_card(page, model_no, "OPN", trust_single=True)
                if match_type == "model":
                    log(f"  OPN search matched on model name '{model_no}'.")

            # VIP ID search — primary for all others, fallback for OPN manufacturers
            if match_type == "none":
                vip_query = f"{_mfr_brand} {product_id}" if _mfr_brand else product_id
                match_type, raw_rows, card_url, _ = _scrape_table_from_card(page, vip_query, "VIP SKU", trust_single=True)
                if match_type == "model":
                    url_confirmed = _check_url(card_url, "VIP SKU")
                    if url_confirmed:
                        log(f"  VIP SKU search: URL sanity check passed — skipping model validation.")
                    else:
                        log(f"  VIP SKU search matched on model name '{model_no}'.")

            # ── Step 2: EAN fallback — search by EAN, same model-name validation
            ean_attempted = False
            ean_nearby_urls = []
            if match_type == "none" and ean:
                log(f"  VIP SKU search failed — trying EAN fallback: {ean}")
                ean_attempted = True
                match_type, raw_rows, card_url, ean_nearby_urls = _scrape_table_from_card(page, ean, "EAN")
            if ean_attempted:
                if match_type == "model":
                    url_confirmed = _check_url(card_url, "EAN")
                    if url_confirmed:
                        log(f"  EAN fallback: URL sanity check passed — skipping model validation.")
                    else:
                        log(f"  EAN fallback matched on model name '{model_no}'.")
                else:
                    log(f"  EAN fallback: model '{model_no}' not found in results.")

            # ── Step 2b: EAN found a product URL but model name didn't match ────
            # EAN codes are globally unique — if the EAN search surfaced exactly
            # one distinct product URL, navigate directly to it and trust it.
            # This handles products whose model_no in our DB is a technical code
            # (e.g. AMD OPN "100-000001595") that doesn't appear in STIC's
            # marketing name ("Ryzen 9 7950X").
            if match_type == "none" and ean_attempted and len(ean_nearby_urls) == 1:
                ean_product_url = ean_nearby_urls[0]
                log(f"  EAN found unique product URL — navigating directly: {ean_product_url}")
                try:
                    page.goto(ean_product_url, wait_until="domcontentloaded")
                    time.sleep(random.uniform(2, 3))
                    direct_result = page.evaluate("""
                        () => {
                            const data = [];
                            const tables = document.querySelectorAll('table');
                            let targetTable = null;
                            for (const t of tables) {
                                const headerRow = t.querySelector('tr');
                                if (headerRow) {
                                    const text = headerRow.innerText.toLowerCase();
                                    if (text.includes('distributor') || text.includes('stock') || text.includes('price')) {
                                        targetTable = t; break;
                                    }
                                }
                            }
                            if (!targetTable) return [];
                            const rows = [];
                            targetTable.querySelectorAll('tr').forEach((row, idx) => {
                                if (idx === 0) return;
                                const cells = row.querySelectorAll('td');
                                if (cells.length < 4) return;
                                let distName = cells[0].innerText.trim();
                                if (!distName) {
                                    const img = cells[0].querySelector('img');
                                    distName = img ? (img.alt || img.title || '') : '';
                                }
                                const priceText = cells[cells.length - 1].innerText.trim();
                                const stockText = cells.length >= 6
                                    ? cells[3].innerText.trim()
                                    : cells[cells.length - 2].innerText.trim();
                                if (distName) rows.push({ distributor: distName, stock: stockText, price: priceText,
                                    allCells: Array.from(cells).map(c => c.innerText.trim()) });
                            });
                            return rows;
                        }
                    """)
                    if direct_result:
                        page_text = page.inner_text("body").lower()
                        brand_ok = (not manufacturer) or any(
                            m.lower() in page_text
                            for m in manufacturer.replace(" Retail","").replace(" MPK","").replace(" OEM","").split()
                        )
                        if brand_ok:
                            log(f"  EAN direct URL scrape succeeded — brand validated.")
                            raw_rows = direct_result
                            match_type = "model"
                            found_url = ean_product_url
                        else:
                            log(f"  EAN direct URL: brand '{manufacturer}' not found on page — skipping.")
                    else:
                        log(f"  EAN direct URL: no distributor table found.")
                except Exception as e:
                    log(f"  EAN direct URL error: {e}")

            result = raw_rows  # only assign from search if Step 0 didn't already succeed

        # Step 3 removed: a broad manufacturer+model-name search that followed the first
        # result link was too inaccurate — STIC sidebars can show the correct manufacturer
        # on entirely wrong product pages, causing false-positive validation and bad URL
        # caching.  If Steps 1 and 2 both fail the SKU is logged as not found so it
        # appears in the Missing Results page for manual URL correction.

        if not result:
            return {}

        # Save the confirmed STIC URL so future runs use it directly (Step 0).
        # IMPORTANT: only save if no URL was already cached — Steps 1/2 search results
        # must never overwrite a previously confirmed or manually-set URL.
        # If the cached URL is wrong, the user corrects it manually via the portal.
        # Safety: never cache a "-Box" STIC URL against an MPK/OEM tray manufacturer.
        _is_tray_mfr = manufacturer and any(t in manufacturer for t in (" MPK", " OEM"))
        if found_url and _is_tray_mfr and "/Box" in found_url:
            log(f"  SAFETY: refusing to cache Box URL '{found_url}' for tray manufacturer '{manufacturer}'.")
            found_url = None
        if found_url and product_id and not cached_url:
            save_stic_url(product_id, found_url)

        cache[model_no] = str(product_id)   # mark as "ever found" for run() failure logging

        # Parse into { dist_name: (price, qty) }
        parsed = {}
        for row in result:
            dist_raw = row.get("distributor", "").lower().strip()
            if not dist_raw:
                continue

            # Match to our target distributors
            matched_dist = None
            for dist_name, aliases in DISTRIBUTOR_ALIASES.items():
                if any(alias in dist_raw for alias in aliases):
                    matched_dist = dist_name
                    break

            if not matched_dist:
                log(f"  UNMATCHED distributor (not in our list): '{dist_raw}'")
                continue

            all_cells = row.get("allCells", [])
            log(f"  Matched {matched_dist}: cells={all_cells}")

            # Stock — second to last cell
            stock_text = row.get("stock", "").replace(",", "").strip()
            qty = None
            try:
                qty = int(stock_text)
            except (ValueError, TypeError):
                qty = 0 if stock_text in ("0", "", "-", "n/a") else None

            # Price — last cell, expect "£12.34"
            price_text = row.get("price", "").replace("£", "").replace(",", "").strip()
            price = None
            try:
                parsed_price = float(price_text)
                price = parsed_price if parsed_price > 0 else None
            except (ValueError, TypeError):
                price = None

            # If distributor already seen, keep the entry with highest qty (and its price)
            if matched_dist in parsed:
                existing_price, existing_qty = parsed[matched_dist]
                if (qty or 0) > (existing_qty or 0):
                    parsed[matched_dist] = (price, qty)
                # else keep existing — it has higher stock
            else:
                parsed[matched_dist] = (price, qty)
            log(f"  {matched_dist}: price={parsed[matched_dist][0]}, qty={parsed[matched_dist][1]}")

        return parsed

    except PlaywrightTimeout:
        log(f"  Timeout searching for: {model_no}")
        return None
    except Exception as e:
        log(f"  Search error for {model_no}: {e}")
        return None

# ── OneDrive sync ────────────────────────────────────────────────────────────
def sync_template_from_onedrive() -> bool:
    """No-op: scraper now reads products from DB (populated nightly by sync_template.py)."""
    log("Products read from DB — no OneDrive sync needed.")
    return True


# ── Telegram notification ─────────────────────────────────────────────────────
def send_telegram(message: str):
    import requests as req
    with open(SECRETS_PATH) as f:
        token = json.load(f).get("TELEGRAM_TOKEN")
    if not token:
        log("No TELEGRAM_TOKEN in secrets — skipping notification.")
        return
    try:
        resp = req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code == 200:
            log("Telegram notification sent.")
        else:
            log(f"Telegram error: {resp.status_code} {resp.text}")
    except Exception as e:
        log(f"Telegram exception: {e}")

# ── Human-like mouse movement ─────────────────────────────────────────────────
def random_mouse_move(page):
    """Move mouse to a random position to appear human."""
    x = random.randint(200, 1200)
    y = random.randint(200, 700)
    page.mouse.move(x, y)

# ── Re-scrape specific product IDs ───────────────────────────────────────────
def run_specific(product_ids: list, date_str: str):
    """
    Re-scrape a specific list of product IDs.
    Clears today's DB rows for those IDs first, then re-runs with EAN fallback active.
    Used to resolve data-bleed suspects flagged in the Telegram nightly report.
    """
    import sqlite3 as _sqlite3

    username, password = get_credentials()
    if not username or not password:
        log("ERROR: STIC_USERNAME or STIC_PASSWORD not found in secrets.json")
        sys.exit(1)

    # Convert DD-MM-YYYY to YYYY-MM-DD
    d, m, y = date_str.split("-")
    iso_date = f"{y}-{m}-{d}"

    # Delete today's entries for these products so fresh data is written
    db = _sqlite3.connect(_DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    placeholders = ",".join("?" for _ in product_ids)
    deleted = db.execute(
        f"DELETE FROM stic_prices WHERE date=? AND product_id IN ({placeholders})",
        [iso_date] + [int(p) for p in product_ids]
    ).rowcount
    db.commit()
    db.close()
    log(f"Cleared {deleted} existing DB rows for {len(product_ids)} products.")

    sync_template_from_onedrive()
    all_products = read_products(1, 99999)
    pid_set = {int(p) for p in product_ids}
    products = [p for p in all_products if int(p["product_id"]) in pid_set]

    if not products:
        log("None of the requested product IDs found in the template.")
        return

    log(f"Re-scraping {len(products)} products: {[p['product_id'] for p in products]}")

    # Remove from today's progress so they are not skipped
    completed = load_progress(date_str)
    for p in products:
        completed.discard(str(p["row_num"]))
    save_progress(date_str, completed)

    cache = load_cache()

    with sync_playwright() as p_ctx:
        browser = p_ctx.chromium.launch(
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
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()

        if not login(page, username, password):
            log("Login failed — aborting.")
            browser.close()
            sys.exit(1)

        products_since_pause = 0
        long_pause_every = random.randint(20, 30)   # tighter than main run — smaller groups

        for product in products:
            model_no     = product["model_no"]
            prod_id      = product["product_id"]
            manufacturer = product["manufacturer"]
            ean          = product.get("ean")

            log(f"\n[RESCRAPE] {model_no} (ID: {prod_id}, EAN: {ean or 'none'})")
            random_mouse_move(page)

            dist_data = search_and_scrape(
                page, model_no, cache,
                product_id=str(prod_id),
                manufacturer=manufacturer,
                ean=ean
            )

            if dist_data is None:
                log(f"  Page load failed — skipping.")
            elif not dist_data:
                log(f"  FAILED — no result found for: {model_no}")
            else:
                write_to_db(date_str, product, dist_data)
                completed.add(str(product["row_num"]))
                save_progress(date_str, completed)
                save_cache(cache)

            products_since_pause += 1
            if products_since_pause >= long_pause_every:
                pause = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
                log(f"  Long pause: {pause:.0f}s")
                time.sleep(pause)
                products_since_pause = 0
                long_pause_every = random.randint(20, 30)
            else:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                log(f"  Delay: {delay:.1f}s")
                time.sleep(delay)

        browser.close()

    # Post-run bleed check to confirm fixes
    remaining = check_data_bleed(iso_date)
    log(f"\nRe-scrape complete. Post-run bleed check: {len(remaining)} suspect pairs remaining.")
    for s in remaining:
        log(f"  • {s['product_id']} {s['model_no']} ↔ {s['matched_to']} {s['matched_model']} ({s['matching_rows']} distis)")


# ── Main run ──────────────────────────────────────────────────────────────────
def run(start: int, end: int, date_str: str, is_final: bool = False):
    username, password = get_credentials()
    if not username or not password:
        log("ERROR: STIC_USERNAME or STIC_PASSWORD not found in secrets.json")
        sys.exit(1)

    cache = load_cache()
    completed = load_progress(date_str)

    sync_template_from_onedrive()

    log(f"Starting STIC scrape: products {start}–{end}, date={date_str}")
    log(f"URL cache: {len(cache)} entries. Already completed today: {len(completed)}")

    products = read_products(start, end)
    log(f"Loaded {len(products)} products for this batch.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-GB",
            timezone_id="Europe/London",
        )

        # Add stealth script to mask automation fingerprints
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()

        # Login
        if not login(page, username, password):
            log("Login failed — aborting.")
            browser.close()
            sys.exit(1)

        products_since_pause = 0
        long_pause_every = random.randint(30, 50)

        for product in products:
            row_num      = product["row_num"]
            model_no     = product["model_no"]
            prod_id      = product["product_id"]
            manufacturer = product["manufacturer"]

            # Skip if already done today
            if str(row_num) in completed:
                log(f"Skipping {model_no} (row {row_num}) — already done today.")
                continue

            log(f"\n[{row_num}/{end}] {model_no} (ID: {prod_id})")

            # Random mouse movement
            random_mouse_move(page)

            # Search and scrape distributor prices from results page
            dist_data = search_and_scrape(page, model_no, cache, product_id=str(prod_id), manufacturer=manufacturer, ean=product.get("ean"))

            if dist_data is None:
                log(f"  Page load failed — skipping (will retry next run).")
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            if not dist_data and model_no not in cache:
                log(f"  FAILED MATCH — no result found for: {model_no}")
                completed.add(str(row_num))
                save_progress(date_str, completed)
                save_cache(cache)
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            # Write to DB
            write_to_db(date_str, product, dist_data)

            completed.add(str(row_num))
            save_progress(date_str, completed)
            save_cache(cache)  # Save cache incrementally

            products_since_pause += 1

            # Long pause every 30-50 products
            if products_since_pause >= long_pause_every:
                pause = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
                log(f"  Long pause: {pause:.0f}s")
                time.sleep(pause)
                products_since_pause = 0
                long_pause_every = random.randint(30, 50)  # reset
            else:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                log(f"  Delay: {delay:.1f}s")
                time.sleep(delay)

        browser.close()

    failed = [p["model_no"] for p in products if str(p["row_num"]) not in completed]
    log(f"\nBatch complete. {len(completed)} products done today. {len(failed)} skipped/failed.")

    # Telegram notification — only on final batch
    if is_final:
        total_today = len(load_progress(date_str))
        status_icon = "✅"

        # Convert DD-MM-YYYY to YYYY-MM-DD for DB query
        d, m, y = date_str.split("-")
        iso_date = f"{y}-{m}-{d}"
        bleed_suspects = check_data_bleed(iso_date)
        log(f"Data bleed check: {len(bleed_suspects)} suspect pairs found.")

        bleed_section = ""
        if bleed_suspects:
            lines = [f"⚠️ <b>Possible data bleed ({len(bleed_suspects)} pairs) — please verify:</b>"]
            for s in bleed_suspects:
                lines.append(
                    f"  • {s['product_id']} {s['model_no']} ↔ {s['matched_to']} {s['matched_model']}"
                    f" ({s['matching_rows']} distis match)"
                )
            bleed_section = "\n" + "\n".join(lines)
        else:
            bleed_section = "\n✅ No data bleed suspects found."

        msg = (
            f"{status_icon} <b>STIC scrape complete</b> — {date_str}\n\n"
            f"✔️ Total scraped today: {total_today} products\n"
            f"❌ This batch failed/skipped: {len(failed)}\n"
            f"🕐 Finished: {datetime.now().strftime('%H:%M')}"
            f"{bleed_section}"
        )
        send_telegram(msg)

# ── Group-based run ───────────────────────────────────────────────────────────
def run_groups(groups: list, date_str: str, random_start_delay: bool = True, force: bool = False):
    """
    Open one browser session and scrape each group in sequence.
    Sends a Telegram notification after each group.
    Random 2–5 min gap between groups.
    If force=True, ignore the completed-today progress file and overwrite existing DB rows.
    Optional random 0–10 min start delay to vary the daily start time.
    """
    if random_start_delay:
        delay = random.uniform(0, 600)
        log(f"Random start delay: {delay:.0f}s ({delay/60:.1f} min)")
        time.sleep(delay)

    username, password = get_credentials()
    if not username or not password:
        log("ERROR: STIC_USERNAME or STIC_PASSWORD not found in secrets.json")
        sys.exit(1)

    # Convert date_str (DD-MM-YYYY) to ISO for DB queries
    d, m, y = date_str.split("-")
    iso_date = f"{y}-{m}-{d}"

    cache     = load_cache()
    completed = set() if force else load_progress(date_str)   # force ignores prior progress

    log(f"run_groups: {len(groups)} groups to scrape, date={date_str}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
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
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()

        if not login(page, username, password):
            log("Login failed — aborting.")
            browser.close()
            sys.exit(1)

        for group_idx, (manufacturer, product_group, label) in enumerate(groups):
            is_last = (group_idx == len(groups) - 1)

            products = read_products_for_group(manufacturer, product_group)
            if not products:
                log(f"\n[GROUP] {label} — no active products, skipping.")
                continue

            log(f"\n{'='*60}")
            log(f"[GROUP {group_idx+1}/{len(groups)}] {label} — {len(products)} products")
            log(f"{'='*60}")

            group_done    = 0
            group_failed  = 0
            products_since_pause = 0
            long_pause_every     = random.randint(30, 50)

            for product in products:
                prod_id      = str(product["product_id"])
                model_no     = product["model_no"]
                manufacturer_p = product["manufacturer"]

                # Skip if already done today
                if prod_id in completed:
                    log(f"  Skipping {model_no} (ID: {prod_id}) — already done today.")
                    continue

                log(f"\n  [{group_done+group_failed+1}/{len(products)}] {model_no} (ID: {prod_id})")
                random_mouse_move(page)

                dist_data = search_and_scrape(
                    page, model_no, cache,
                    product_id=prod_id,
                    manufacturer=manufacturer_p,
                    ean=product.get("ean")
                )

                if dist_data is None:
                    log(f"  Page load failed — skipping.")
                    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                    continue

                if not dist_data:
                    log(f"  FAILED — no result found for: {model_no}")
                    group_failed += 1
                else:
                    write_to_db(date_str, product, dist_data, force=force)
                    group_done += 1

                completed.add(prod_id)
                save_progress(date_str, completed)
                save_cache(cache)

                products_since_pause += 1
                if products_since_pause >= long_pause_every:
                    pause = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
                    log(f"  Long pause: {pause:.0f}s")
                    time.sleep(pause)
                    products_since_pause = 0
                    long_pause_every = random.randint(30, 50)
                else:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    log(f"  Delay: {delay:.1f}s")
                    time.sleep(delay)

            # ── Per-group Telegram ────────────────────────────────────────────
            bleed_section = ""
            if is_last:
                bleeds = check_data_bleed(iso_date)
                if bleeds:
                    lines = [f"\n⚠️ <b>Data bleed suspects ({len(bleeds)} pairs):</b>"]
                    for s in bleeds:
                        lines.append(f"  • {s['product_id']} {s['model_no']} ↔ {s['matched_to']} {s['matched_model']}")
                    bleed_section = "\n".join(lines)
                else:
                    bleed_section = "\n✅ No data bleeds detected."

            msg = (
                f"✅ <b>{label}</b> — {date_str}\n"
                f"📦 Scraped: {group_done} | ❌ Failed: {group_failed}\n"
                f"🕐 {datetime.now().strftime('%H:%M')}"
                f"{bleed_section}"
            )
            send_telegram(msg)
            log(f"[GROUP] {label} complete — {group_done} scraped, {group_failed} failed.")

            # ── Inter-group gap (not after the last group) ────────────────────
            if not is_last:
                gap = random.uniform(GROUP_GAP_MIN, GROUP_GAP_MAX)
                log(f"\nInter-group gap: {gap:.0f}s ({gap/60:.1f} min) before next group…")
                time.sleep(gap)

        browser.close()

    log(f"\nAll groups complete. Total done today: {len(completed)}")


# ── Missing-all server-side sequential run ───────────────────────────────────
MISSING_ALL_STATUS_PATH = "/opt/stic-scraper/data/missing_all_status.json"

def _write_missing_status(data: dict):
    """Write current run status to a file the portal can poll."""
    try:
        with open(MISSING_ALL_STATUS_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def run_missing_all(date_str: str):
    """
    Query every SCRAPE_GROUP for missing product IDs, sort smallest-first,
    then scrape them all in ONE browser session with proper inter-group gaps.
    Entirely server-side — no browser tab required.
    """
    import sqlite3 as _sq

    d, m, y = date_str.split("-")
    iso_date = f"{y}-{m}-{d}"

    # ── Build the work list ───────────────────────────────────────────────────
    db = _sq.connect(_DB_PATH, timeout=30)
    db.row_factory = _sq.Row
    groups_work = []

    for manufacturer, product_group, label in SCRAPE_GROUPS:
        if manufacturer:
            last = db.execute(
                "SELECT MAX(date) AS d FROM stic_prices WHERE manufacturer=? AND product_group=?",
                (manufacturer, product_group)
            ).fetchone()
        else:
            last = db.execute(
                "SELECT MAX(date) AS d FROM stic_prices WHERE product_group=?",
                (product_group,)
            ).fetchone()
        last_scraped = last["d"] if last else None
        if not last_scraped:
            continue

        if manufacturer:
            missing_rows = db.execute(
                """SELECT product_id FROM products
                   WHERE eol=0 AND manufacturer=? AND product_group=?
                     AND product_id NOT IN (
                         SELECT DISTINCT product_id FROM stic_prices
                         WHERE date=? AND manufacturer=? AND product_group=?
                     )""",
                (manufacturer, product_group, last_scraped, manufacturer, product_group)
            ).fetchall()
        else:
            missing_rows = db.execute(
                """SELECT product_id FROM products
                   WHERE eol=0 AND product_group=?
                     AND product_id NOT IN (
                         SELECT DISTINCT product_id FROM stic_prices
                         WHERE date=? AND product_group=?
                     )""",
                (product_group, last_scraped, product_group)
            ).fetchall()

        if missing_rows:
            groups_work.append({
                "manufacturer": manufacturer,
                "product_group": product_group,
                "label": label,
                "product_ids": [r["product_id"] for r in missing_rows],
            })
    db.close()

    if not groups_work:
        log("run_missing_all: no missing SKUs found — nothing to do.")
        _write_missing_status({"running": False, "done": True, "current": "Nothing missing"})
        return

    # Sort smallest group first
    groups_work.sort(key=lambda g: len(g["product_ids"]))
    total_groups = len(groups_work)
    total_skus   = sum(len(g["product_ids"]) for g in groups_work)
    log(f"run_missing_all: {total_groups} groups, {total_skus} missing SKUs — starting")
    _write_missing_status({
        "running": True, "done": False,
        "total_groups": total_groups, "total_skus": total_skus,
        "groups_done": 0, "current": "Logging in…",
    })

    username, password = get_credentials()
    if not username or not password:
        log("ERROR: STIC credentials not found.")
        _write_missing_status({"running": False, "done": True, "current": "ERROR: no credentials"})
        return

    cache = load_cache()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-GB",
            timezone_id="Europe/London",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()

        if not login(page, username, password):
            log("run_missing_all: Login failed — aborting.")
            _write_missing_status({"running": False, "done": True, "current": "Login failed"})
            browser.close()
            return

        for group_idx, grp in enumerate(groups_work):
            is_last     = (group_idx == total_groups - 1)
            label       = grp["label"]
            product_ids = grp["product_ids"]
            log(f"\n{'='*60}")
            log(f"[MISSING {group_idx+1}/{total_groups}] {label} — {len(product_ids)} missing SKUs")
            log(f"{'='*60}")
            _write_missing_status({
                "running": True, "done": False,
                "total_groups": total_groups, "total_skus": total_skus,
                "groups_done": group_idx, "current": f"{label} ({len(product_ids)} SKUs)",
            })

            # Load full product rows for these IDs
            all_products = read_products(1, 99999)
            pid_set  = {int(p) for p in product_ids}
            products = [p for p in all_products if int(p["product_id"]) in pid_set]

            # Clear today's stale rows so fresh data is written
            db2 = _sq.connect(_DB_PATH, timeout=30)
            db2.execute("PRAGMA journal_mode=WAL")
            ph = ",".join("?" for _ in product_ids)
            deleted = db2.execute(
                f"DELETE FROM stic_prices WHERE date=? AND product_id IN ({ph})",
                [iso_date] + [int(p) for p in product_ids]
            ).rowcount
            db2.commit()
            db2.close()
            if deleted:
                log(f"  Cleared {deleted} stale rows for this group.")

            group_done   = 0
            group_failed = 0
            products_since_pause = 0
            long_pause_every     = random.randint(20, 30)

            for product in products:
                prod_id      = str(product["product_id"])
                model_no     = product["model_no"]
                manufacturer = product["manufacturer"]
                ean          = product.get("ean")

                log(f"\n  [{group_done+group_failed+1}/{len(products)}] {model_no} (ID: {prod_id})")
                random_mouse_move(page)

                dist_data = search_and_scrape(
                    page, model_no, cache,
                    product_id=prod_id,
                    manufacturer=manufacturer,
                    ean=ean,
                )

                if dist_data is None:
                    log(f"  Page load failed — skipping.")
                elif not dist_data:
                    log(f"  FAILED — no result found for: {model_no}")
                    group_failed += 1
                else:
                    write_to_db(date_str, product, dist_data)
                    group_done += 1

                save_cache(cache)

                products_since_pause += 1
                if products_since_pause >= long_pause_every:
                    pause = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
                    log(f"  Long pause: {pause:.0f}s")
                    time.sleep(pause)
                    products_since_pause = 0
                    long_pause_every = random.randint(20, 30)
                else:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    log(f"  Delay: {delay:.1f}s")
                    time.sleep(delay)

            send_telegram(
                f"🔍 <b>Missing scrape: {label}</b>\n"
                f"📦 Found: {group_done} | ❌ Failed: {group_failed}\n"
                f"🕐 {datetime.now().strftime('%H:%M')}"
            )
            log(f"[MISSING] {label} done — {group_done} found, {group_failed} failed.")

            if not is_last:
                gap = random.uniform(GROUP_GAP_MIN, GROUP_GAP_MAX)
                log(f"\nInter-group gap: {gap:.0f}s ({gap/60:.1f} min)…")
                time.sleep(gap)

        browser.close()

    _write_missing_status({
        "running": False, "done": True,
        "total_groups": total_groups, "total_skus": total_skus,
        "groups_done": total_groups, "current": "Complete",
    })
    log(f"\nrun_missing_all complete — {total_groups} groups processed.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runall",     action="store_true", help="Morning run: all groups in sequence with random start delay")
    parser.add_argument("--gpus",       action="store_true", help="Afternoon run: GPU groups only with random start delay")
    parser.add_argument("--cpus-amd",   action="store_true", help="Manual run: AMD CPU groups only (AMD Retail + AMD MPK)")
    parser.add_argument("--cpus-intel", action="store_true", help="Manual run: Intel CPU groups only (Intel + Intel OEM)")
    parser.add_argument("--group",      type=str,            help="Run a single named group by label, e.g. --group \"ASUS GPU\"")
    parser.add_argument("--batch",    type=int, choices=[1, 2, 3], help="Legacy: 1/2/3 — split dynamically from product count")
    parser.add_argument("--test",     action="store_true", help="Test run: first 20 products only")
    parser.add_argument("--start",    type=int,            help="Custom start row")
    parser.add_argument("--end",      type=int,            help="Custom end row")
    parser.add_argument("--rescrape",    type=str,            help="Comma-separated product IDs to re-scrape")
    parser.add_argument("--missing-all", action="store_true", help="Server-side: scrape all missing SKUs across all groups, smallest first, one browser session")
    parser.add_argument("--force",       action="store_true", help="Ignore today's progress file and overwrite existing DB rows — use for re-runs and afternoon passes")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%d-%m-%Y")

    if args.missing_all:
        log("Missing-all mode: querying all groups for missing SKUs…")
        run_missing_all(date_str)
    elif args.rescrape:
        pids = [x.strip() for x in args.rescrape.split(",") if x.strip()]
        log(f"Re-scrape mode: {len(pids)} products — {pids}")
        run_specific(pids, date_str)
    elif args.group:
        label = args.group.strip()
        matched = [g for g in SCRAPE_GROUPS if g[2].lower() == label.lower()]
        if not matched:
            available = ", ".join(f'"{g[2]}"' for g in SCRAPE_GROUPS)
            log(f"ERROR: group '{label}' not found. Available: {available}")
            sys.exit(1)
        log(f"Single-group run: {matched[0][2]}" + (" (force)" if args.force else ""))
        run_groups(matched, date_str, random_start_delay=False, force=args.force)
    elif args.runall:
        log(f"Morning run: {len(SCRAPE_GROUPS)} groups — all products" + (" (force)" if args.force else ""))
        run_groups(SCRAPE_GROUPS, date_str, random_start_delay=True, force=args.force)
    elif args.gpus:
        log(f"Afternoon run: {len(GPU_GROUPS)} GPU groups only (force=True)")
        run_groups(GPU_GROUPS, date_str, random_start_delay=True, force=True)
    elif args.cpus_amd:
        log(f"Manual run: {len(CPU_AMD_GROUPS)} AMD CPU groups" + (" (force)" if args.force else ""))
        run_groups(CPU_AMD_GROUPS, date_str, random_start_delay=False, force=args.force)
    elif args.cpus_intel:
        log(f"Manual run: {len(CPU_INTEL_GROUPS)} Intel CPU groups" + (" (force)" if args.force else ""))
        run_groups(CPU_INTEL_GROUPS, date_str, random_start_delay=False, force=args.force)
    elif args.test:
        run(1, 20, date_str, is_final=False)
    elif args.batch in (1, 2, 3):
        total = count_products()
        ranges = batch_ranges(total)
        log(f"Template has {total} products — batch ranges: {ranges}")
        start, end = ranges[args.batch - 1]
        is_final = (args.batch == 3)
        run(start, end, date_str, is_final=is_final)
    elif args.start and args.end:
        run(args.start, args.end, date_str, is_final=True)
    else:
        parser.print_help()
