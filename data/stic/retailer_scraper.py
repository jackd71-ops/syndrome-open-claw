#!/usr/bin/env python3
"""
Retailer price scraper for OpenClaw.
Uses direct product IDs (ASINs / Currys SKUs) from the retailer_ids DB table
for guaranteed accuracy; falls back to model-number search where no ID exists.
All product data is sourced from the products + retailer_ids DB tables (no Excel).

Retailer status (2026-04):
  ✅ Amazon UK    — dp/{ASIN} direct page (homepage warm-up for cookies)
  ✅ Currys       — search by SKU → direct product redirect
  ✅ Overclockers — OCUK code search via camoufox (ocuk_code in retailer_ids)
  ❌ Argos        — 403 on all URLs (Akamai)
  ✅ Scan         — patchright + Xvfb; URLs in retailer_ids (auto-discovered via Google)
  ✅ Box          — patchright + Xvfb; Angular SPA, price hydrates ~8s post-load
  ✅ CCL Online   — direct product URL → JSON-LD price
  ✅ AWD-IT       — direct product URL → JSON-LD price (URLs auto-discovered)
  ✅ Very         — patchright + Xvfb; URLs in retailer_ids (auto-discovered via Google)

Pre-flight discovery (runs automatically before batch 1):
  • AWD-IT: matches new products against cached category catalog; re-scrapes
    AWD-IT category pages only when unmatched products remain.
  • Scan: for any product with a Scan LN code but no Scan URL, searches
    Google UK (site:scan.co.uk) and saves the discovered URL.
  • Box: for any product with no Box URL, searches Google UK
    (site:box.co.uk) by model number and saves the discovered URL.
  • CCL: for any product with no CCL URL, searches Google UK
    (site:cclonline.com) by model number and saves the discovered URL.

Usage:
  python3 retailer_scraper.py --batch 1|2|3
  python3 retailer_scraper.py --test
  python3 retailer_scraper.py --discover        # run only the pre-flight phase
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import random
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import sqlite3
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from patchright.sync_api import sync_playwright as patchright_playwright

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH            = "/opt/openclaw/data/analytics/prices.db"
OUTPUT_DIR         = "/opt/openclaw/data/general"
PROGRESS_PATH      = "/opt/openclaw/data/stic/retailer_progress_{date}.json"
LOG_PATH           = "/opt/openclaw/logs/retailer.log"
SECRETS_PATH       = "/opt/openclaw/secrets.json"
AWDIT_CATALOG_PATH = "/opt/openclaw/data/stic/awdit_catalog.json"
SCAN_SCRAPE_PATH   = "/opt/openclaw/data/stic/scan_scrape.py"
VERY_SCRAPE_PATH   = "/opt/openclaw/data/stic/very_scrape.py"
BOX_SCRAPE_PATH    = "/opt/openclaw/data/stic/box_scrape.py"
ARGOS_SCRAPE_PATH  = "/opt/openclaw/data/stic/argos_scrape.py"
LOCK_PATH          = "/opt/openclaw/data/stic/retailer_scraper.lock"

# Populated by discover_* functions; consumed by run_discovery_sanity_report()
_DISCOVERY_LOG: list = []

# ── Process lock ──────────────────────────────────────────────────────────────
@contextmanager
def scraper_lock():
    """Prevent concurrent scraper/discovery instances from corrupting shared files."""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        yield
    except IOError:
        print(f"[Lock] Another scraper instance is already running — exiting.")
        sys.exit(0)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


# ── Virtual display (for patchright headed mode) ──────────────────────────────
@contextmanager
def virtual_display():
    """Start a temporary Xvfb display for headed browser sessions."""
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


# ── AWD-IT category pages for URL discovery ───────────────────────────────────
AWDIT_CATEGORIES = [
    "https://www.awd-it.co.uk/components/motherboards.html",
    "https://www.awd-it.co.uk/components/motherboards/intel-motherboards.html",
    "https://www.awd-it.co.uk/components/motherboards/amd-motherboards.html",
    "https://www.awd-it.co.uk/components/graphics-cards.html",
    "https://www.awd-it.co.uk/components/graphics-cards/nvidia-graphics-cards.html",
    "https://www.awd-it.co.uk/components/graphics-cards/amd-graphics-cards.html",
    "https://www.awd-it.co.uk/components.html",
    "https://www.awd-it.co.uk/networking.html",
    "https://www.awd-it.co.uk/components/storage.html",
    "https://www.awd-it.co.uk/components/memory.html",
]

# Polite delay between Google searches (seconds)
GOOGLE_DELAY_MIN = 6
GOOGLE_DELAY_MAX = 12

# ── Retailer definitions ───────────────────────────────────────────────────────
# id_col : column name in Retailer_IDs sheet (None = search-only, no IDs)
# blocked: skip entirely, write "—"
RETAILERS = [
    {"name": "Amazon UK",    "id_col": "amazon_asin", "blocked": False},
    {"name": "Currys",       "id_col": "currys_sku",  "blocked": False},
    {"name": "Argos",        "id_col": "argos_sku",   "blocked": False},
    {"name": "Scan",         "id_col": "scan_url",    "blocked": False},
    {"name": "Overclockers", "id_col": None,          "blocked": False},
    {"name": "Box",          "id_col": "box_url",     "blocked": False},
    {"name": "CCL Online",   "id_col": "ccl_url",     "blocked": False},
    {"name": "AWD-IT",       "id_col": "awdit_url",   "blocked": False},
    {"name": "Very",         "id_col": "very_url",    "blocked": False},
]

# ── Timing ────────────────────────────────────────────────────────────────────
DELAY_MIN      = 6
DELAY_MAX      = 14
LONG_PAUSE_MIN = 30
LONG_PAUSE_MAX = 90

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def get_secrets():
    with open(SECRETS_PATH) as f:
        return json.load(f)

# ── Progress ──────────────────────────────────────────────────────────────────
def load_progress(date_str):
    path = PROGRESS_PATH.format(date=date_str)
    if Path(path).exists():
        with open(path) as f:
            return set(json.load(f))
    return set()

def save_progress(date_str, completed):
    with open(PROGRESS_PATH.format(date=date_str), "w") as f:
        json.dump(list(completed), f)

# ── Dynamic batch split ───────────────────────────────────────────────────────
def count_products():
    """Return number of active (non-EOL) products in DB."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT COUNT(*) FROM products WHERE eol=0").fetchone()
    con.close()
    return row[0]

def batch_ranges(total):
    """Return list of (offset, limit) tuples for 3 equal batches."""
    size, rem = total // 3, total % 3
    ranges, offset = [], 0
    for i in range(3):
        limit = size + (1 if i < rem else 0)
        ranges.append((offset, limit))
        offset += limit
    return ranges

# ── Load retailer IDs from DB ─────────────────────────────────────────────────
def load_retailer_ids():
    """Returns dict: {product_id (str): {db_col_name: value}}"""
    con  = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM retailer_ids").fetchall()
    con.close()
    mapping = {}
    for row in rows:
        pid   = str(row["product_id"])
        codes = {k: row[k] for k in row.keys()
                 if k != "product_id" and row[k] is not None and str(row[k]).strip()}
        mapping[pid] = codes
    return mapping

# ── Read products from DB ──────────────────────────────────────────────────────
def read_products(offset=0, limit=None):
    """Return active products ordered by product_id, with optional offset/limit for batching."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if limit is not None:
        rows = con.execute(
            "SELECT product_id, description, model_no, manufacturer, product_group, msrp "
            "FROM products WHERE eol=0 ORDER BY product_id LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT product_id, description, model_no, manufacturer, product_group, msrp "
            "FROM products WHERE eol=0 ORDER BY product_id"
        ).fetchall()
    con.close()
    return [
        {
            "product_id":    str(row["product_id"]),
            "description":   row["description"] or "",
            "model_no":      row["model_no"] or "",
            "manufacturer":  row["manufacturer"] or "",
            "product_group": row["product_group"],
            "msrp":          row["msrp"],
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

# ── Amazon scraper (direct dp/ page) ─────────────────────────────────────────
amazon_warmed_up = False

def warm_up_amazon(page):
    global amazon_warmed_up
    if amazon_warmed_up:
        return
    log("  [Amazon] Warming up cookies via homepage...")
    page.goto("https://www.amazon.co.uk", wait_until="domcontentloaded", timeout=20000)
    time.sleep(4)
    amazon_warmed_up = True

def scrape_amazon(page, asin):
    warm_up_amazon(page)
    url = f"https://www.amazon.co.uk/dp/{asin}"
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(random.uniform(4, 7))
    for sel in [".a-price .a-offscreen", "#apex_offerDisplay_desktop .a-price .a-offscreen"]:
        try:
            el = page.query_selector(sel)
            if el:
                p = parse_price(el.inner_text())
                if p:
                    return p
        except Exception:
            pass
    return None

# ── Currys scraper (search by SKU → product page redirect) ───────────────────
def scrape_currys(page, sku):
    url = f"https://www.currys.co.uk/search?q={sku}"
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(random.uniform(5, 9))
    for sel in [".product-price", "[class*='ProductPrice']", "[class*='price']"]:
        try:
            el = page.query_selector(sel)
            if el:
                p = parse_price(el.inner_text())
                if p:
                    return p
        except Exception:
            pass
    return None

# ── CCL scraper (direct product URL → JSON-LD price) ─────────────────────────
def scrape_ccl(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(random.uniform(4, 7))
    price = page.evaluate("""() => {
        for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
                const d = JSON.parse(s.textContent);
                if (d.offers && d.offers.price) return parseFloat(d.offers.price);
                if (d['@graph']) {
                    for (const item of d['@graph']) {
                        if (item.offers && item.offers.price) return parseFloat(item.offers.price);
                    }
                }
            } catch(e) {}
        }
        return null;
    }""")
    return float(price) if price and price > 0 else None

# ── AWD-IT scraper (direct product URL → JSON-LD price) ──────────────────────
def scrape_awdit(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(random.uniform(4, 7))
    price = page.evaluate("""() => {
        for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
                const d = JSON.parse(s.textContent);
                if (d['@type'] === 'Product' && d.offers && d.offers.price) {
                    return parseFloat(d.offers.price);
                }
            } catch(e) {}
        }
        return null;
    }""")
    return float(price) if price and price > 0 else None

# ── Scan scraper (patchright + Xvfb subprocess) ──────────────────────────────
def scrape_scan(url):
    try:
        result = subprocess.run(
            ["xvfb-run", "--auto-servernum", "/usr/bin/python3", SCAN_SCRAPE_PATH, url],
            capture_output=True, text=True, timeout=60
        )
        out = result.stdout.strip()
        if out and out != "NOT_FOUND":
            return float(out)
    except (subprocess.TimeoutExpired, ValueError, Exception):
        pass
    return None


# ── Very scraper (patchright + Xvfb subprocess) ───────────────────────────────
def scrape_very(url):
    try:
        result = subprocess.run(
            ["xvfb-run", "--auto-servernum", "/usr/bin/python3", VERY_SCRAPE_PATH, url],
            capture_output=True, text=True, timeout=90
        )
        out = result.stdout.strip()
        if out and out != "NOT_FOUND":
            return float(out)
    except (subprocess.TimeoutExpired, ValueError, Exception):
        pass
    return None


# ── Box scraper (patchright + Xvfb subprocess) ───────────────────────────────
def scrape_box(url):
    try:
        result = subprocess.run(
            ["xvfb-run", "--auto-servernum", "/usr/bin/python3", BOX_SCRAPE_PATH, url],
            capture_output=True, text=True, timeout=120
        )
        out = result.stdout.strip()
        if out and out != "NOT_FOUND":
            return float(out)
    except (subprocess.TimeoutExpired, ValueError, Exception):
        pass
    return None


# ── Argos scraper (patchright + Xvfb subprocess) ─────────────────────────────
def scrape_argos(sku):
    try:
        result = subprocess.run(
            ["xvfb-run", "--auto-servernum", "/usr/bin/python3", ARGOS_SCRAPE_PATH, sku],
            capture_output=True, text=True, timeout=90
        )
        out = result.stdout.strip()
        if out and out != "NOT_FOUND":
            return float(out)
    except (subprocess.TimeoutExpired, ValueError, Exception):
        pass
    return None


# ── Overclockers scraper (camoufox + Xvfb subprocess) ────────────────────────
OCUK_SCRAPE_PATH = "/opt/openclaw/data/stic/ocuk_scrape.py"

def scrape_overclockers(code):
    try:
        result = subprocess.run(
            ["xvfb-run", "--auto-servernum", "/usr/bin/python3", OCUK_SCRAPE_PATH, code],
            capture_output=True, text=True, timeout=90
        )
        out = result.stdout.strip()
        if out and out != "NOT_FOUND":
            return float(out)
    except (subprocess.TimeoutExpired, ValueError, Exception):
        pass
    return None

# ── Main scrape per product ───────────────────────────────────────────────────
def scrape_product(page, product, retailer, id_codes):
    name     = retailer["name"]
    id_col   = retailer.get("id_col")

    # Scan: patchright + Xvfb subprocess, uses stored Scan URL
    if name == "Scan":
        url = id_codes.get(product["product_id"], {}).get("scan_url")
        if not url:
            return None, "NOT_STOCKED"
        try:
            price = scrape_scan(url)
            return (price, None) if price else (None, "NOT_FOUND")
        except Exception as e:
            log(f"    [{name}] ERROR: {e}")
            return None, "ERROR"

    # Very: patchright + Xvfb subprocess, uses stored Very URL
    if name == "Very":
        url = id_codes.get(product["product_id"], {}).get("very_url")
        sku = id_codes.get(product["product_id"], {}).get("very_sku")
        if not url:
            return None, "OUT_OF_STOCK" if sku else "NOT_STOCKED"
        try:
            price = scrape_very(url)
            return (price, None) if price else (None, "NOT_FOUND")
        except Exception as e:
            log(f"    [{name}] ERROR: {e}")
            return None, "ERROR"

    # Box: patchright + Xvfb subprocess, uses stored Box URL
    if name == "Box":
        url = id_codes.get(product["product_id"], {}).get("box_url")
        if not url:
            return None, "NOT_STOCKED"
        try:
            price = scrape_box(url)
            return (price, None) if price else (None, "NOT_FOUND")
        except Exception as e:
            log(f"    [{name}] ERROR: {e}")
            return None, "ERROR"

    # Overclockers: uses OCUK code from retailer_ids, scraped via camoufox subprocess
    if name == "Overclockers":
        code = id_codes.get(product["product_id"], {}).get("ocuk_code")
        if not code:
            return None, "NOT_STOCKED"
        try:
            price = scrape_overclockers(code)
            return (price, None) if price else (None, "NOT_FOUND")
        except Exception as e:
            log(f"    [{name}] ERROR: {e}")
            return None, "ERROR"

    # All other retailers: use id_codes from Retailer_IDs sheet
    prod_id = product["product_id"]
    code    = id_codes.get(prod_id, {}).get(id_col) if id_col else None

    if id_col and not code:
        return None, "NOT_STOCKED"

    try:
        if name == "Amazon UK":
            price = scrape_amazon(page, code)
        elif name == "Currys":
            price = scrape_currys(page, code)
        elif name == "Argos":
            price = scrape_argos(code)
        elif name == "CCL Online":
            price = scrape_ccl(page, code)
        elif name == "AWD-IT":
            price = scrape_awdit(page, code)
        else:
            return None, "NOT_FOUND"

        return (price, None) if price else (None, "NOT_FOUND")

    except PlaywrightTimeout:
        return None, "TIMEOUT"
    except Exception as e:
        log(f"    [{name}] ERROR: {e}")
        return None, "ERROR"

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

# ── SQLite DB write ───────────────────────────────────────────────────────────
_RETAILER_DB_NAME = {
    "Amazon UK":    "Amazon",
    "Currys":       "Currys",
    "Argos":        "Argos",
    "Scan":         "Scan",
    "Overclockers": "Overclockers",
    "Box":          "Box",
    "CCL Online":   "CCL Online",
    "AWD-IT":       "AWD-IT",
    "Very":         "Very",
}

def write_to_db(date_str, product, retailer_prices_dict, retailer_in_stock_dict=None):
    """Write one row per retailer for this product. Never raises — logs on failure."""
    try:
        d, m, y = date_str.split("-")
        iso_date = f"{y}-{m}-{d}"

        product_id    = int(product["product_id"])
        model_no      = product["model_no"]
        description   = product.get("description", "")
        manufacturer  = product["manufacturer"]
        product_group = product.get("product_group")
        msrp          = product.get("msrp")

        if retailer_in_stock_dict is None:
            retailer_in_stock_dict = {}

        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA journal_mode=WAL")
        for retailer_name, price in retailer_prices_dict.items():
            db_retailer = _RETAILER_DB_NAME.get(retailer_name, retailer_name)
            below_msrp = None
            if price is not None and msrp is not None:
                below_msrp = 1 if price < msrp else 0
            in_stock = retailer_in_stock_dict.get(retailer_name)
            db.execute(
                """INSERT OR IGNORE INTO retailer_prices
                   (date, product_id, model_no, description, manufacturer,
                    product_group, msrp, retailer, price, below_msrp, in_stock)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (iso_date, product_id, model_no, description, manufacturer,
                 product_group, msrp, db_retailer, price, below_msrp, in_stock),
            )
        db.commit()
        db.close()
    except Exception as e:
        log(f"  DB write error (non-fatal): {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def _needs_query(extra_where="", extra_cols=""):
    """Base query joining products + retailer_ids for discovery helper functions."""
    return (
        "SELECT p.product_id, p.model_no, p.manufacturer"
        + (", " + extra_cols if extra_cols else "")
        + " FROM products p"
        " LEFT JOIN retailer_ids r ON p.product_id = r.product_id"
        " WHERE p.eol = 0"
        + (" AND " + extra_where if extra_where else "")
    )


def load_products_needing_awdit():
    """Return list of {product_id, model_no, manufacturer} rows with no AWD-IT URL."""
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        _needs_query("(r.awdit_url IS NULL OR r.awdit_url = '')")
    ).fetchall()
    con.close()
    return [{"product_id": str(r[0]), "model_no": r[1] or "", "manufacturer": r[2] or ""}
            for r in rows]


def load_products_needing_scan_url():
    """Return list of {product_id, ln_code, model_no} rows with Scan LN but no Scan URL."""
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        _needs_query(
            "r.scan_ln IS NOT NULL AND r.scan_ln != '' "
            "AND (r.scan_url IS NULL OR r.scan_url = '')",
            extra_cols="r.scan_ln"
        )
    ).fetchall()
    con.close()
    return [{"product_id": str(r[0]), "model_no": r[1] or "",
             "ln_code": r[3]} for r in rows]


def load_products_needing_scan_url_by_model():
    """Return products with model_no but no Scan URL (no LN code required)."""
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        _needs_query(
            "(r.scan_url IS NULL OR r.scan_url = '') "
            "AND p.model_no IS NOT NULL AND p.model_no != ''"
        )
    ).fetchall()
    con.close()
    return [{"product_id": str(r[0]), "model_no": r[1] or "", "manufacturer": r[2] or ""}
            for r in rows]


def load_products_needing_very_url():
    """Return list of {product_id, sku, model_no} rows with Very SKU but no Very URL."""
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        _needs_query(
            "r.very_sku IS NOT NULL AND r.very_sku != '' "
            "AND (r.very_url IS NULL OR r.very_url = '')",
            extra_cols="r.very_sku"
        )
    ).fetchall()
    con.close()
    return [{"product_id": str(r[0]), "model_no": r[1] or "",
             "sku": r[3]} for r in rows]


def load_products_needing_box_url():
    """Return list of {product_id, model_no, manufacturer} rows with no Box URL."""
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        _needs_query(
            "(r.box_url IS NULL OR r.box_url = '') "
            "AND p.model_no IS NOT NULL AND p.model_no != ''"
        )
    ).fetchall()
    con.close()
    return [{"product_id": str(r[0]), "model_no": r[1] or "", "manufacturer": r[2] or ""}
            for r in rows]


def load_products_needing_ccl_url():
    """Return list of {product_id, model_no, manufacturer} rows with no CCL URL."""
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        _needs_query(
            "(r.ccl_url IS NULL OR r.ccl_url = '') "
            "AND p.model_no IS NOT NULL AND p.model_no != ''"
        )
    ).fetchall()
    con.close()
    return [{"product_id": str(r[0]), "model_no": r[1] or "", "manufacturer": r[2] or ""}
            for r in rows]


_URL_COL_MAP = {
    "AWD-IT URL": "awdit_url",
    "Scan URL":   "scan_url",
    "Very URL":   "very_url",
    "Box URL":    "box_url",
    "CCL URL":    "ccl_url",
}

def _write_discovered_urls(col_name, id_to_url):
    """Write {product_id: url} into the retailer_ids DB table."""
    if not id_to_url:
        return 0
    db_col = _URL_COL_MAP.get(col_name)
    if not db_col:
        log(f"[Discovery] ERROR: unknown URL column '{col_name}'")
        return 0
    con     = sqlite3.connect(DB_PATH)
    written = 0
    for pid, url in id_to_url.items():
        # Ensure row exists first (some products may not yet be in retailer_ids)
        con.execute(
            "INSERT OR IGNORE INTO retailer_ids (product_id) VALUES (?)", (int(pid),)
        )
        # Only write if not already set
        existing = con.execute(
            f"SELECT {db_col} FROM retailer_ids WHERE product_id=?", (int(pid),)
        ).fetchone()
        if existing and not existing[0]:
            con.execute(
                f"UPDATE retailer_ids SET {db_col}=? WHERE product_id=?",
                (url, int(pid))
            )
            written += 1
    con.commit()
    con.close()
    log(f"[Discovery] Wrote {written} URLs for '{col_name}'.")
    return written


# ── AWD-IT: token matching ────────────────────────────────────────────────────
def _is_awdit_match(catalog_name, model_no, manufacturer):
    name_l  = catalog_name.lower()
    model_l = model_no.lower()
    mfr_l   = manufacturer.lower().split()[0] if manufacturer else ""
    tokens  = [t for t in re.split(r'[\s\-/]+', model_l) if len(t) >= 2]
    if not tokens:
        return False
    match_count = sum(1 for t in tokens if t in name_l)
    mfr_match   = (mfr_l in name_l) if mfr_l else True
    return mfr_match and match_count >= max(2, len(tokens) * 0.7)


# ── AWD-IT: scrape one category page (all paginated pages) ───────────────────
def _awdit_scrape_category(page, category_url):
    """Return list of {name, url} across all pagination pages for one category."""
    all_items, url, page_num = [], category_url, 1
    while url:
        log(f"  [AWD-IT] Category page {page_num}: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(random.uniform(4, 7))
        except Exception as e:
            log(f"  [AWD-IT] Error: {e}")
            break
        body = page.inner_text("body")[:200].lower()
        if any(x in body for x in ("cloudflare", "just a moment", "blocked")):
            log("  [AWD-IT] Blocked — stopping category.")
            break
        items = page.evaluate("""() => {
            const results = [];
            for (const card of document.querySelectorAll(
                    '.product-item-info, .product-item, li.product-item')) {
                const link = card.querySelector(
                    'a.product-item-link, .product-item-link, a[title]');
                if (link) results.push({
                    name: (link.getAttribute('title') || link.innerText || '').trim(),
                    url:  link.href || ''
                });
            }
            return results;
        }""")
        new = [i for i in items if i["url"] and i["name"] and "awd-it.co.uk" in i["url"]]
        all_items.extend(new)
        log(f"  [AWD-IT] {len(new)} products (total: {len(all_items)})")
        if not new:
            break
        next_url = page.evaluate("""() => {
            const n = document.querySelector(
                'a.action.next, [aria-label="Next"], .pages-item-next a');
            return n ? n.href : null;
        }""")
        if next_url and next_url != url:
            url = next_url
            page_num += 1
        else:
            break
    return all_items


# ── AWD-IT discovery: match new products from cache, scrape if needed ─────────
def discover_awdit_urls(page):
    """
    Discover AWD-IT product URLs for products not yet in the Retailer_IDs sheet.
    1. Load cached catalog (JSON) — try to match new products from it first.
    2. Only scrape fresh AWD-IT category pages if unmatched products remain.
    3. Write newly found URLs to 'AWD-IT URL' column and save catalog.
    """
    needs = load_products_needing_awdit()
    if not needs:
        log("[Discovery] AWD-IT: all products already have URLs — skipping.")
        return 0

    log(f"[Discovery] AWD-IT: {len(needs)} products need URLs.")

    # Load existing catalog cache
    catalog = {}
    if Path(AWDIT_CATALOG_PATH).exists():
        try:
            with open(AWDIT_CATALOG_PATH) as f:
                catalog = json.load(f)
            log(f"[Discovery] AWD-IT: loaded {len(catalog)} cached catalog entries.")
        except Exception as e:
            log(f"[Discovery] AWD-IT: cache load error: {e}")

    def try_match(products):
        matched, unmatched = {}, []
        for p in products:
            hit = None
            for cat_name, cat_url in catalog.items():
                if _is_awdit_match(cat_name, p["model_no"], p["manufacturer"]):
                    hit = cat_url
                    break
            if hit:
                matched[p["product_id"]] = hit
                log(f"  [AWD-IT] ✅ {p['model_no'][:40]} (from cache)")
            else:
                unmatched.append(p)
        return matched, unmatched

    matched, still_needs = try_match(needs)

    if still_needs:
        log(f"[Discovery] AWD-IT: {len(still_needs)} still unmatched — scraping category pages...")

        # Discover extra category URLs from AWD-IT navigation
        try:
            page.goto("https://www.awd-it.co.uk", wait_until="domcontentloaded", timeout=25000)
            time.sleep(4)
            nav_links = page.evaluate("""() => {
                return [...document.querySelectorAll('nav a, .navigation a, #store\\.menu a')]
                    .map(a => ({text: a.innerText.trim(), href: a.href}))
                    .filter(a => a.href.includes('awd-it.co.uk') &&
                                 a.text.length > 2 && a.text.length < 50)
                    .slice(0, 50);
            }""")
            extra = [
                l["href"] for l in nav_links
                if any(kw in l["text"].lower() for kw in
                       ["motherboard", "graphics", "gpu", "component",
                        "storage", "memory", "networking", "monitor"])
            ]
            category_urls = list(set(AWDIT_CATEGORIES + extra))
        except Exception as e:
            log(f"[Discovery] AWD-IT: nav error ({e}), using predefined categories.")
            category_urls = AWDIT_CATEGORIES

        for cat_url in category_urls:
            items = _awdit_scrape_category(page, cat_url)
            for item in items:
                catalog[item["name"]] = item["url"]
            time.sleep(random.uniform(3, 6))

        # Save updated catalog
        try:
            with open(AWDIT_CATALOG_PATH, "w") as f:
                json.dump(catalog, f, indent=2)
            log(f"[Discovery] AWD-IT: catalog saved ({len(catalog)} entries).")
        except Exception as e:
            log(f"[Discovery] AWD-IT: catalog save error: {e}")

        # Try matching again with fresh catalog
        new_matched, final_unmatched = try_match(still_needs)
        matched.update(new_matched)
        if final_unmatched:
            log(f"[Discovery] AWD-IT: {len(final_unmatched)} products still unmatched:")
            for p in final_unmatched[:10]:
                log(f"  - {p['model_no']}")

    log(f"[Discovery] AWD-IT: matched {len(matched)}/{len(needs)} new products.")
    return _write_discovered_urls("AWD-IT URL", matched)


# ── Scan: Google UK search for one LN code ────────────────────────────────────
def _google_scan_url(page, ln_code):
    """Search Google UK for <LN> site:scan.co.uk; return first product URL or None."""
    search_url = (
        f"https://www.google.co.uk/search"
        f"?q={ln_code}+site%3Ascan.co.uk&hl=en-GB&gl=GB&num=5"
    )
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(random.uniform(3, 5))
    except Exception as e:
        log(f"  [Scan] Navigation error for {ln_code}: {e}")
        return None

    body = page.inner_text("body")[:400].lower()
    if "before you continue" in body or "i'm not a robot" in body:
        log(f"  [Scan] ⚠️  Google consent/CAPTCHA for {ln_code}")
        return None

    urls = page.evaluate(r"""() => {
        const found = [];
        for (const a of document.querySelectorAll('a[href]')) {
            const h = a.href || '';
            const m = h.match(/[?&]q=(https?:\/\/(?:www\.)?scan\.co\.uk\/products\/[^&]+)/);
            if (m) { found.push(decodeURIComponent(m[1])); continue; }
            if (/scan\.co\.uk\/products\//.test(h) && !h.includes('google.'))
                found.push(h);
        }
        return [...new Set(found)];
    }""")

    if urls:
        ln_lower = ln_code.lower()
        for u in urls:
            if ln_lower in u.lower():
                return u
        return urls[0]

    # Regex fallback on raw HTML
    raw = page.content()
    matches = re.findall(r'https?://(?:www\.)?scan\.co\.uk/products/[^\s"\'<>]+', raw)
    if matches:
        cleaned = [re.sub(r'["\'>]+$', '', m) for m in matches if m.startswith("http")]
        if cleaned:
            return cleaned[0]
    return None


def _fetch_scan_mpn(url):
    """
    Fetch a Scan product page and return the best model identifier for matching.
    Tries itemprop=mpn first; if that looks like an internal code (e.g. ASUS's
    90MB1EG0-M0EAY0), falls back to extracting the model from the page title.
    Scan product pages are not Cloudflare-gated for GET requests.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        mpn = None
        m = re.search(r'itemprop=["\']mpn["\'][^>]*>([^<]+)<', html)
        if m:
            mpn = m.group(1).strip()
        if not mpn:
            m2 = re.search(r'Manufacturer code:[^<]*<strong>([^<]+)<', html)
            if m2:
                mpn = m2.group(1).strip()

        # If MPN is an internal reference code (e.g. "90MB1EG0-M0EAY0"), extract
        # the model name from the page title instead (before the LN number).
        if mpn and re.match(r'^[0-9A-Z]{4,}[\-/]', mpn):
            m3 = re.search(r'<title>([^<]+)</title>', html)
            if m3:
                title = m3.group(1)
                # Strip trailing " | SCAN UK" and everything after " LN\d+"
                title = re.sub(r'\s+LN\d+.*', '', title)
                title = title.strip()
                if title:
                    return title  # e.g. "ASUS PRIME B650M-A WIFI II DDR5 PCIe 4.0 MicroATX Motherboard"

        return mpn
    except Exception:
        pass
    return None


def _mpn_matches(mpn, model_no):
    """
    Return True if the identifier fetched from Scan matches the model number.
    Both are normalised (lowercased, spaces/hyphens collapsed).
    Accepts: exact match, model contained in identifier, or identifier in model.
    """
    def norm(s):
        return re.sub(r'[\s\-]+', ' ', s.strip().lower())
    n_mpn   = norm(mpn)
    n_model = norm(model_no)
    return n_mpn == n_model or n_model in n_mpn or n_mpn in n_model


def _scan_direct_url(page, model_no, manufacturer):
    """
    Search scan.co.uk's own search engine for manufacturer + model_no.
    For each candidate URL, fetch the product page to check itemprop=mpn.
    Returns the first URL whose MPN matches, or None.
    """
    q = urllib.parse.quote_plus(f"{manufacturer} {model_no}")
    try:
        page.goto(f"https://www.scan.co.uk/search?q={q}",
                  wait_until="domcontentloaded", timeout=25000)
        time.sleep(random.uniform(3, 5))
    except Exception as e:
        log(f"  [Scan] Nav error for {model_no}: {e}")
        return None

    candidates = page.evaluate(r"""() => {
        const found = [];
        for (const a of document.querySelectorAll('a[href]')) {
            const h = a.href || '';
            if (/scan\.co\.uk\/products\//.test(h) && !h.includes('google.'))
                found.push(h.split('#')[0].split('?')[0]);
        }
        return [...new Set(found)];
    }""")

    # Exclude bundle/refurbished/open-box URLs — we want standalone product pages only
    _SCAN_SLUG_SKIP = ('bundle', 'refurbished', 'open-box', 'combo')
    filtered = [u for u in candidates if not any(s in u.lower() for s in _SCAN_SLUG_SKIP)]

    for url in filtered[:5]:
        mpn = _fetch_scan_mpn(url)
        if mpn and _mpn_matches(mpn, model_no):
            return url
        elif mpn:
            log(f"  [Scan]   skip {mpn!r} (wanted {model_no!r})")
    return None


# ── Scan URL discovery ────────────────────────────────────────────────────────
def discover_scan_urls(page):
    """
    For every product that has a Scan LN code but no Scan URL, search Google UK
    and write the discovered URL to the 'Scan URL' column.
    """
    needs = load_products_needing_scan_url()
    if not needs:
        log("[Discovery] Scan: all LN codes already have URLs — skipping.")
        return 0

    log(f"[Discovery] Scan: {len(needs)} products need URLs (searching Google UK).")
    found_urls = {}
    failed     = []

    for i, row in enumerate(needs, 1):
        ln    = row["ln_code"]
        pid   = row["product_id"]
        model = row["model_no"]
        log(f"  [Scan] [{i}/{len(needs)}] {ln}  ({model[:40]})")

        url = _google_scan_url(page, ln)
        if url:
            log(f"  [Scan] ✅ {url}")
            found_urls[pid] = url
        else:
            log(f"  [Scan] ❌ not found")
            failed.append(ln)

        if i < len(needs):
            time.sleep(random.uniform(GOOGLE_DELAY_MIN, GOOGLE_DELAY_MAX))

    log(f"[Discovery] Scan: found {len(found_urls)}/{len(needs)} URLs.")
    if failed:
        log(f"[Discovery] Scan: unresolved LN codes: {', '.join(failed[:20])}")
    return _write_discovered_urls("Scan URL", found_urls)


def discover_scan_urls_by_model(page):
    """
    Discover Scan URLs by searching scan.co.uk directly using manufacturer + model number.
    No LN code required. Validates each URL to avoid storing wrong-generation results.
    """
    needs = load_products_needing_scan_url_by_model()
    if not needs:
        log("[Discovery] Scan (model): all products have Scan URLs — skipping.")
        return 0

    log(f"[Discovery] Scan (model): {len(needs)} products need URLs (direct Scan search).")
    found_urls = {}

    # Warm up on Scan homepage before searching
    try:
        page.goto("https://www.scan.co.uk/", wait_until="domcontentloaded", timeout=25000)
        time.sleep(random.uniform(3, 5))
    except Exception as e:
        log(f"[Discovery] Scan (model): warmup failed: {e}")

    for i, row in enumerate(needs, 1):
        pid   = row["product_id"]
        model = row["model_no"]
        mfr   = row["manufacturer"]
        log(f"  [Scan] [{i}/{len(needs)}] {mfr} {model[:40]}")

        url = _scan_direct_url(page, model, mfr)
        if url:
            log(f"  [Scan] ✅ {url}")
            found_urls[pid] = url
            _DISCOVERY_LOG.append({"product_id": pid, "model_no": model,
                                   "manufacturer": mfr, "retailer": "Scan", "url": url})
        else:
            log(f"  [Scan] ❌ not found / no valid match")

        if i < len(needs):
            time.sleep(random.uniform(3, 6))

    log(f"[Discovery] Scan (model): found {len(found_urls)}/{len(needs)} URLs.")
    return _write_discovered_urls("Scan URL", found_urls)


# ── Combined pre-flight orchestrator ─────────────────────────────────────────
def _very_search_url(page, sku):
    """Search Very's own search box for <sku>; intercept the .prd redirect URL.
    Works even when Akamai blocks the product page — the redirect URL fires in
    network traffic before the block page renders."""
    intercepted = []

    def _on_response(resp):
        u = resp.url
        if ".prd" in u and "very.co.uk" in u:
            intercepted.append(u)

    page.on("response", _on_response)
    try:
        page.fill("#header-searchInput", sku)
        time.sleep(random.uniform(0.5, 1.0))
        page.keyboard.press("Enter")
        time.sleep(random.uniform(4, 6))
    except Exception as e:
        log(f"  [Very] Search input error for {sku}: {e}")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    if intercepted:
        return intercepted[0]

    # Fallback: scrape .prd links from page if somehow we landed on a results page
    try:
        links = page.evaluate("""() => {
            return [...document.querySelectorAll('a[href*=".prd"]')]
                .map(a => a.href)
                .filter(h => h.includes('very.co.uk'));
        }""")
        if links:
            return links[0]
    except Exception:
        pass

    return None


def _very_ensure_homepage(page):
    """Navigate back to Very homepage if we've drifted to a product/error page."""
    try:
        url = page.url
        if url != "https://www.very.co.uk/" and ".prd" not in url:
            return  # probably still on results page — fine
        if ".prd" in url or "access" in page.title().lower():
            page.goto("https://www.very.co.uk/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(random.uniform(2, 3))
    except Exception:
        pass


def discover_very_urls(page):
    """Discover Very product URLs for products that have a Very SKU but no Very URL.
    Uses Very's own search box + network response interception — works even when
    Akamai blocks the product page, because the redirect URL fires before the block."""
    needs = load_products_needing_very_url()
    if not needs:
        log("[Discovery] Very: all SKUs already have URLs — skipping.")
        return 0
    log(f"[Discovery] Very: {len(needs)} products need URLs (searching via Very search box).")

    # Ensure we're on the Very homepage with cookies accepted
    try:
        if "very.co.uk" not in page.url:
            page.goto("https://www.very.co.uk/", wait_until="domcontentloaded", timeout=25000)
            time.sleep(4)
        page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button')]
                .find(b => /accept all|allow all|accept cookies/i.test(b.textContent));
            if (btn) btn.click();
        }""")
        time.sleep(2)
    except Exception as e:
        log(f"  [Very] Homepage warm-up error: {e}")

    found_urls, failed = {}, []
    for i, p in enumerate(needs, 1):
        sku   = p["sku"]
        model = p["model_no"]
        log(f"  [Very] [{i}/{len(needs)}] {sku}  ({model[:40]})")
        time.sleep(random.uniform(GOOGLE_DELAY_MIN, GOOGLE_DELAY_MAX))

        url = _very_search_url(page, sku)
        if url:
            found_urls[p["product_id"]] = url
            log(f"  [Very] ✅ {url}")
        else:
            failed.append(sku)
            log(f"  [Very] ❌ not found")

        # Return to homepage between searches so search box is available
        _very_ensure_homepage(page)

    log(f"[Discovery] Very: found {len(found_urls)}/{len(needs)} URLs.")
    if failed:
        log(f"[Discovery] Very: unresolved SKUs: {', '.join(failed[:20])}")
    return _write_discovered_urls("Very URL", found_urls)


# ── URL verification via page title ──────────────────────────────────────────
def _title_matches(title, model_no):
    """
    Return True if the page title contains enough tokens from model_no.
    All significant tokens (4+ chars) must be present; shorter ones at least half.
    """
    def norm(s):
        return re.sub(r'[\s\-/]+', ' ', s.strip().lower())
    nt = norm(title)
    nm = norm(model_no)
    # Quick exact substring check
    if nm in nt:
        return True
    tokens = [t for t in nm.split() if len(t) >= 3]
    if not tokens:
        return False
    long_tokens  = [t for t in tokens if len(t) >= 4]
    short_tokens = [t for t in tokens if len(t) == 3]
    # All long tokens must appear
    if not all(t in nt for t in long_tokens):
        return False
    # At least half of short tokens
    short_ok = sum(1 for t in short_tokens if t in nt)
    return short_ok >= max(1, len(short_tokens) * 0.5) if short_tokens else True


def _verify_url_by_title(page, url, model_no, label=""):
    """
    Navigate to url and check the page title contains the model_no tokens.
    Used to confirm Box search results before storing the URL.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(random.uniform(2, 3))
        title = page.title()
        if _title_matches(title, model_no):
            return True
        log(f"  [{label}] title mismatch: {title[:80]!r}")
    except Exception as e:
        log(f"  [{label}] verify nav error for {url}: {e}")
    return False


def _verify_ccl_url(page, url, model_no, label=""):
    """
    Navigate to a CCL product page and verify by extracting the 'Mfg Code:' field.
    CCL renders this as a text node: 'Mfg Code: <model>'.
    Falls back to title matching if the field is absent.
    Returns True if verified, False otherwise.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(random.uniform(2, 3))

        mfg_code = page.evaluate("""() => {
            // Walk all text nodes looking for "Mfg Code:"
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                const t = node.textContent.trim();
                if (t.startsWith('Mfg Code:')) {
                    return t.slice('Mfg Code:'.length).trim();
                }
            }
            return null;
        }""")

        if mfg_code:
            if _mpn_matches(mfg_code, model_no):
                return True
            log(f"  [{label}] Mfg Code mismatch: {mfg_code!r} (wanted {model_no!r})")
            return False

        # Fallback to title if Mfg Code not found
        title = page.title()
        if _title_matches(title, model_no):
            return True
        log(f"  [{label}] title mismatch (no Mfg Code): {title[:80]!r}")
    except Exception as e:
        log(f"  [{label}] verify nav error for {url}: {e}")
    return False


# ── Multi-engine site: search (DDG primary, Google fallback) ─────────────────
_BOX_SKIP = {
    'laptops','gaming','components','computing','monitors','phones','printers',
    'networking','home','business','refurbished','deals','contact','warranty',
    'returns','news','account','wishlist','basket','search','store','blog',
    'gpu','cpus','motherboards','memory','storage','cooling','power-supplies',
    'pc-cases','graphics-cards','laptops-store','gaming-store','components-store',
}

# Engines tried in order. DDG first — no rate limiting observed.
# Google last — rate-limits after the first search per session but useful as fallback.
_SEARCH_ENGINES = [
    ("DDG",    lambda q: f"https://duckduckgo.com/?q={q}&kl=uk-en"),
    ("Google", lambda q: f"https://www.google.co.uk/search?q={q}&hl=en-GB&gl=GB&num=5"),
]

def _is_blocked(page):
    body = page.inner_text("body")[:500].lower()
    return ("before you continue" in body or "i'm not a robot" in body
            or "verify you are human" in body or len(body) < 50)

def _google_box_url(page, model_no, manufacturer):
    """
    Search for site:box.co.uk using DDG then Google (fallback).
    For each candidate URL, verifies the product page title matches model_no.
    Returns the first verified URL, or None.
    """
    q = urllib.parse.quote_plus(f"{manufacturer} {model_no} site:box.co.uk")
    skip = list(_BOX_SKIP)

    for engine_name, engine_url in _SEARCH_ENGINES:
        try:
            page.goto(engine_url(q), wait_until="domcontentloaded", timeout=20000)
            time.sleep(random.uniform(3, 5))
        except Exception as e:
            log(f"  [Box:{engine_name}] nav error: {e}")
            continue

        if _is_blocked(page):
            log(f"  [Box:{engine_name}] blocked/consent — trying next engine")
            continue

        candidates = page.evaluate(r"""(skip) => {
            const found = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.href || '';
                const m = h.match(/[?&](?:q|uddg)=(https?:\/\/(?:www\.)?box\.co\.uk\/[^&"<>#]+)/);
                const raw = m ? decodeURIComponent(m[1])
                              : (/box\.co\.uk\//.test(h)
                                 && !h.includes('google.') && !h.includes('duckduck') ? h : null);
                if (!raw) continue;
                try {
                    const u = new URL(raw);
                    const parts = u.pathname.split('/').filter(p => p);
                    if (parts.length !== 1) continue;
                    const seg = parts[0].toLowerCase();
                    if (seg.length < 10 || skip.includes(seg) || seg.includes('store')) continue;
                    found.push(raw.split('#')[0]);
                } catch(e) {}
            }
            return [...new Set(found)];
        }""", skip)

        # Regex fallback on raw HTML
        if not candidates:
            raw = page.content()
            matches = re.findall(r'https?://(?:www\.)?box\.co\.uk/([^\s"\'<>&/#]{15,})', raw)
            for slug in matches:
                slug = re.sub(r'["\'>]+$', '', slug)
                if not any(slug.lower() == s or slug.lower().startswith(s + '-') for s in _BOX_SKIP):
                    candidates.append(f"https://box.co.uk/{slug}")

        if not candidates:
            log(f"  [Box:{engine_name}] ❌ no candidates")
            continue

        for url in candidates[:5]:
            if _verify_url_by_title(page, url, model_no, label=f"Box:{engine_name}"):
                return url

    return None


# ── CCL Online: multi-engine search ──────────────────────────────────────────
def _google_ccl_url(page, model_no, manufacturer):
    """
    Search for site:cclonline.com using DDG then Google (fallback).
    Returns first valid CCL product URL (slug must contain 4+ digit code), or None.
    """
    q = urllib.parse.quote_plus(f"{manufacturer} {model_no} site:cclonline.com")

    for engine_name, engine_url in _SEARCH_ENGINES:
        try:
            page.goto(engine_url(q), wait_until="domcontentloaded", timeout=20000)
            time.sleep(random.uniform(3, 5))
        except Exception as e:
            log(f"  [CCL:{engine_name}] nav error: {e}")
            continue

        if _is_blocked(page):
            log(f"  [CCL:{engine_name}] blocked/consent — trying next engine")
            continue

        candidates = page.evaluate(r"""() => {
            const found = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.href || '';
                const m = h.match(/[?&](?:q|uddg)=(https?:\/\/(?:www\.)?cclonline\.com\/[^&"<>#]+)/);
                const raw = m ? decodeURIComponent(m[1])
                              : (/cclonline\.com\//.test(h)
                                 && !h.includes('google.') && !h.includes('duckduck') ? h : null);
                if (!raw) continue;
                try {
                    const u = new URL(raw);
                    const parts = u.pathname.split('/').filter(p => p);
                    if (parts.length !== 1 || parts[0].length < 10) continue;
                    if (!/\d{4,}/.test(parts[0])) continue;
                    found.push('https://www.cclonline.com/' + parts[0] + '/');
                } catch(e) {}
            }
            return [...new Set(found)];
        }""")

        if not candidates:
            raw = page.content()
            matches = re.findall(r'https?://(?:www\.)?cclonline\.com/([^\s"\'<>&/#]{15,})/?', raw)
            for slug in matches:
                slug = re.sub(r'["\'>]+$', '', slug)
                if re.search(r'\d{4,}', slug):
                    candidates.append(f"https://www.cclonline.com/{slug}/")

        if not candidates:
            log(f"  [CCL:{engine_name}] ❌ no candidates")
            continue

        for url in candidates[:5]:
            if _verify_ccl_url(page, url, model_no, label=f"CCL:{engine_name}"):
                return url

    return None


# ── Box URL discovery ─────────────────────────────────────────────────────────
def discover_box_urls(page):
    """Discover Box product URLs for all products with no Box URL via Google search."""
    needs = load_products_needing_box_url()
    if not needs:
        log("[Discovery] Box: all products already have URLs — skipping.")
        return 0
    log(f"[Discovery] Box: {len(needs)} products need URLs (searching Google UK).")
    found_urls, failed = {}, []
    for i, p in enumerate(needs, 1):
        model = p["model_no"]
        mfr   = p["manufacturer"]
        log(f"  [Box] [{i}/{len(needs)}] {model[:40]}  ({mfr})")
        time.sleep(random.uniform(GOOGLE_DELAY_MIN, GOOGLE_DELAY_MAX))
        url = _google_box_url(page, model, mfr)
        if url:
            found_urls[p["product_id"]] = url
            log(f"  [Box] ✅ {url}")
            _DISCOVERY_LOG.append({"product_id": p["product_id"], "model_no": model,
                                   "manufacturer": mfr, "retailer": "Box", "url": url})
        else:
            failed.append(model)
            log(f"  [Box] ❌ not found")
    log(f"[Discovery] Box: found {len(found_urls)}/{len(needs)} URLs.")
    if failed:
        log(f"[Discovery] Box: unresolved: {', '.join(f[:30] for f in failed[:20])}")
    return _write_discovered_urls("Box URL", found_urls)


# ── CCL URL discovery ─────────────────────────────────────────────────────────
def discover_ccl_urls(page):
    """Discover CCL Online product URLs for all products with no CCL URL via Google search."""
    needs = load_products_needing_ccl_url()
    if not needs:
        log("[Discovery] CCL: all products already have URLs — skipping.")
        return 0
    log(f"[Discovery] CCL: {len(needs)} products need URLs (searching Google UK).")
    found_urls, failed = {}, []
    for i, p in enumerate(needs, 1):
        model = p["model_no"]
        mfr   = p["manufacturer"]
        log(f"  [CCL] [{i}/{len(needs)}] {model[:40]}  ({mfr})")
        time.sleep(random.uniform(GOOGLE_DELAY_MIN, GOOGLE_DELAY_MAX))
        url = _google_ccl_url(page, model, mfr)
        if url:
            found_urls[p["product_id"]] = url
            log(f"  [CCL] ✅ {url}")
            _DISCOVERY_LOG.append({"product_id": p["product_id"], "model_no": model,
                                   "manufacturer": mfr, "retailer": "CCL Online", "url": url})
        else:
            failed.append(model)
            log(f"  [CCL] ❌ not found")
    log(f"[Discovery] CCL: found {len(found_urls)}/{len(needs)} URLs.")
    if failed:
        log(f"[Discovery] CCL: unresolved: {', '.join(f[:30] for f in failed[:20])}")
    return _write_discovered_urls("CCL URL", found_urls)


def load_amazon_prices():
    """
    Return {product_id (str): amazon_price (float)} from the most recent date
    in retailer_prices DB for Amazon UK. Returns empty dict if unavailable.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT MAX(date) AS d FROM retailer_prices WHERE retailer='Amazon UK'"
        ).fetchone()
        latest = row["d"] if row else None
        if not latest:
            con.close()
            return {}
        rows = con.execute(
            "SELECT product_id, price FROM retailer_prices "
            "WHERE retailer='Amazon UK' AND date=? AND price IS NOT NULL AND price>0",
            (latest,)
        ).fetchall()
        con.close()
        return {str(r["product_id"]): float(r["price"]) for r in rows}
    except Exception as e:
        log(f"[Sanity] Could not load Amazon prices from DB: {e}")
        return {}


def _quick_price_for_sanity(retailer, url, page):
    """
    Scrape a price from url for the sanity check.
    Uses the existing scraper for each retailer; returns float or None.
    """
    try:
        if retailer == "CCL Online":
            return scrape_ccl(page, url)
        if retailer == "Box":
            return scrape_box(url)
        if retailer == "Scan":
            return scrape_scan(url)
    except Exception as e:
        log(f"  [Sanity] scrape error for {retailer} {url}: {e}")
    return None


def run_discovery_sanity_report(page):
    """
    For every URL written during this discovery session, scrape its price and
    compare to the most recently scraped Amazon price for the same product.
    If a retailer price is more than 5% below Amazon, flag it — this may mean
    the search matched a cheaper/different variant.
    Writes a human-readable report to OUTPUT_DIR/discovery_sanity_YYYY-MM-DD.txt.
    """
    if not _DISCOVERY_LOG:
        log("[Sanity] No new URLs discovered this session — skipping sanity check.")
        return

    log(f"[Sanity] Checking {len(_DISCOVERY_LOG)} newly discovered URLs against Amazon prices...")
    amazon = load_amazon_prices()
    if not amazon:
        log("[Sanity] No Amazon prices available — skipping sanity check.")
        return

    THRESHOLD = 0.05  # flag if retailer is >5% cheaper than Amazon

    flags   = []
    ok_rows = []

    for item in _DISCOVERY_LOG:
        pid      = item["product_id"]
        model    = item["model_no"]
        mfr      = item["manufacturer"]
        retailer = item["retailer"]
        url      = item["url"]

        amz = amazon.get(pid)
        if not amz:
            log(f"  [Sanity] {pid} — no Amazon price on record, skipping")
            continue

        log(f"  [Sanity] {pid} {model[:35]} ({retailer}) ...")
        price = _quick_price_for_sanity(retailer, url, page)

        if price is None:
            log(f"  [Sanity]   could not scrape price")
            continue

        diff_pct = (price - amz) / amz  # negative = cheaper than Amazon
        log(f"  [Sanity]   {retailer} £{price:.2f}  vs  Amazon £{amz:.2f}  ({diff_pct:+.1%})")

        row = {
            "pid": pid, "model": model, "manufacturer": mfr,
            "retailer": retailer, "url": url,
            "retailer_price": price, "amazon_price": amz,
            "diff_pct": diff_pct,
        }
        if diff_pct < -THRESHOLD:
            flags.append(row)
        else:
            ok_rows.append(row)

    # Write report
    report_path = f"{OUTPUT_DIR}/discovery_sanity_{datetime.now().strftime('%Y-%m-%d')}.txt"
    lines = [
        f"Discovery Price Sanity Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Threshold: flag if retailer price >5% below Amazon",
        "=" * 72,
    ]

    if flags:
        lines.append(f"\n⚠️  FLAGGED — please verify SKU match ({len(flags)} items):\n")
        for r in flags:
            lines += [
                f"  Product:   {r['pid']}  {r['manufacturer']} {r['model']}",
                f"  Retailer:  {r['retailer']}  £{r['retailer_price']:.2f}",
                f"  Amazon:    £{r['amazon_price']:.2f}",
                f"  Diff:      {r['diff_pct']:+.1%}  ← CHEAPER THAN AMAZON BY >{THRESHOLD:.0%}",
                f"  URL:       {r['url']}",
                "",
            ]
    else:
        lines.append("\n✅ No flags — all discovered prices are within 5% of Amazon.\n")

    if ok_rows:
        lines.append(f"OK items ({len(ok_rows)}):\n")
        for r in ok_rows:
            lines.append(
                f"  {r['pid']:8s}  {r['manufacturer']:10s} {r['model'][:35]:35s}"
                f"  {r['retailer']:12s} £{r['retailer_price']:7.2f}"
                f"  Amazon £{r['amazon_price']:7.2f}  ({r['diff_pct']:+.1%})"
            )

    report_text = "\n".join(lines) + "\n"
    try:
        with open(report_path, "w") as f:
            f.write(report_text)
        log(f"[Sanity] Report written → {report_path}")
        if flags:
            log(f"[Sanity] ⚠️  {len(flags)} item(s) flagged — check report before next batch.")
            lines = [f"⚠️ <b>Discovery sanity check — {len(flags)} URL(s) need review</b>"]
            lines.append("Retailer price &gt;5% below Amazon — possible wrong SKU match:\n")
            for r in flags:
                lines.append(
                    f"• <b>{r['manufacturer']} {r['model']}</b>\n"
                    f"  {r['retailer']} £{r['retailer_price']:.2f} vs Amazon £{r['amazon_price']:.2f}"
                    f" ({r['diff_pct']:+.1%})\n"
                    f"  {r['url']}"
                )
            send_telegram("\n".join(lines))
    except Exception as e:
        log(f"[Sanity] Could not write report: {e}")
        log(report_text)


def run_preflight_discovery():
    """
    Run URL discovery for any new products that don't yet have retailer URLs.
    Uses its own browser instance so discovery and scraping are isolated.
    Discovered URLs are written directly to the retailer_ids DB table.
    """
    log("\n" + "=" * 65)
    log("PRE-FLIGHT URL DISCOVERY")
    log("=" * 65)

    _DISCOVERY_LOG.clear()  # reset for this session

    # Quick check: is there anything to do at all?
    awdit_needs      = load_products_needing_awdit()
    scan_needs       = load_products_needing_scan_url()
    scan_model_needs = load_products_needing_scan_url_by_model()
    very_needs       = load_products_needing_very_url()
    box_needs        = load_products_needing_box_url()
    ccl_needs        = load_products_needing_ccl_url()

    if not awdit_needs and not scan_needs and not scan_model_needs and not very_needs and not box_needs and not ccl_needs:
        log("[Discovery] Nothing to discover — all products have URLs. Skipping.")
        return

    log(
        f"[Discovery] AWD-IT: {len(awdit_needs)} | Scan(LN): {len(scan_needs)} | "
        f"Scan(model): {len(scan_model_needs)} | Very: {len(very_needs)} | "
        f"Box: {len(box_needs)} | CCL: {len(ccl_needs)}"
    )

    total_written = 0

    # AWD-IT: headless playwright (no bot detection issues)
    if awdit_needs:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"]
            )
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
                timezone_id="Europe/London",
                extra_http_headers={
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "DNT": "1",
                }
            )
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});
                Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3]});
                window.chrome = {runtime: {}};
            """)
            page = ctx.new_page()
            written = discover_awdit_urls(page)
            total_written += written
            browser.close()

    # Scan + Very + Box + CCL: patchright headed via Xvfb (Google blocks headless Chromium)
    if scan_needs or scan_model_needs or very_needs or box_needs or ccl_needs:
        with virtual_display():
            with patchright_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=False,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 768},
                    locale="en-GB",
                    timezone_id="Europe/London",
                )
                page = ctx.new_page()

                if scan_needs:
                    written = discover_scan_urls(page)
                    total_written += written

                if scan_model_needs:
                    written = discover_scan_urls_by_model(page)
                    total_written += written

                if very_needs:
                    written = discover_very_urls(page)
                    total_written += written

                if box_needs:
                    written = discover_box_urls(page)
                    total_written += written

                if ccl_needs:
                    written = discover_ccl_urls(page)
                    total_written += written

                # Price sanity check — uses same live browser page
                run_discovery_sanity_report(page)

                browser.close()

    if total_written:
        log(f"[Discovery] {total_written} new URLs written to DB.")
    else:
        log("[Discovery] No new URLs found this run.")

    log("PRE-FLIGHT COMPLETE\n" + "=" * 65 + "\n")


# ── Main run ──────────────────────────────────────────────────────────────────
def run(offset, limit, date_str, notify=False):
    log(f"Starting retailer scrape: offset={offset}, limit={limit}, date={date_str}")

    id_codes  = load_retailer_ids()
    products  = read_products(offset, limit)
    completed = load_progress(date_str)

    active = [r for r in RETAILERS if not r["blocked"]]
    log(f"Loaded {len(products)} products | ID codes: {len(id_codes)} | Active: {[r['name'] for r in active]}")

    found = 0
    not_stocked = 0
    not_found = 0
    products_done = 0
    since_pause   = 0
    long_pause_every = random.randint(20, 35)

    with virtual_display():
      with patchright_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox","--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            viewport={"width":1366,"height":768},
            locale="en-GB", timezone_id="Europe/London",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "DNT": "1",
            }
        )
        page = context.new_page()

        for product in products:
            product_id = product["product_id"]
            model_no   = product["model_no"]
            msrp       = product["msrp"]

            if product_id in completed:
                log(f"Skipping {product_id} ({model_no}) — already done.")
                continue

            msrp_str = f"£{msrp:.2f}" if msrp else "no MSRP"
            log(f"\n[{product_id}] {model_no} ({msrp_str})")

            page.mouse.move(random.randint(200,1100), random.randint(150,600))

            # Scrape active retailers; collect prices and in_stock for DB write
            db_prices   = {}
            db_in_stock = {}

            for retailer in active:
                name  = retailer["name"]
                price, status = scrape_product(page, product, retailer, id_codes)

                if price is not None:
                    below = " ⚠️ BELOW MSRP" if msrp and price < msrp else ""
                    log(f"    [{name}] £{price:.2f}{below}")
                    db_prices[name]   = price
                    db_in_stock[name] = 1
                    found += 1
                elif status == "OUT_OF_STOCK":
                    log(f"    [{name}] out of stock")
                    db_prices[name]   = None
                    db_in_stock[name] = 0
                    not_stocked += 1
                elif status == "NOT_STOCKED":
                    log(f"    [{name}] not stocked (no ID code)")
                    db_prices[name]   = None
                    db_in_stock[name] = None
                    not_stocked += 1
                else:
                    log(f"    [{name}] not found ({status})")
                    db_prices[name]   = None
                    db_in_stock[name] = None
                    not_found += 1

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            write_to_db(date_str, product, db_prices, db_in_stock)

            products_done += 1
            completed.add(product_id)
            save_progress(date_str, completed)

            since_pause += 1
            if since_pause >= long_pause_every:
                pause = random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX)
                log(f"Long pause: {pause:.0f}s")
                time.sleep(pause)
                since_pause = 0
                long_pause_every = random.randint(20, 35)

        browser.close()

    log(f"\nBatch complete. Products={products_done}, Found={found}, Not stocked={not_stocked}, Not found={not_found}")

    if notify:
        total_today = len(load_progress(date_str))
        send_telegram(
            f"🛒 <b>Retailer Tracker complete</b> — {date_str}\n\n"
            f"✔️ Total today: {total_today} products\n"
            f"💰 Prices found: {found}\n"
            f"🚫 Not stocked: {not_stocked}\n"
            f"❌ Not found: {not_found}"
        )

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch",    type=int, choices=[1, 2, 3],
                        help="Run one of three equal-sized batches (discovery runs before batch 1)")
    parser.add_argument("--test",     action="store_true",
                        help="Scrape first 20 products (includes pre-flight discovery)")
    parser.add_argument("--discover", action="store_true",
                        help="Run only the pre-flight URL discovery, then exit")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%d-%m-%Y")

    with scraper_lock():

      if args.discover:
        # Standalone discovery run — useful to trigger manually after adding new IDs to DB
        run_preflight_discovery()

      elif args.test:
        # Test mode: run discovery then scrape first 20 products
        run_preflight_discovery()
        run(offset=0, limit=20, date_str=date_str, notify=False)

      elif args.batch in (1, 2, 3):
        total  = count_products()
        ranges = batch_ranges(total)
        log(f"DB: {total} active products — batch ranges (offset, limit): {ranges}")
        offset, limit = ranges[args.batch - 1]

        if args.batch == 1:
            # Run URL discovery for any new products before price scraping starts
            run_preflight_discovery()
            # Re-read count after discovery (new products may have been found in other tables,
            # but DB product count itself doesn't change here)
            total  = count_products()
            ranges = batch_ranges(total)
            offset, limit = ranges[0]

        run(offset, limit, date_str, notify=(args.batch == 3))

      else:
        parser.print_help()
