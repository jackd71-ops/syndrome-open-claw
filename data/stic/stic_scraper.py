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
CACHE_PATH    = "/opt/openclaw/data/stic/url_cache.json"
PROGRESS_PATH = "/opt/openclaw/data/stic/progress_{date}.json"
SESSION_PATH  = "/opt/openclaw/data/stic/session.json"
LOG_PATH      = "/opt/openclaw/logs/stic.log"

# ── STIC config ───────────────────────────────────────────────────────────────
STIC_BASE     = "https://www.stockinthechannel.co.uk"
STIC_LOGIN    = "https://www.stockinthechannel.co.uk/Account/Login"
STIC_SEARCH   = "https://www.stockinthechannel.co.uk/Search?q={query}"

# Credentials — stored in secrets.json
SECRETS_PATH  = "/opt/openclaw/secrets.json"

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
]

# Partial match aliases — STIC may show slightly different names
DISTRIBUTOR_ALIASES = {
    "TD Synnex UK":  ["td synnex", "tdsynnex", "synnex"],
    "VIP":           ["vip", "vip computers", "vip distribution"],
    "Westcoast":     ["westcoast", "west coast"],
    "Target":        ["target", "target components"],
    "M2M Direct":    ["m2m", "m2m direct"],
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

# ── Read products from DB (populated nightly by sync_template.py) ────────────
def _db_products_conn():
    import sqlite3
    db = sqlite3.connect(_DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def get_stic_url(product_id) -> str | None:
    """Return the cached STIC product detail URL for this product, or None."""
    db = _db_products_conn()
    row = db.execute("SELECT stic_url FROM products WHERE product_id=?", (int(product_id),)).fetchone()
    db.close()
    return row["stic_url"] if row and row["stic_url"] else None

def save_stic_url(product_id, url: str):
    """Persist a confirmed STIC product detail URL to the products table."""
    db = _db_products_conn()
    db.execute("UPDATE products SET stic_url=? WHERE product_id=?", (url, int(product_id)))
    db.commit()
    db.close()

def clear_stic_url(product_id):
    """Remove a confirmed-bad STIC URL so the product appears in Missing Results."""
    db = _db_products_conn()
    db.execute("UPDATE products SET stic_url=NULL WHERE product_id=?", (int(product_id),))
    db.commit()
    db.close()

def count_products() -> int:
    """Count active (non-EOL) products in the products DB table."""
    db = _db_products_conn()
    row = db.execute("SELECT COUNT(*) AS c FROM products WHERE eol=0").fetchone()
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
    rows = db.execute("SELECT * FROM products WHERE eol=0 ORDER BY product_id").fetchall()
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
            "SELECT * FROM products WHERE eol=0 AND manufacturer=? AND product_group=? ORDER BY product_id",
            (manufacturer, product_group)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM products WHERE eol=0 AND product_group=? ORDER BY product_id",
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
    (None,         "PROBE",      "Probe SKUs"),
]

GPU_GROUPS = [g for g in SCRAPE_GROUPS if g[1] == "PROD_VIDEO"]

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
_DB_PATH = "/opt/openclaw/data/analytics/prices.db"

_DIST_DB_NAME = {
    "TD Synnex UK": "TD Synnex",
    "VIP":          "VIP",
    "Westcoast":    "Westcoast",
    "Target":       "Target",
    "M2M Direct":   "M2M Direct",
}

def write_to_db(date_str: str, product: dict, distributor_data: dict, force: bool = False):
    """Write scraped distributor prices to SQLite. Never raises — logs on failure.
    If force=True, existing rows for this product+date are deleted first so the
    afternoon re-scrape overwrites morning data with fresh prices/stock."""
    import sqlite3
    try:
        # Convert DD-MM-YYYY to YYYY-MM-DD for DB consistency
        d, m, y = date_str.split("-")
        iso_date = f"{y}-{m}-{d}"

        product_id    = product["product_id"]
        model_no      = product["model_no"]
        manufacturer  = product["manufacturer"]
        product_group = product.get("product_group")
        chipset       = product.get("chipset")

        db = sqlite3.connect(_DB_PATH)
        db.execute("PRAGMA journal_mode=WAL")

        if force:
            # Delete today's rows so fresh data replaces them
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
        page.screenshot(path="/opt/openclaw/data/stic/pre_submit.png")

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
        page.screenshot(path="/opt/openclaw/data/stic/login_debug.png")
        log("Screenshot saved to /opt/openclaw/data/stic/login_debug.png")

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

    def _scrape_table_from_card(page_obj, search_query, log_label):
        """
        Navigate to STIC search for search_query, find the card whose text contains
        model_no (case-insensitive), scrape its distributor table.
        Returns (match_type_str, raw_rows_list, product_url_or_none).
        match_type is 'model' or 'none'.  product_url is the detail page href from the card.
        """
        url = STIC_SEARCH.format(query=str(search_query).replace(" ", "+"))
        log(f"  Searching ({log_label}): {search_query}")
        page_obj.goto(url, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))
        page_obj.mouse.wheel(0, random.randint(100, 400))
        time.sleep(random.uniform(0.5, 1.5))

        content = page_obj.content()
        if "0 Results Found" in content or "No results" in content.lower():
            log(f"  No results on STIC for: {search_query}")
            return "none", [], None

        res = page_obj.evaluate("""
            ([modelNo, mfr]) => {
                const modelLower = modelNo.toLowerCase();
                const mfrLower   = mfr ? mfr.toLowerCase() : null;
                const tables = document.querySelectorAll('table');
                if (!tables.length) return { match_type: 'none', rows: [] };

                // Check whether a card element contains our model name.
                // A line matches if it includes our model as a substring, BUT we reject
                // lines that contain variant markers (wifi/wi-fi) that our model doesn't
                // have — prevents "B650-PLUS" matching a "B650-PLUS WIFI" card, while
                // allowing "AM5 PROART X870E-CREATOR WIFI" to match "PROART X870E-CREATOR WIFI".
                const VARIANT_MARKERS = ['wifi', 'wi-fi'];
                const modelNorm = modelLower.replace(/-/g, ' ');
                const modelHasVariant = new Set(VARIANT_MARKERS.filter(v => modelNorm.includes(v)));
                function lineMatchesModel(line) {
                    if (!line.includes(modelLower)) return false;
                    const lineNorm = line.replace(/-/g, ' ');
                    for (const marker of VARIANT_MARKERS) {
                        if (lineNorm.includes(marker) && !modelHasVariant.has(marker)) return false;
                    }
                    return true;
                }
                function cardMatchesModel(el) {
                    const cardText = (el.innerText || '').toLowerCase();
                    // Manufacturer must appear somewhere in the card (if we know it)
                    if (mfrLower && !cardText.includes(mfrLower)) return false;
                    const lines = cardText.split('\\n').map(l => l.trim());
                    return lines.some(l => lineMatchesModel(l));
                }

                for (const table of tables) {
                    let el = table.parentElement;
                    for (let i = 0; i < 12; i++) {
                        if (!el) break;
                        if (cardMatchesModel(el)) {
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
                                // Extract product detail URL from the card
                                let productUrl = null;
                                const link = el.querySelector('a[href*="/Product/"]');
                                if (link) productUrl = link.href;
                                return { match_type: 'model', rows, productUrl };
                            }
                        }
                        el = el.parentElement;
                    }
                }
                return { match_type: 'none', rows: [], productUrl: null };
            }
        """, [model_no, manufacturer])

        mt = res.get("match_type", "none") if isinstance(res, dict) else "none"
        rr = res.get("rows", []) if isinstance(res, dict) else []
        pu = res.get("productUrl") if isinstance(res, dict) else None
        return mt, rr, pu

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
                    brand_ok = (not manufacturer) or (manufacturer.lower() in page_text)
                    model_ok = (not model_no)     or (model_no.lower() in page_text)
                    if brand_ok and model_ok:
                        log(f"  Direct URL scrape succeeded (brand + model validated).")
                        result = direct_result
                        found_url = cached_url
                    else:
                        failed = []
                        if not brand_ok: failed.append(f"brand '{manufacturer}'")
                        if not model_ok: failed.append(f"model '{model_no}'")
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

            vip_query = f"{manufacturer} {product_id}" if manufacturer else product_id
            match_type, raw_rows, card_url = _scrape_table_from_card(page, vip_query, "VIP SKU")

            if match_type == "model":
                url_confirmed = _check_url(card_url, "VIP SKU")
                if url_confirmed:
                    log(f"  VIP SKU search: URL sanity check passed — skipping model validation.")
                else:
                    log(f"  VIP SKU search matched on model name '{model_no}'.")

            # ── Step 2: EAN fallback — search by EAN, same model-name validation
            if match_type == "none" and ean:
                log(f"  VIP SKU search failed — trying EAN fallback: {ean}")
                match_type, raw_rows, card_url = _scrape_table_from_card(page, ean, "EAN")
            if match_type == "model":
                url_confirmed = _check_url(card_url, "EAN")
                if url_confirmed:
                    log(f"  EAN fallback: URL sanity check passed — skipping model validation.")
                else:
                    log(f"  EAN fallback matched on model name '{model_no}'.")
            else:
                log(f"  EAN fallback: model '{model_no}' not found in results.")

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
    db = _sqlite3.connect(_DB_PATH)
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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runall",   action="store_true", help="Morning run: all groups in sequence with random start delay")
    parser.add_argument("--gpus",     action="store_true", help="Afternoon run: GPU groups only with random start delay")
    parser.add_argument("--group",    type=str,            help="Run a single named group by label, e.g. --group \"ASUS GPU\"")
    parser.add_argument("--batch",    type=int, choices=[1, 2, 3], help="Legacy: 1/2/3 — split dynamically from product count")
    parser.add_argument("--test",     action="store_true", help="Test run: first 20 products only")
    parser.add_argument("--start",    type=int,            help="Custom start row")
    parser.add_argument("--end",      type=int,            help="Custom end row")
    parser.add_argument("--rescrape", type=str,            help="Comma-separated product IDs to re-scrape")
    parser.add_argument("--force",    action="store_true", help="Ignore today's progress file and overwrite existing DB rows — use for re-runs and afternoon passes")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%d-%m-%Y")

    if args.rescrape:
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
