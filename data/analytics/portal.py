#!/usr/bin/env python3
"""
OpenClaw Sales Portal — port 8090
Power BI-inspired design. Two tabs: STIC and Retailer.
Queries SQLite live on every request. No caching.
Authentication is handled by Cloudflare Access upstream.
"""

import sqlite3
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)
DB_PATH = "/opt/openclaw/data/analytics/prices.db"

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def qry(sql, params=()):
    db = get_db()
    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]

def qry_one(sql, params=()):
    db = get_db()
    row = db.execute(sql, params).fetchone()
    db.close()
    return dict(row) if row else None

def latest_date(table="stic_prices"):
    from datetime import datetime, timedelta
    import zoneinfo
    today = datetime.now(zoneinfo.ZoneInfo("Europe/London")).date().isoformat()
    yesterday = (datetime.now(zoneinfo.ZoneInfo("Europe/London")).date() - timedelta(days=1)).isoformat()
    db = get_db()
    counts = {r["d"]: r["c"] for r in db.execute(
        f"SELECT date AS d, COUNT(*) AS c FROM {table} WHERE date IN (?,?) GROUP BY date",
        (today, yesterday)
    ).fetchall()}
    db.close()
    today_c = counts.get(today, 0)
    yest_c  = counts.get(yesterday, 0)
    if yest_c and today_c >= yest_c * 0.9:
        return today
    return yesterday if yest_c else (today if today_c else None)


def latest_date_for_group(group_filter):
    """Return the most recent date that has meaningful data for a product group.
    Falls back up to 14 days to handle partial/missed scrape days."""
    db = get_db()
    rows = db.execute(
        f"SELECT sp.date, COUNT(DISTINCT sp.product_id) AS c "
        f"FROM stic_prices sp "
        f"JOIN products p ON p.product_id = sp.product_id "
        f"WHERE {group_filter} "
        f"GROUP BY sp.date ORDER BY sp.date DESC LIMIT 14"
    ).fetchall()
    db.close()
    if not rows:
        return None
    counts = [(r["date"], r["c"]) for r in rows]
    # Use the most recent date that has at least 50% of the max seen
    max_c = max(c for _, c in counts)
    threshold = max(1, max_c * 0.5)
    for date, c in counts:
        if c >= threshold:
            return date
    return counts[0][0]  # fallback: most recent regardless

def prev_date(table, current):
    r = qry_one(
        f"SELECT MAX(date) AS d FROM {table} WHERE date < ?", (current,)
    )
    return r["d"] if r else None


def _init_products():
    """Create the products table if it doesn't exist yet, and migrate new columns.
    sync_template.py populates it nightly from the Excel master."""
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS products (
        product_id    INTEGER PRIMARY KEY,
        description   TEXT,
        model_no      TEXT,
        manufacturer  TEXT,
        product_group TEXT,
        chipset       TEXT,
        ean           TEXT,
        eol           INTEGER NOT NULL DEFAULT 0,
        stic_url      TEXT
    )""")
    # Migrate: add stic_url column if upgrading from older schema
    try:
        db.execute("ALTER TABLE products ADD COLUMN stic_url TEXT")
    except Exception:
        pass  # column already exists
    db.commit()
    db.close()


def read_template_products():
    """Return all products from the products DB table as list of dicts."""
    rows = qry("SELECT * FROM products ORDER BY product_id")
    for r in rows:
        r["eol"] = bool(r.get("eol", 0))
    return rows


def write_eol_to_template(product_id: int, mark: bool):
    """Write EOL flag to the products DB table (DB-only; nightly sync flushes to Excel)."""
    db = get_db()
    db.execute(
        "UPDATE products SET eol=? WHERE product_id=?",
        (1 if mark else 0, product_id)
    )
    changed = db.execute("SELECT changes()").fetchone()[0]
    db.commit()
    db.close()
    return bool(changed)


# ── Chipset extraction ─────────────────────────────────────────────────────────

_CHIPSET_RE = re.compile(
    r'\b(Z[0-9]{3}[A-Z]?|B[0-9]{3}[A-Z]?|H[0-9]{3}[A-Z]?|X[0-9]{3}[A-Z]?|'
    r'A[0-9]{3}[A-Z]?|W[0-9]{3}[A-Z]?|TRX[0-9]+|WRX[0-9]+)\b'
)

_GPU_CHIP_RE = re.compile(r'((?:RTX|RX|GTX|GT)\d{3,4}(?:TI|XT|XTX)?)', re.IGNORECASE)
_GPU_MEM_RE  = re.compile(r'(?:^|[-])O?(\d+)G(?:D\d)?(?:[-]|$)', re.IGNORECASE)

def extract_chipset(model_no):
    m = _CHIPSET_RE.search(model_no.upper())
    return m.group(1) if m else "Other"

def extract_gpu_chipset(model_no):
    chip_m = _GPU_CHIP_RE.search(model_no)
    if not chip_m:
        return "Other"
    chip = chip_m.group(1).upper()  # e.g. RTX5060TI, RX9070XT, GT710
    mem_m = _GPU_MEM_RE.search(model_no)
    mem = f" {mem_m.group(1)}G" if mem_m else ""
    return f"{chip}{mem}"

# ── HTML template ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Competition Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; font-size: 13px;
         background: #F3F2F1; color: #323130; display: flex; flex-direction: column; height: 100vh; }

  /* Tab bar */
  .tab-bar { background: #fff; border-bottom: 1px solid #EDEBE9; padding: 0 16px;
              display: flex; align-items: flex-end; gap: 2px; }
  .tab { padding: 10px 20px; cursor: pointer; border-bottom: 2px solid transparent;
         font-size: 13px; color: #605E5C; transition: all .15s; user-select: none; }
  .tab:hover { color: #323130; background: #F3F2F1; }
  .tab.active { color: #0078D4; border-bottom-color: #0078D4; font-weight: 600; }
  .tab-bar-title { font-size: 16px; font-weight: 600; color: #323130; padding: 12px 20px 12px 0;
                   border-right: 1px solid #EDEBE9; margin-right: 8px; }

  /* Layout */
  .layout { display: flex; flex: 1; overflow: hidden; }
  .sidebar { width: 220px; background: #fff; border-right: 1px solid #EDEBE9;
             overflow-y: auto; padding: 12px 0; flex-shrink: 0; }
  .main { flex: 1; overflow-y: auto; padding: 20px; }

  /* Sidebar */
  .sidebar-section { margin-bottom: 4px; }
  .sidebar-section-header { padding: 6px 16px; font-size: 11px; font-weight: 600;
                             text-transform: uppercase; letter-spacing: .5px; color: #A19F9D;
                             cursor: pointer; display: flex; justify-content: space-between;
                             align-items: center; user-select: none; }
  .sidebar-section-header:hover { background: #F3F2F1; }
  .sidebar-section-header .arrow { transition: transform .2s; font-size: 10px; }
  .sidebar-section-header.collapsed .arrow { transform: rotate(-90deg); }
  .sidebar-items { }
  .sidebar-items.hidden { display: none; }
  .sidebar-btn { display: block; width: 100%; text-align: left; padding: 7px 16px 7px 24px;
                 background: none; border: none; cursor: pointer; font-size: 12px; color: #323130;
                 border-left: 3px solid transparent; transition: all .1s; }
  .sidebar-btn:hover { background: #F3F2F1; color: #0078D4; }
  .sidebar-btn.active { background: #DEECF9; color: #0078D4; border-left-color: #0078D4;
                        font-weight: 600; }

  /* KPI cards */
  .overview-cards { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:20px; }
  .kpi-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .scrape-status-row { display:none; }
  .kpi-card { background: #fff; border: 1px solid #EDEBE9; border-radius: 2px;
               padding: 14px 18px; position:relative; }
  .kpi-card .label { font-size: 11px; color: #A19F9D; text-transform: uppercase;
                     letter-spacing: .4px; margin-bottom: 6px; }
  .kpi-card .value { font-size: 24px; font-weight: 600; color: #323130; }
  .kpi-card .sub { font-size: 11px; color: #605E5C; margin-top: 4px; }
  .kpi-card .tl-dot { position:absolute; top:10px; right:10px; width:10px; height:10px;
    border-radius:50%; display:inline-block; }
  .tl-green  { background:#107C10; }
  .tl-amber  { background:#F7941D; }
  .tl-red    { background:#D13438; }

  /* Search */
  .search-bar { display: flex; gap: 8px; margin-bottom: 20px; }
  .search-bar input { flex: 1; padding: 7px 12px; border: 1px solid #8A8886; border-radius: 2px;
                      font-size: 13px; font-family: inherit; outline: none; }
  .search-bar input:focus { border-color: #0078D4; }
  .search-bar button { padding: 7px 16px; background: #0078D4; color: #fff; border: none;
                       border-radius: 2px; cursor: pointer; font-size: 13px; font-family: inherit; }
  .search-bar button:hover { background: #106EBE; }

  /* Tables */
  .section-title { font-size: 14px; font-weight: 600; color: #323130; margin-bottom: 10px; }
  .tbl-wrap { overflow-x: auto; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #EDEBE9; }
  th { background: #C8D6E5; color: #323130; font-weight: 600; font-size: 12px;
       padding: 8px 10px; text-align: left; white-space: nowrap; }
  td { padding: 7px 10px; font-size: 12px; border-bottom: 1px solid #F3F2F1; }
  tr:nth-child(even) td { background: #FAFAFA; }
  tr:hover td { background: #DEECF9; }
  .clickable { cursor: pointer; }
  .clickable td:first-child { color: #0078D4; }
  tr.row-selected td { background: #EFF6FC !important; }
  .drill-header { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
  .drill-close { background:none; border:1px solid #C8C6C4; border-radius:3px; padding:2px 10px; cursor:pointer; color:#605E5C; font-size:12px; }
  .drill-close:hover { background:#F3F2F1; }
  .watch-btn { background:none; border:none; cursor:pointer; font-size:18px; color:#C8C6C4; padding:0 2px; line-height:1; vertical-align:middle; transition:color .15s; }
  .watch-btn.watched { color:#FFB900; }
  .watch-btn:hover { color:#FFB900; }
  .watch-star { background:none; border:none; cursor:pointer; font-size:13px; color:#C8C6C4; padding:0 3px; line-height:1; }
  .watch-star.watched { color:#FFB900; }
  .watch-star:hover { color:#FFB900; }
  td.wstar { width:26px; padding:4px 2px !important; text-align:center !important; }
  th.wstar { width:26px; padding:4px 2px !important; }
  .eol-btn { background:none; border:1px solid #D13438; color:#D13438; border-radius:3px;
    cursor:pointer; font-size:11px; font-weight:600; padding:2px 7px; line-height:1.4;
    transition:background .15s,color .15s; white-space:nowrap; }
  .eol-btn:hover { background:#D13438; color:#fff; }
  .eol-btn.is-eol { background:#D13438; color:#fff; }
  .eol-btn.is-eol:hover { background:#A4262C; border-color:#A4262C; }
  .inv-subsection { margin-bottom:18px; }
  .inv-subsection-title { font-size:12px; font-weight:600; color:#605E5C; text-transform:uppercase;
    letter-spacing:.04em; margin-bottom:6px; padding-bottom:4px; border-bottom:1px solid #EDEBE9; }
  .inv-empty { color:#A19F9D; font-size:13px; padding:6px 0; }

  /* Badges */
  .badge { display: inline-block; padding: 2px 6px; border-radius: 2px; font-size: 11px;
           font-weight: 600; }
  .badge-red { background: #FDE7E9; color: #A4262C; }
  .badge-green { background: #DFF6DD; color: #107C10; }
  .badge-orange { background: #FFF4CE; color: #8A4B00; }
  .badge-blue { background: #DEECF9; color: #0078D4; }

  /* Scrape trigger button */
  .scrape-trigger-btn { padding: 5px 14px; font-size: 12px; font-weight: 600; border: none;
    border-radius: 2px; cursor: pointer; background: #0078D4; color: #fff; }
  .scrape-trigger-btn:hover:not([disabled]) { background: #106EBE; }

  /* Import / Export tool cards */
  .ie-tool-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 12px; margin-bottom: 20px; }
  .ie-tool-card { background: #fff; border: 1px solid #EDEBE9; border-radius: 2px;
    padding: 16px 18px; cursor: pointer; transition: border-color .15s, box-shadow .15s;
    display: flex; flex-direction: column; gap: 4px; }
  .ie-tool-card:hover { border-color: #0078D4; box-shadow: 0 2px 8px rgba(0,120,212,0.1); }
  .ie-tool-card .ie-icon { font-size: 22px; margin-bottom: 4px; }
  .ie-tool-card .ie-name { font-size: 13px; font-weight: 600; color: #323130; }
  .ie-tool-card .ie-desc { font-size: 12px; color: #605E5C; line-height: 1.5; }
  .ie-tool-card .ie-tag { display: inline-block; margin-top: 6px; padding: 2px 8px;
    border-radius: 2px; font-size: 11px; font-weight: 600; }
  .ie-tag-import { background: #DFF6DD; color: #107C10; }
  .ie-tag-export { background: #DEECF9; color: #0078D4; }

  /* Drop zone */
  .ie-dropzone { border: 2px dashed #C8C6C4; border-radius: 4px; padding: 32px 20px;
    text-align: center; background: #FAFAFA; cursor: pointer;
    transition: border-color .15s, background .15s; }
  .ie-dropzone:hover, .ie-dropzone.drag-over { border-color: #0078D4; background: #EFF6FC; }
  .ie-dropzone .dz-icon { font-size: 28px; margin-bottom: 8px; color: #A19F9D; }
  .ie-dropzone .dz-text { font-size: 13px; color: #605E5C; }
  .ie-dropzone .dz-hint { font-size: 11px; color: #A19F9D; margin-top: 4px; }
  .ie-dropzone input[type=file] { display: none; }

  /* Import action buttons */
  .ie-btn { padding: 7px 16px; font-size: 12px; font-weight: 600; border: none;
    border-radius: 2px; cursor: pointer; font-family: inherit; }
  .ie-btn-primary { background: #0078D4; color: #fff; }
  .ie-btn-primary:hover:not([disabled]) { background: #106EBE; }
  .ie-btn-secondary { background: #fff; color: #323130; border: 1px solid #C8C6C4; }
  .ie-btn-secondary:hover { background: #F3F2F1; }
  .ie-btn-success { background: #107C10; color: #fff; }
  .ie-btn-success:hover:not([disabled]) { background: #0E6A0E; }
  .ie-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .ie-action-bar { display: flex; gap: 8px; align-items: center; margin: 12px 0 16px; }

  /* Row status pills */
  .ie-new    { background: #DFF6DD; color: #107C10; padding: 1px 6px; border-radius: 2px;
               font-size: 11px; font-weight: 600; }
  .ie-update { background: #DEECF9; color: #0078D4; padding: 1px 6px; border-radius: 2px;
               font-size: 11px; font-weight: 600; }
  .ie-error  { background: #FDE7E9; color: #A4262C; padding: 1px 6px; border-radius: 2px;
               font-size: 11px; font-weight: 600; }
  .ie-warn   { background: #FFF4CE; color: #8A4B00; padding: 1px 6px; border-radius: 2px;
               font-size: 11px; font-weight: 600; }

  /* Import summary bar */
  .ie-summary { display: flex; gap: 16px; padding: 10px 14px; background: #fff;
    border: 1px solid #EDEBE9; border-radius: 2px; margin-bottom: 12px;
    font-size: 12px; align-items: center; flex-wrap: wrap; }
  .ie-summary strong { font-size: 15px; margin-right: 2px; }

  /* Charts */
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .chart-box { background: #fff; border: 1px solid #EDEBE9; border-radius: 2px; padding: 16px; }
  .chart-box h4 { font-size: 12px; font-weight: 600; color: #605E5C; margin-bottom: 10px; }
  .chart-box canvas { max-height: 200px; }

  /* Spinner */
  .spinner { text-align: center; padding: 40px; color: #A19F9D; font-size: 13px; }

  /* Info modal */
  .modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.35);
                    z-index: 1000; align-items: center; justify-content: center; }
  .modal-backdrop.open { display: flex; }
  .modal { background: #fff; border-radius: 2px; width: 520px; max-width: 90vw;
           max-height: 80vh; display: flex; flex-direction: column;
           box-shadow: 0 8px 32px rgba(0,0,0,0.18); }
  .modal-header { background: #0078D4; color: #fff; padding: 14px 18px;
                  display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
  .modal-header h3 { font-size: 14px; font-weight: 600; margin: 0; }
  .modal-close { background: none; border: none; color: #fff; font-size: 18px; cursor: pointer;
                 line-height: 1; padding: 0 2px; opacity: 0.85; }
  .modal-close:hover { opacity: 1; }
  .modal-body { padding: 20px; overflow-y: auto; font-size: 13px; line-height: 1.6; color: #323130; }
  .modal-body p { margin-bottom: 10px; }
  .modal-body p:last-child { margin-bottom: 0; }
  .modal-body strong { color: #0078D4; }
  /* Product edit modal */
  .edit-modal { width: 680px; }
  .edit-modal .modal-body { max-height: 72vh; overflow-y: auto; }
  .edit-field { margin-bottom: 14px; }
  .edit-field label { display: block; font-size: 12px; font-weight: 600; color: #605E5C; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.03em; }
  .edit-field input, .edit-field select { width: 100%; padding: 6px 10px; border: 1px solid #C8C6C4; border-radius: 2px; font-size: 13px; box-sizing: border-box; }
  .edit-field input:focus, .edit-field select:focus { outline: none; border-color: #0078D4; box-shadow: 0 0 0 1px #0078D4; }
  .edit-field input.highlight { border-color: #0078D4; background: #EFF6FC; }
  .edit-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 20px; border-top: 1px solid #EDEBE9; flex-shrink: 0; }
  .edit-msg { font-size: 12px; padding: 4px 0; }
  .clickable-row { cursor: pointer; }
  .clickable-row:hover td { background: #F3F2F1; }

  /* Info button */
  .cg-btn { background:#fff; border:1px solid #C8C6C4; border-radius:2px; padding:4px 12px;
             font-size:12px; cursor:pointer; color:#323130; }
  .cg-btn:hover { background:#F3F2F1; }
  .cg-btn.cg-active { background:#0078D4; color:#fff; border-color:#0078D4; font-weight:600; }
  .info-btn { background: none; border: 1px solid #C8D6E5; border-radius: 50%; width: 20px; height: 20px;
              font-size: 11px; cursor: pointer; color: #0078D4; font-weight: 700; line-height: 18px;
              text-align: center; margin-left: 8px; vertical-align: middle; display: inline-block; }
  .info-btn:hover { background: #DEECF9; }

  /* Section */
  .content-section { display: none; }
  .content-section.active { display: block; }

  /* Back button */
  .back-btn { background: none; border: 1px solid #8A8886; padding: 5px 12px; border-radius: 2px;
              cursor: pointer; font-size: 12px; color: #605E5C; margin-bottom: 16px; }
  .back-btn:hover { background: #F3F2F1; }

  /* No sidebar for retailer tab */
  .no-sidebar .sidebar { display: none; }
</style>
</head>
<body>

<div class="tab-bar">
  <span class="tab-bar-title">Competition Analysis</span>
  <div class="tab active" id="tab-stic" onclick="switchTab('stic')">STIC</div>
  <div class="tab" id="tab-retailer" onclick="switchTab('retailer')">Retailer</div>
  <div class="tab" id="tab-catalogue" onclick="switchTab('catalogue')">Catalogue</div>
</div>

<!-- STIC layout -->
<div class="layout" id="layout-stic">
  <div class="sidebar" id="sidebar-stic">
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Overview <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn active" onclick="showSticSection('overview',this)">Daily Overview</button>
        <button class="sidebar-btn" onclick="showSticSection('search',this)">Search SKUs</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Stock Intelligence <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadWatchlistReport(this)">★ Watched SKUs</button>
        <button class="sidebar-btn" onclick="loadReport('no_channel_stock',this)">No channel stock 5+ days</button>
        <button class="sidebar-btn" onclick="loadReport('back_in_stock',this)">Back in stock</button>
        <button class="sidebar-btn" onclick="loadReport('single_distributor',this)">Single distributor</button>
        <button class="sidebar-btn" onclick="loadReport('new_stock_arrival',this)">New stock arrival</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        VIP Performance <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadReport('vip_out_on_price',this)">VIP out on price</button>
        <button class="sidebar-btn" onclick="loadReport('vip_static',this)">VIP static market moving</button>
        <button class="sidebar-btn" onclick="loadReport('vip_exclusive',this)">VIP exclusive</button>
        <button class="sidebar-btn" onclick="loadReport('vip_price_gap',this)">VIP price gap</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Market Opportunities <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadReport('never_stocked',this)">No channel stock ever</button>
        <button class="sidebar-btn" onclick="loadReport('price_dropping',this)">Price dropping</button>
        <button class="sidebar-btn" onclick="loadReport('price_rising',this)">Price rising</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Daily Changes <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadReport('daily_changes',this)">Changes since yesterday</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Investigate <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadInvestigateReport(this)">🔍 Investigate</button>
        <button class="sidebar-btn" onclick="loadProbeSkus(this)">🔬 Probe SKUs</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Scraper <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadScrapeGroups(this)">⟳ Refresh SKUs</button>
        <button class="sidebar-btn" onclick="loadMissingResults(this)">❌ Missing Results</button>
      </div>
    </div>
  </div>

  <div class="main" id="main-stic">
    <!-- Overview -->
    <div class="content-section active" id="stic-overview">
      <div id="stic-overview-cards" class="overview-cards"><div class="spinner">Loading…</div></div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
        <span class="section-title" style="margin-bottom:0">Chipset Daily Overview</span>
        <div style="display:flex;gap:4px;margin-left:12px">
          <button id="cg-mbrd"   class="cg-btn cg-active" onclick="switchChipsetGroup('mbrd')">Motherboard</button>
          <button id="cg-server" class="cg-btn"            onclick="switchChipsetGroup('server')">Server</button>
          <button id="cg-gpu"    class="cg-btn"            onclick="switchChipsetGroup('gpu')">VGA</button>
        </div>
      </div>
      <div class="tbl-wrap" id="stic-chipset-tbl"><div class="spinner">Loading…</div></div>
      <div id="stic-chipset-drill" style="display:none;margin-top:4px">
        <div class="drill-header">
          <span id="stic-chipset-drill-title" class="section-title" style="margin:0"></span>
          <button class="drill-close" onclick="closeChipsetDrill()">✕ Close</button>
        </div>
        <div class="tbl-wrap" id="stic-chipset-drill-tbl"></div>
      </div>
    </div>

    <!-- Search -->
    <div class="content-section" id="stic-search">
      <div class="search-bar">
        <input id="stic-search-input" type="text" placeholder="Search by VIP code, model number, or description…" onkeydown="if(event.key==='Enter')doSticSearch()">
        <button onclick="doSticSearch()">Search</button>
      </div>
      <div id="stic-search-results"></div>
    </div>

    <!-- SKU Drill-down -->
    <div class="content-section" id="stic-sku">
      <button class="back-btn" id="stic-sku-back" onclick="showSticSection('overview')">← Back</button>
      <div id="stic-sku-content"><div class="spinner">Loading…</div></div>
    </div>

    <!-- Report results -->
    <div class="content-section" id="stic-report">
      <button class="back-btn" onclick="showSticSection('overview')">← Back to Overview</button>
      <div id="stic-report-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Investigate -->
    <div class="content-section" id="stic-investigate">
      <div id="stic-investigate-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Probe SKUs -->
    <div class="content-section" id="stic-probe">
      <div id="stic-probe-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Scrape Groups -->
    <div class="content-section" id="stic-scrape">
      <div id="stic-scrape-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Missing Results -->
    <div class="content-section" id="stic-missing">
      <div id="stic-missing-content"><div class="spinner">Loading…</div></div>
    </div>
  </div>
</div>

<!-- Retailer layout -->
<div class="layout" id="layout-retailer" style="display:none">
  <div class="sidebar" id="sidebar-retailer">
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Overview <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn active" id="ret-btn-overview" onclick="showRetSection('overview',this)">Daily Overview</button>
        <button class="sidebar-btn" onclick="showRetSection('search',this)">Search SKUs</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Stock Intelligence <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadRetReport('out_of_stock',this)">Out of Stock Today</button>
        <button class="sidebar-btn" onclick="loadRetReport('back_in_stock',this)">Back in Stock</button>
        <button class="sidebar-btn" onclick="loadRetReport('never_listed',this)">Never Listed</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Price Intelligence <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadRetReport('price_trends',this)">Price Trends — Top Movers (14d)</button>
        <button class="sidebar-btn" onclick="loadRetReport('price_dropping',this)">Price Dropping</button>
        <button class="sidebar-btn" onclick="loadRetReport('price_rising',this)">Price Rising</button>
        <button class="sidebar-btn" onclick="loadRetReport('price_gaps',this)">Price Gaps Between Retailers</button>
        <button class="sidebar-btn" onclick="loadRetReport('daily_changes',this)">Changes Since Yesterday</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        MSRP Analysis <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadRetReport('below_msrp',this)">Below MSRP</button>
        <button class="sidebar-btn" onclick="loadRetReport('above_msrp',this)">All Retailers Above MSRP</button>
        <button class="sidebar-btn" onclick="loadRetReport('msrp_gap',this)">Furthest from MSRP</button>
      </div>
    </div>
  </div>

  <div class="main" id="main-retailer">
    <!-- Overview / KPI -->
    <div class="content-section active" id="ret-overview">
      <div id="retailer-kpi" class="kpi-row"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Search -->
    <div class="content-section" id="ret-search">
      <div class="search-bar">
        <input id="ret-search-input" type="text" placeholder="Search by model number or description…" onkeydown="if(event.key==='Enter')doRetSearch()">
        <button onclick="doRetSearch()">Search</button>
      </div>
      <div id="ret-search-results"></div>
    </div>
    <!-- SKU drill-down -->
    <div class="content-section" id="ret-sku">
      <button class="back-btn" id="ret-sku-back" onclick="showRetSection('overview')">← Back</button>
      <div id="ret-sku-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Report results -->
    <div class="content-section" id="ret-report">
      <button class="back-btn" onclick="showRetSection('overview')">← Back to Overview</button>
      <div id="ret-report-content"><div class="spinner">Loading…</div></div>
    </div>
  </div>
</div>

<!-- Catalogue layout -->
<div class="layout" id="layout-catalogue" style="display:none">
  <div class="sidebar" id="sidebar-catalogue">
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Products <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadCatProducts(this)">📦 View / Search SKUs</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'new-skus')">📥 Add / Update SKUs</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'eol-status')">🔄 Update EOL Status</button>
        <button class="sidebar-btn" onclick="loadCatEOL(this)">⛔ View EOL SKUs</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'export-skus')">📤 Export Active SKUs</button>
        <button class="sidebar-btn" onclick="loadMissingEan(this)">⚠️ Missing EAN Report</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Retailers <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadCatRetailerIds(this)">🔗 View Retailer IDs</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'retailer-ids-import')">📥 Import Retailer IDs</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'retailer-ids-export')">📤 Export Retailer IDs</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        MSRP <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'msrp-by-vip')">💷 Import by VIP Code</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'msrp-by-ean')">💷 Import by EAN</button>
        <button class="sidebar-btn" onclick="loadCatImportExport(this,'msrp-by-model')">💷 Import by Model</button>
        <button class="sidebar-btn" onclick="loadMissingMsrp(this)">⚠️ Missing MSRP Report</button>
      </div>
    </div>
  </div>
  <div class="main" id="main-catalogue">
    <!-- Products view -->
    <div class="content-section active" id="cat-products">
      <div id="cat-products-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- EOL view -->
    <div class="content-section" id="cat-eol">
      <div id="cat-eol-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Import / Export -->
    <div class="content-section" id="cat-import-export">
      <div id="cat-import-export-content"></div>
    </div>
    <!-- Retailer IDs view -->
    <div class="content-section" id="cat-retailer-ids">
      <div id="cat-retailer-ids-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Missing MSRP report -->
    <div class="content-section" id="cat-missing-msrp">
      <div id="cat-missing-msrp-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Missing EAN report -->
    <div class="content-section" id="cat-missing-ean">
      <div id="cat-missing-ean-content"><div class="spinner">Loading…</div></div>
    </div>
  </div>
</div>

<!-- Info modal -->
<div class="modal-backdrop" id="info-modal" onclick="if(event.target===this)closeHelp()">
  <div class="modal">
    <div class="modal-header">
      <h3 id="modal-title">Report Info</h3>
      <button class="modal-close" onclick="closeHelp()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<!-- Purge dates modal -->
<div class="modal-backdrop" id="purge-dates-modal" onclick="if(event.target===this)closePurgeDatesModal()">
  <div class="modal" style="width:420px">
    <div class="modal-header">
      <h3 id="pdm-title">Purge dates</h3>
      <button class="modal-close" onclick="closePurgeDatesModal()">✕</button>
    </div>
    <div class="modal-body" style="padding:16px 20px">
      <p id="pdm-desc" style="font-size:12px;color:#605E5C;margin:0 0 12px">Select individual dates to delete. Retailer ID is left untouched.</p>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button onclick="pdmSelectAll(true)"  style="font-size:11px;padding:3px 8px;border:1px solid #C8C6C4;border-radius:2px;cursor:pointer">All</button>
        <button onclick="pdmSelectAll(false)" style="font-size:11px;padding:3px 8px;border:1px solid #C8C6C4;border-radius:2px;cursor:pointer">None</button>
      </div>
      <div id="pdm-dates" style="max-height:280px;overflow-y:auto;border:1px solid #E1E1E1;border-radius:3px;padding:8px"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button onclick="closePurgeDatesModal()" style="padding:6px 14px;border:1px solid #C8C6C4;border-radius:2px;cursor:pointer;background:none">Cancel</button>
        <button id="pdm-confirm-btn" onclick="confirmPurgeDates()" style="padding:6px 14px;border:none;border-radius:2px;cursor:pointer;background:#A4262C;color:#fff;font-weight:600">Purge Selected</button>
      </div>
    </div>
  </div>
</div>

<!-- Product edit modal -->
<div class="modal-backdrop" id="edit-modal" onclick="if(event.target===this)_closeEditModal()">
  <div class="modal edit-modal">
    <div class="modal-header">
      <h3 id="edit-modal-title">Edit Product</h3>
      <button class="modal-close" onclick="_closeEditModal()">✕</button>
    </div>
    <div class="modal-body" style="padding:16px 20px">
      <input type="hidden" id="ep-product-id">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 16px">
        <div class="edit-field" style="grid-column:1/-1">
          <label>Model</label>
          <input type="text" id="ep-model-no">
        </div>
        <div class="edit-field">
          <label>Manufacturer</label>
          <input type="text" id="ep-manufacturer">
        </div>
        <div class="edit-field">
          <label>Product Group</label>
          <select id="ep-product-group">
            <option value="PROD_VIDEO">GPU (PROD_VIDEO)</option>
            <option value="PROD_MBRD">Motherboard (PROD_MBRD)</option>
            <option value="PROD_MBRDS">Server/Pro (PROD_MBRDS)</option>
          </select>
        </div>
        <div class="edit-field" style="grid-column:1/-1">
          <label>Description</label>
          <input type="text" id="ep-description">
        </div>
        <div class="edit-field">
          <label>Chipset</label>
          <input type="text" id="ep-chipset">
        </div>
        <div class="edit-field">
          <label>EAN</label>
          <input type="text" id="ep-ean" placeholder="13-digit EAN">
        </div>
        <div class="edit-field">
          <label>MSRP (£)</label>
          <input type="number" id="ep-msrp" step="0.01" min="0" placeholder="0.00">
        </div>
      </div>

      <div style="margin:16px 0 12px;border-top:1px solid #EDEBE9;padding-top:14px">
        <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#605E5C;margin-bottom:12px">Retailer IDs &amp; URLs</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 16px">
          <div class="edit-field">
            <label>Amazon ASIN</label>
            <input type="text" id="ep-amazon-asin" placeholder="e.g. B0XXXXXXXX">
          </div>
          <div class="edit-field">
            <label>Currys SKU</label>
            <input type="text" id="ep-currys-sku" placeholder="e.g. 10XXXXXX">
          </div>
          <div class="edit-field">
            <label>Argos SKU</label>
            <input type="text" id="ep-argos-sku" placeholder="e.g. 7629329">
          </div>
          <div class="edit-field">
            <label>Overclockers Code</label>
            <input type="text" id="ep-ocuk-code" placeholder="e.g. MSI-XXX-XXX">
          </div>
          <div class="edit-field">
            <label>Very SKU</label>
            <input type="text" id="ep-very-sku" placeholder="SKU code">
          </div>
          <div class="edit-field">
            <label>Scan LN Code</label>
            <input type="text" id="ep-scan-ln" placeholder="e.g. LN12345">
          </div>
          <div class="edit-field" style="grid-column:1/-1">
            <label>Very URL</label>
            <input type="text" id="ep-very-url" placeholder="https://www.very.co.uk/...">
          </div>
          <div class="edit-field" style="grid-column:1/-1">
            <label>Scan URL</label>
            <input type="text" id="ep-scan-url" placeholder="https://www.scan.co.uk/products/...">
          </div>
          <div class="edit-field" style="grid-column:1/-1">
            <label>AWD-IT URL</label>
            <input type="text" id="ep-awdit-url" placeholder="https://www.awd-it.co.uk/...">
          </div>
          <div class="edit-field" style="grid-column:1/-1">
            <label>CCL Online URL</label>
            <input type="text" id="ep-ccl-url" placeholder="https://www.cclonline.com/...">
          </div>
          <div class="edit-field" style="grid-column:1/-1">
            <label>Box URL</label>
            <input type="text" id="ep-box-url" placeholder="https://www.box.co.uk/...">
          </div>
        </div>
      </div>

      <div id="ep-msg" class="edit-msg"></div>
    </div>
    <div class="edit-footer">
      <button onclick="_closeEditModal()" style="padding:6px 16px;border:1px solid #C8C6C4;background:#fff;border-radius:2px;font-size:13px;cursor:pointer">Cancel</button>
      <button onclick="_saveProduct()" style="padding:6px 16px;background:#0078D4;color:#fff;border:none;border-radius:2px;font-size:13px;cursor:pointer;font-weight:600">Save</button>
    </div>
  </div>
</div>

<script>
// ── Date formatting ───────────────────────────────────────────────────────────
function fmtDate(d) {
  if (!d || d === '—') return d;
  // "2026-04-22" → "22/04/26"
  const parts = d.split('-');
  if (parts.length !== 3) return d;
  return `${parts[2]}/${parts[1]}/${parts[0].slice(2)}`;
}

// ── Sortable table utility ────────────────────────────────────────────────────
// makeSortable(tableOrId)  — call after any innerHTML that contains a <table>
// makeSortableAll(container) — applies to every <table> inside the element
//
// Cells with complex HTML (badges, icons, links) can set data-val on the <td>
// for an explicit sort key. Otherwise textContent is used with smart extraction:
//   £1,234.56  →  1234.56 (numeric)
//   +12.5%     →  12.5    (numeric)
//   ✓ / ✗      →  1 / 0  (in-stock)
//   ↗          →  1       (has link)
//   — / ''     →  null    (always sorts last)

function _cellSortVal(cell) {
  if (!cell) return null;
  const raw = (cell.dataset.val !== undefined
    ? cell.dataset.val
    : cell.textContent).trim();
  if (!raw || raw === '—' || raw === '?' || raw === '…') return null;
  if (raw === '✓' || raw === '↗') return 1;
  if (raw === '✗') return 0;
  // Strip currency / percent / sign / commas and try numeric
  const stripped = raw.replace(/[£%+,\s]/g, '');
  const n = parseFloat(stripped);
  if (!isNaN(n) && stripped !== '') return n;
  return raw.toLowerCase();
}

function _thSetArrow(th) {
  th.textContent = (th.dataset.label || '') +
    (th._sortDir === 1 ? ' ▲' : th._sortDir === -1 ? ' ▼' : '');
  th.style.color = th._sortDir ? '#0078D4' : '';
}

function makeSortable(tableOrId) {
  const tbl = typeof tableOrId === 'string'
    ? document.getElementById(tableOrId) : tableOrId;
  if (!tbl || tbl._sortable) return;
  tbl._sortable = true;
  const ths = Array.from(tbl.querySelectorAll('thead tr:first-child th'));
  ths.forEach((th, colIdx) => {
    th.dataset.label   = th.textContent.trim();
    th.style.cursor    = 'pointer';
    th.style.userSelect = 'none';
    th.style.whiteSpace = 'nowrap';
    th._sortDir = 0;
    th.addEventListener('click', () => {
      const prev = th._sortDir;
      ths.forEach(h => { h._sortDir = 0; _thSetArrow(h); });
      th._sortDir = (prev === 1) ? -1 : 1;
      _thSetArrow(th);
      const tbody = tbl.querySelector('tbody');
      if (!tbody) return;
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const dir  = th._sortDir;
      rows.sort((a, b) => {
        const av = _cellSortVal(a.cells[colIdx]);
        const bv = _cellSortVal(b.cells[colIdx]);
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
        if (typeof av === 'number') return -dir;
        if (typeof bv === 'number') return  dir;
        return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
}

function makeSortableAll(container) {
  (container || document).querySelectorAll('table').forEach(t => makeSortable(t));
}

// ── CSV export utility ────────────────────────────────────────────────────────
// Reads visible rows from any rendered <table> and triggers a CSV download.
// Hidden rows (display:none from a filter) are skipped.
function _exportTableCsv(tableId, filename) {
  const tbl = document.getElementById(tableId);
  if (!tbl) return;
  const rows = [];
  // Headers — strip sort arrows
  const ths = tbl.querySelectorAll('thead th');
  rows.push([...ths].map(th => (th.dataset.label || th.textContent).replace(/ [▲▼]$/, '').trim()));
  // Body — visible rows only
  tbl.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.style.display === 'none') return;
    rows.push([...tr.cells].map(td => {
      // Use data-val if present, otherwise text; skip icon-only cells cleanly
      const v = (td.dataset.val !== undefined ? td.dataset.val : td.textContent).trim();
      return v === '↗' ? '' : v;
    }));
  });
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\r\n');
  const a   = document.createElement('a');
  a.href    = 'data:text/csv;charset=utf-8,' + encodeURIComponent('﻿' + csv);
  a.download = filename;
  a.click();
}

// ── Tab switching ─────────────────────────────────────────────────────────────
let currentTab = 'stic';
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.getElementById('layout-stic').style.display      = (tab === 'stic')      ? 'flex' : 'none';
  document.getElementById('layout-retailer').style.display  = (tab === 'retailer')  ? 'flex' : 'none';
  document.getElementById('layout-catalogue').style.display = (tab === 'catalogue') ? 'flex' : 'none';
  if (tab === 'retailer'  && !retailerKpiLoaded)  loadRetailerKpi();
  if (tab === 'catalogue' && !catProductsLoaded)  loadCatProducts();
}

// ── Catalogue section management ──────────────────────────────────────────────
let catProductsLoaded = false;
function showCatSection(name, btn) {
  document.querySelectorAll('#main-catalogue .content-section').forEach(s => s.classList.remove('active'));
  document.getElementById('cat-' + name).classList.add('active');
  document.querySelectorAll('#sidebar-catalogue .sidebar-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

// ── Sidebar section collapse ──────────────────────────────────────────────────
function toggleSection(hdr) {
  hdr.classList.toggle('collapsed');
  hdr.nextElementSibling.classList.toggle('hidden');
}

// ── STIC section management ───────────────────────────────────────────────────
function showSticSection(name, btn) {
  document.querySelectorAll('#main-stic .content-section').forEach(s => s.classList.remove('active'));
  document.getElementById('stic-' + name).classList.add('active');
  document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

// ── STIC KPI & Overview ───────────────────────────────────────────────────────
function loadSticOverview() {
  const container = document.getElementById('stic-overview-cards');
  let kpiData = null, groupData = null;

  function _renderOverviewCards() {
    if (!kpiData || !groupData) return;
    const today = new Date().toISOString().slice(0,10);
    const yesterday = new Date(Date.now()-864e5).toISOString().slice(0,10);
    let html = `
      <div class="kpi-card"><div class="label">SKUs Tracked</div><div class="value">${kpiData.total_skus}</div><div class="sub">products</div></div>
      <div class="kpi-card"><div class="label">SKUs In Stock</div><div class="value">${kpiData.skus_in_stock}</div><div class="sub">of ${kpiData.total_skus} tracked</div></div>
      <div class="kpi-card"><div class="label">SKUs No Stock</div><div class="value">${kpiData.skus_no_stock}</div><div class="sub">zero channel inventory</div></div>`;
    groupData.forEach(g => {
      const pct = g.sku_count > 0 ? g.scraped_count / g.sku_count : 0;
      const isToday = g.last_scraped === today;
      const dotCls = !isToday ? 'tl-red' : pct >= 0.90 ? 'tl-green' : pct >= 0.75 ? 'tl-amber' : 'tl-red';
      const dateLabel = !g.last_scraped ? 'Never' : isToday ? 'Today'
        : g.last_scraped === yesterday ? 'Yesterday' : fmtDate(g.last_scraped);
      const failed = g.sku_count - g.scraped_count;
      const failTxt = failed > 0 ? `<span style="color:#D13438">(${failed} failed)</span>` : '';
      html += `<div class="kpi-card">
        <span class="tl-dot ${dotCls}"></span>
        <div class="label">${g.label}</div>
        <div class="value" style="font-size:16px">${dateLabel}</div>
        <div class="sub">${g.scraped_count} / ${g.sku_count} SKUs ${failTxt}</div>
      </div>`;
    });
    container.innerHTML = html;
  }

  fetch('/api/stic/kpi').then(r=>r.json()).then(d => { kpiData = d; _renderOverviewCards(); });
  fetch('/api/scrape/groups').then(r=>r.json()).then(d => { groupData = d; _renderOverviewCards(); });

  loadChipsetOverview('mbrd');
}

// ── Watchlist ─────────────────────────────────────────────────────────────────
let _watchedIds = new Set();

function loadWatchlist() {
  fetch('/api/watchlist').then(r=>r.json()).then(data => {
    _watchedIds = new Set(data.ids);
    _refreshAllStars();
  });
}

function _refreshAllStars() {
  document.querySelectorAll('[data-watch-pid]').forEach(el => {
    const pid = +el.dataset.watchPid;
    _applyStarState(el, _watchedIds.has(pid));
  });
}

function _applyStarState(el, watched) {
  el.classList.toggle('watched', watched);
  el.title = watched ? 'Remove from watchlist' : 'Add to watchlist';
  el.textContent = watched ? '★' : '☆';
}

function toggleWatch(pid, event) {
  if (event) event.stopPropagation();
  const watching = _watchedIds.has(pid);
  const method   = watching ? 'DELETE' : 'POST';
  fetch('/api/watchlist/' + pid, { method }).then(r=>r.json()).then(data => {
    if (data.watched) _watchedIds.add(pid); else _watchedIds.delete(pid);
    _refreshAllStars();
  });
}

function watchStarHtml(pid, cls) {
  const w = _watchedIds.has(pid);
  return `<button class="${cls||'watch-star'}${w?' watched':''}" data-watch-pid="${pid}"
    onclick="toggleWatch(${pid},event)" title="${w?'Remove from watchlist':'Add to watchlist'}">${w?'★':'☆'}</button>`;
}

// ── EOL state ────────────────────────────────────────────────────────────────
let _eolIds = new Set();

function loadEOLState() {
  fetch('/api/eol').then(r=>r.json()).then(data => {
    _eolIds = new Set(data.products.map(p => p.product_id));
    _refreshAllEolBtns();
  });
}

function _refreshAllEolBtns() {
  document.querySelectorAll('[data-eol-pid]').forEach(btn => {
    const pid = parseInt(btn.dataset.eolPid);
    const isEol = _eolIds.has(pid);
    btn.textContent = isEol ? '✕ EOL' : 'Mark EOL';
    btn.classList.toggle('is-eol', isEol);
    btn.title = isEol ? 'Remove EOL — will resume scraping next cycle' : 'Mark as End of Life — scraper will skip';
  });
}

function toggleEOL(pid, event) {
  if (event) event.stopPropagation();
  const marking = !_eolIds.has(pid);
  const label = marking ? 'Mark as EOL?' : 'Remove EOL status?';
  if (!confirm(label + '\n\nProduct: ' + pid)) return;
  const method = marking ? 'POST' : 'DELETE';
  fetch('/api/eol/' + pid, { method }).then(r=>r.json()).then(data => {
    if (data.eol) _eolIds.add(pid); else _eolIds.delete(pid);
    _refreshAllEolBtns();
    // Refresh EOL section if it's currently visible
    const eolSection = document.getElementById('cat-eol');
    if (eolSection && eolSection.classList.contains('active')) loadCatEOL();
  });
}

function eolBtnHtml(pid) {
  const isEol = _eolIds.has(pid);
  return `<button class="eol-btn${isEol?' is-eol':''}" data-eol-pid="${pid}"
    onclick="toggleEOL(${pid},event)"
    title="${isEol?'Remove EOL — will resume scraping next cycle':'Mark as End of Life — scraper will skip'}"
    >${isEol?'✕ EOL':'Mark EOL'}</button>`;
}

function saveSticUrl(pid) {
  // Support both the SKU page input (stic-url-input-<pid>) and the missing-results inline input (miss-url-<pid>)
  const input = document.getElementById('stic-url-input-' + pid)
             || document.getElementById('miss-url-' + pid);
  const url = input ? input.value.trim() : '';
  if (!url) { alert('Please paste a STIC URL first'); return; }
  const btn = input ? input.nextElementSibling : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  fetch('/api/stic-url/' + pid, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  }).then(r => r.json()).then(data => {
    if (data.saved) {
      // If we're on the SKU detail page, reload it; if on missing results, just confirm in-place
      const missingActive = document.getElementById('stic-missing')?.classList.contains('active');
      if (missingActive) {
        if (btn) { btn.disabled = false; btn.textContent = '✓ Saved'; btn.style.background='#107C10'; }
      } else {
        loadSticSku(pid, sticSkuBackSection || 'overview');
      }
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
      alert('Save failed: ' + (data.error || 'unknown error'));
    }
  }).catch(() => {
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
    alert('Network error — URL not saved.');
  });
}

function saveNotes(pid) {
  const ta = document.getElementById('sku-notes-' + pid);
  if (!ta) return;
  const notes = ta.value.trim();
  fetch('/api/notes/' + pid, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ notes })
  }).then(r=>r.json()).then(() => {
    const btn = ta.nextElementSibling;
    if (btn) { const orig = btn.textContent; btn.textContent = '✓ Saved'; btn.style.background='#107C10';
      setTimeout(()=>{ btn.textContent=orig; btn.style.background='#0078D4'; }, 1500); }
  }).catch(() => alert('Network error — notes not saved.'));
}

function clearNotes(pid) {
  const ta = document.getElementById('sku-notes-' + pid);
  if (!ta || !ta.value.trim()) return;
  if (!confirm('Clear notes for this SKU?')) return;
  ta.value = '';
  fetch('/api/notes/' + pid, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ notes: '' })
  }).catch(() => alert('Network error — notes not cleared.'));
}

function triggerRescrape(pid) {
  const btn = document.getElementById('rescrape-btn-' + pid);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Scraping…'; }
  fetch('/api/scrape/sku', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ product_id: pid })
  }).then(r => r.json()).then(data => {
    if (!data.started) {
      if (btn) { btn.disabled = false; btn.textContent = '▶ Scrape Now'; }
      alert('Could not start scrape: ' + (data.error || 'unknown error'));
      return;
    }
    // Poll until done, then reload the SKU page
    const interval = setInterval(() => {
      fetch('/api/scrape/sku/status?product_id=' + pid).then(r => r.json()).then(s => {
        if (s.done) {
          clearInterval(interval);
          if (btn) { btn.disabled = false; btn.textContent = '▶ Scrape Now'; }
          loadSticSku(pid, currentSection);  // refresh product data
        }
      }).catch(() => clearInterval(interval));
    }, 3000);
    // Safety timeout — stop polling after 2 minutes regardless
    setTimeout(() => {
      clearInterval(interval);
      if (btn) { btn.disabled = false; btn.textContent = '▶ Scrape Now'; }
    }, 120000);
  }).catch(() => {
    if (btn) { btn.disabled = false; btn.textContent = '▶ Scrape Now'; }
    alert('Network error — could not start scrape.');
  });
}

function loadWatchlistReport(btn) {
  if (currentSidebarBtn) currentSidebarBtn.classList.remove('active');
  if (btn) { btn.classList.add('active'); currentSidebarBtn = btn; }
  showSticSection('report');
  document.getElementById('stic-report-content').innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/watchlist/report').then(r=>r.json()).then(data => {
    renderWatchlistReport(data);
  });
}

function renderWatchlistReport(data) {
  const el = document.getElementById('stic-report-content');
  if (!data.rows || !data.rows.length) {
    el.innerHTML = `<div class="section-title">★ Watched SKUs</div>
      <p style="color:#A19F9D;padding:20px 0">No SKUs on your watchlist yet. Open any SKU and click the ★ to start tracking it.</p>`;
    return;
  }

  const dists = data.distributors.filter(d => data.rows.some(r => r.distributors[d] !== undefined));
  let html = `<div class="section-title">★ Watched SKUs <span style="font-size:12px;font-weight:400;color:#605E5C">(${data.rows.length} SKUs — ${fmtDate(data.date)})</span></div>`;
  html += '<div class="tbl-wrap"><table><thead><tr>';
  html += '<th></th><th>Product</th><th>Model</th><th>Manufacturer</th>';
  dists.forEach(d => html += `<th>${d}</th>`);
  html += '<th>Total</th><th>Δ Yesterday</th></tr></thead><tbody>';

  data.rows.forEach(r => {
    const delta     = r.delta;
    const deltaFmt  = delta === 0 ? '<span style="color:#A19F9D">—</span>'
                    : delta > 0   ? `<span style="color:#107C10;font-weight:600">+${delta}</span>`
                    :               `<span style="color:#D13438;font-weight:600">${delta}</span>`;
    html += `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
      <td class="wstar">${watchStarHtml(r.product_id)}</td>
      <td>${r.product_id}</td>
      <td>${r.model_no}</td>
      <td>${r.manufacturer}</td>`;
    dists.forEach(d => {
      const qty = (r.distributors[d] || {}).today || 0;
      html += `<td>${qty > 0 ? qty.toLocaleString() : '<span style="color:#C8C6C4">0</span>'}</td>`;
    });
    html += `<td><strong>${r.total_today.toLocaleString()}</strong></td><td>${deltaFmt}</td></tr>`;
  });

  html += '</tbody></table></div>';
  el.innerHTML = html;
  makeSortableAll(el);
  _refreshAllStars();
}

let _chipsetGroup  = 'mbrd';
let _chipsetActive = null;
let _cgRows        = [];

function switchChipsetGroup(group) {
  _chipsetGroup  = group;
  _chipsetActive = null;
  ['mbrd','server','gpu'].forEach(g => {
    document.getElementById('cg-'+g).classList.toggle('cg-active', g === group);
  });
  document.getElementById('stic-chipset-drill').style.display = 'none';
  loadChipsetOverview(group);
}

function loadChipsetOverview(group) {
  _chipsetGroup = group;
  document.getElementById('stic-chipset-tbl').innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/stic/chipset-overview?group=' + group).then(r=>r.json()).then(rows => {
    _cgRows = rows;
    if (!rows.length) { document.getElementById('stic-chipset-tbl').innerHTML = '<p style="color:#A19F9D;padding:20px">No data</p>'; return; }
    const cols = ['Chipset','VIP SKUs','Channel Floor £','VIP Lowest £','VIP vs Floor','Channel Stock'];
    let html = '<table><thead><tr>' + cols.map(c=>`<th>${c}</th>`).join('') + '</tr></thead><tbody>';
    rows.forEach((r, i) => {
      const diff = (r.vip_price && r.floor_price) ? ((r.vip_price - r.floor_price) / r.floor_price * 100).toFixed(1) : null;
      const diffBadge = diff === null ? '' : diff > 5 ? `<span class="badge badge-red">+${diff}%</span>` : diff > 0 ? `<span class="badge badge-orange">+${diff}%</span>` : `<span class="badge badge-green">${diff}%</span>`;
      const sel = r.chipset === _chipsetActive ? ' row-selected' : '';
      html += `<tr class="clickable${sel}" data-ci="${i}" title="Click to view SKUs">
        <td><strong>${r.chipset}</strong></td>
        <td>${r.vip_skus}</td>
        <td>${r.floor_price ? '£'+r.floor_price.toFixed(2) : '—'}</td>
        <td>${r.vip_price ? '£'+r.vip_price.toFixed(2) : '—'}</td>
        <td>${diffBadge}</td>
        <td>${(r.channel_stock||0).toLocaleString()}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    const _chipsetTblEl = document.getElementById('stic-chipset-tbl');
    _chipsetTblEl.innerHTML = html;
    makeSortableAll(_chipsetTblEl);
    document.querySelectorAll('#stic-chipset-tbl tr[data-ci]').forEach(tr => {
      tr.addEventListener('click', () => {
        const cs = _cgRows[+tr.dataset.ci].chipset;
        toggleChipsetDrill(tr, cs);
      });
    });
    // Re-open drill if one was already active
    if (_chipsetActive) loadChipsetDrill(_chipsetActive);
  });
}

function toggleChipsetDrill(tr, chipset) {
  if (_chipsetActive === chipset) { closeChipsetDrill(); return; }
  document.querySelectorAll('#stic-chipset-tbl tr.row-selected').forEach(r => r.classList.remove('row-selected'));
  tr.classList.add('row-selected');
  _chipsetActive = chipset;
  loadChipsetDrill(chipset);
}

function closeChipsetDrill() {
  _chipsetActive = null;
  document.getElementById('stic-chipset-drill').style.display = 'none';
  document.querySelectorAll('#stic-chipset-tbl tr.row-selected').forEach(r => r.classList.remove('row-selected'));
}

function loadChipsetDrill(chipset) {
  const drillEl = document.getElementById('stic-chipset-drill');
  drillEl.style.display = 'block';
  document.getElementById('stic-chipset-drill-title').textContent = chipset + ' — SKUs';
  document.getElementById('stic-chipset-drill-tbl').innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/stic/chipset-skus?group=' + _chipsetGroup + '&chipset=' + encodeURIComponent(chipset))
    .then(r=>r.json()).then(rows => {
      if (!rows.length) {
        document.getElementById('stic-chipset-drill-tbl').innerHTML = '<p style="color:#A19F9D;padding:20px">No SKUs found</p>';
        return;
      }
      let html = '<table><thead><tr><th class="wstar"></th><th>Product</th><th>Model</th><th>Manufacturer</th><th>Channel Stock</th><th>VIP Stock</th><th>Floor £</th><th>VIP £</th></tr></thead><tbody>';
      rows.forEach(r => {
        html += `<tr class="clickable" onclick="loadSticSku(${r.product_id},'overview')" title="Click for full SKU detail">
          <td class="wstar">${watchStarHtml(r.product_id)}</td>
          <td>${r.product_id}</td>
          <td>${r.model_no}</td>
          <td>${r.manufacturer}</td>
          <td>${(r.channel_stock||0).toLocaleString()}</td>
          <td>${(r.vip_stock||0).toLocaleString()}</td>
          <td>${r.floor_price ? '£'+r.floor_price.toFixed(2) : '—'}</td>
          <td>${vipCell(r.vip_price, r.floor_price)}</td>
        </tr>`;
      });
      html += '</tbody></table>';
      const _drillEl = document.getElementById('stic-chipset-drill-tbl');
      _drillEl.innerHTML = html;
      makeSortableAll(_drillEl);
    });
}

// ── STIC search ───────────────────────────────────────────────────────────────
function doSticSearch() {
  const q = document.getElementById('stic-search-input').value.trim();
  if (!q) return;
  showSticSection('search');
  document.getElementById('stic-search-results').innerHTML = '<div class="spinner">Searching…</div>';
  fetch('/api/stic/search?q=' + encodeURIComponent(q)).then(r=>r.json()).then(rows => {
    if (!rows.length) { document.getElementById('stic-search-results').innerHTML = '<p style="color:#A19F9D;padding:20px">No results</p>'; return; }
    let html = '<div class="section-title">Results (' + rows.length + ')</div><div class="tbl-wrap"><table><thead><tr><th class="wstar"></th><th>Product</th><th>Model</th><th>Manufacturer</th><th>Channel Stock</th><th>VIP Stock</th><th>Floor £</th><th>VIP £</th></tr></thead><tbody>';
    rows.forEach(r => {
      html += `<tr class="clickable" onclick="loadSticSku(${r.product_id},'search')">
        <td class="wstar">${watchStarHtml(r.product_id)}</td>
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
        <td>${(r.total_stock||0).toLocaleString()}</td>
        <td>${(r.vip_stock||0).toLocaleString()}</td>
        <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
        <td>${vipCell(r.vip_price, r.min_price)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    const _sticSearchEl = document.getElementById('stic-search-results');
    _sticSearchEl.innerHTML = html;
    makeSortableAll(_sticSearchEl);
  });
}

// ── STIC SKU drill-down ───────────────────────────────────────────────────────
let sticSkuBackSection = 'overview';
function loadSticSku(productId, backSection) {
  sticSkuBackSection = backSection || 'overview';
  document.getElementById('stic-sku-back').onclick = () => showSticSection(sticSkuBackSection);
  showSticSection('sku');
  document.getElementById('stic-sku-content').innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/stic/sku/' + productId).then(r=>r.json()).then(data => {
    renderSticSku(data);
  });
}

// ── VIP price cell helper ─────────────────────────────────────────────────────
// Red  = VIP above floor (priced out)
// Blue = VIP equals floor (VIP is the cheapest / at floor)
// No badge = VIP below recorded floor (edge case — VIP has no stock but a lower list price)
function vipCell(vip, floor) {
  if (vip == null) return '—';
  const fmt = '£' + vip.toFixed(2);
  if (floor == null) return fmt;
  if (vip > floor) return `<span class="badge badge-red">${fmt}</span>`;
  if (vip < floor) return fmt;   // below floor — just show the price, no badge needed
  return `<span class="badge badge-blue">${fmt}</span>`;
}

function renderSticSku(data) {
  const el = document.getElementById('stic-sku-content');
  const { info, snapshot, price_history, stock_history, cheapest_history } = data;

  const metaParts = [
    `Product: ${info.product_id}`,
    `Manufacturer: ${info.manufacturer}`,
    `Model: ${info.model_no}`,
    `EAN: ${info.ean || '—'}`,
    `Group: ${info.product_group || '—'}`,
  ];
  if (info.description) metaParts.push(`Description: ${info.description}`);
  let html = `<div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:8px">
    <div style="flex:1">
      <h3 style="margin:0 0 4px">${info.manufacturer} — ${info.model_no}</h3>
      <p style="color:#605E5C;margin:0;font-size:12px">${metaParts.join(' | ')}</p>
    </div>
    <button class="watch-btn" data-watch-pid="${info.product_id}"
      onclick="toggleWatch(${info.product_id},event)"
      title="${_watchedIds.has(info.product_id)?'Remove from watchlist':'Add to watchlist'}"
      style="margin-top:2px">${_watchedIds.has(info.product_id)?'★':'☆'}</button>
    ${eolBtnHtml(info.product_id)}
  </div>
  <div style="margin-bottom:12px;font-size:12px;color:#605E5C">
    <span style="font-weight:600">STIC URL:</span>
    ${info.stic_url
      ? `<a href="${info.stic_url}" target="_blank" style="color:#0078D4;margin-left:6px">${info.stic_url}</a>`
      : `<span style="margin-left:6px;color:#A19F9D">Not yet cached — will be saved on next successful scrape</span>`}
    <span style="margin-left:12px">
      <input id="stic-url-input-${info.product_id}" type="text" placeholder="Paste correct STIC URL to override…"
        style="width:340px;padding:3px 7px;font-size:12px;border:1px solid #8A8886;border-radius:2px;font-family:inherit"/>
      <button onclick="saveSticUrl(${info.product_id})"
        style="margin-left:4px;padding:3px 10px;background:#0078D4;color:#fff;border:none;border-radius:2px;cursor:pointer;font-size:12px">Save</button>
      <button id="rescrape-btn-${info.product_id}" onclick="triggerRescrape(${info.product_id})"
        style="margin-left:8px;padding:3px 10px;background:#107C10;color:#fff;border:none;border-radius:2px;cursor:pointer;font-size:12px"
        title="Re-scrape STIC now for this SKU and refresh the page">▶ Scrape Now</button>
    </span>
  </div>
  <div style="margin-bottom:14px">
    <div style="font-size:11px;font-weight:600;color:#605E5C;margin-bottom:4px">Notes</div>
    <div style="display:flex;gap:6px;align-items:flex-start">
      <textarea id="sku-notes-${info.product_id}" rows="2"
        placeholder="Add notes — why EOL'd, why not listed, anything useful for later…"
        style="flex:1;max-width:600px;padding:5px 8px;font-size:12px;border:1px solid #C8C6C4;border-radius:2px;font-family:inherit;resize:vertical"
      >${info.notes ? info.notes.replace(/</g,'&lt;') : ''}</textarea>
      <button onclick="saveNotes(${info.product_id})"
        style="padding:5px 12px;background:#0078D4;color:#fff;border:none;border-radius:2px;cursor:pointer;font-size:12px;white-space:nowrap">Save</button>
      <button onclick="clearNotes(${info.product_id})"
        style="padding:5px 12px;background:#fff;color:#D13438;border:1px solid #D13438;border-radius:2px;cursor:pointer;font-size:12px;white-space:nowrap">Clear</button>
    </div>
  </div>`;

  // Snapshot table
  html += '<div class="section-title">Current Snapshot</div><div class="tbl-wrap"><table><thead><tr><th>Distributor</th><th>Price</th><th>Stock</th></tr></thead><tbody>';
  snapshot.forEach(r => {
    html += `<tr><td>${r.distributor}</td><td>${r.price ? '£'+r.price.toFixed(2) : '<span class="badge badge-orange">No price</span>'}</td><td>${r.qty !== null ? r.qty : '—'}</td></tr>`;
  });
  html += '</tbody></table></div>';

  // Cheapest history table
  html += `<div style="display:flex;align-items:center;justify-content:space-between;margin:12px 0 4px">
    <div class="section-title" style="margin:0">Cheapest Price History</div>
    <button onclick="openSticPurgeDatesModal(${info.product_id})"
      style="padding:3px 10px;font-size:11px;background:#A4262C;color:#fff;border:none;border-radius:2px;cursor:pointer"
      title="Delete bad data for one or more days">🗑 Purge Days</button>
  </div>
  <div class="tbl-wrap"><table><thead><tr><th>Date</th><th>Distributor</th><th>Price</th></tr></thead><tbody>`;
  cheapest_history.forEach(r => {
    html += `<tr><td>${fmtDate(r.date)}</td><td>${r.distributor}</td><td>${r.price ? '£'+r.price.toFixed(2) : '—'}</td></tr>`;
  });
  html += '</tbody></table></div>';

  el.innerHTML = html;
  makeSortableAll(el);

  // Charts
  const dists = [...new Set(price_history.map(r => r.distributor))];
  const dates  = [...new Set(price_history.map(r => r.date))].sort();
  const DIST_COLOURS = {
    'VIP':        '#0078D4',   // blue
    'M2M Direct': '#FFB900',   // amber
    'TD Synnex':  '#D13438',   // red
    'Target':     '#8A8886',   // grey
    'Westcoast':  '#107C10',   // green
  };
  const _fallback = ['#00B7C3','#8764B8','#E88C1A','#69797E'];
  const distColour = (d, i) => DIST_COLOURS[d] ?? _fallback[i % _fallback.length];

  const priceDs = dists.map((d, i) => ({
    label: d,
    data: dates.map(dt => { const row = price_history.find(r => r.distributor===d && r.date===dt); return row?.price ?? null; }),
    borderColor: distColour(d, i), backgroundColor: 'transparent',
    tension: 0.2, spanGaps: true, pointRadius: 2,
  }));

  const stockDs = dists.map((d, i) => ({
    label: d,
    data: dates.map(dt => { const row = stock_history.find(r => r.distributor===d && r.date===dt); return row?.qty ?? 0; }),
    backgroundColor: distColour(d, i),
  }));

  const cheapestDs = [{
    label: 'Cheapest',
    data: dates.map(dt => { const row = cheapest_history.find(r => r.date===dt); return row?.price ?? null; }),
    borderColor: '#0078D4', backgroundColor: 'transparent',
    tension: 0.2, spanGaps: true, pointRadius: 2,
  }];

  // Helper: return ISO week key + Monday date for a YYYY-MM-DD string
  function _isoWeekInfo(dateStr) {
    const d = new Date(dateStr + 'T00:00:00Z');
    const dow = d.getUTCDay() || 7;            // Mon=1 … Sun=7
    const thu = new Date(d);
    thu.setUTCDate(d.getUTCDate() + (4 - dow)); // Thursday of same ISO week
    const y = thu.getUTCFullYear();
    const jan1 = new Date(Date.UTC(y, 0, 1));
    const wn = Math.ceil(((thu - jan1) / 86400000 + 1) / 7);
    const mon = new Date(d);
    mon.setUTCDate(d.getUTCDate() - (dow - 1)); // Monday of same ISO week
    return { key: `${y}-W${String(wn).padStart(2,'0')}`, monday: mon };
  }

  // Compute daily stock drops per distributor (stock increases = deliveries, ignored)
  const _dailyDrops = {}; // { dist: { dateStr: units } }
  dists.forEach(d => {
    _dailyDrops[d] = {};
    dates.forEach((dt, idx) => {
      if (idx === 0) return;
      const prevDt = dates[idx - 1];
      const curr = stock_history.find(r => r.distributor === d && r.date === dt);
      const prev = stock_history.find(r => r.distributor === d && r.date === prevDt);
      if (!curr || !prev || curr.qty === null || prev.qty === null) return;
      const drop = prev.qty - curr.qty;
      if (drop > 0) _dailyDrops[d][dt] = drop;
    });
  });

  // Build a fixed 12-week x-axis ending at the latest week in the data.
  // Weeks with no data show zero bars — the scale never shrinks.
  const _MON_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  // Find the Monday of a given ISO week key ("YYYY-Www")
  function _mondayOfWeekKey(weekKey) {
    const [y, w] = weekKey.split('-W').map(Number);
    const jan4  = new Date(Date.UTC(y, 0, 4));          // Jan 4 is always in week 1
    const dow4  = jan4.getUTCDay() || 7;                // Mon=1
    const mon1  = new Date(jan4);
    mon1.setUTCDate(jan4.getUTCDate() - (dow4 - 1));    // Monday of week 1
    const target = new Date(mon1);
    target.setUTCDate(mon1.getUTCDate() + (w - 1) * 7);
    return target;
  }

  // Latest week present in the data (or current week if no data)
  const _dataWeeks = [...new Set(dates.map(dt => _isoWeekInfo(dt).key))].sort();
  const _latestWk  = _dataWeeks.length ? _dataWeeks[_dataWeeks.length - 1]
                                       : _isoWeekInfo(new Date().toISOString().slice(0,10)).key;
  const _latestMon = _mondayOfWeekKey(_latestWk);

  // Generate exactly 12 consecutive week keys/labels ending at _latestWk
  const _weekKeys   = [];
  const _weekLabels = [];
  for (let i = 11; i >= 0; i--) {
    const mon = new Date(_latestMon);
    mon.setUTCDate(_latestMon.getUTCDate() - i * 7);
    const ds = mon.toISOString().slice(0, 10);
    _weekKeys.push(_isoWeekInfo(ds).key);
    _weekLabels.push(`${mon.getUTCDate()} ${_MON_ABBR[mon.getUTCMonth()]}`);
  }

  // Weekly totals per distributor (last 12 weeks)
  const _weeklyByDist = dists.map(d =>
    _weekKeys.map(wk =>
      Object.entries(_dailyDrops[d])
        .filter(([dt]) => _isoWeekInfo(dt).key === wk)
        .reduce((sum, [, v]) => sum + v, 0)
    )
  );

  // Total units sold per week across all distributors
  const _weekTotals = _weekKeys.map((_, wi) =>
    dists.reduce((sum, _, di) => sum + _weeklyByDist[di][wi], 0)
  );

  // Rolling 4-week average — average of non-zero weeks in each 4-week window.
  // Returns null for windows with no data at all (padding weeks) so the line
  // doesn't appear until real data begins.
  const _rolling4 = _weekKeys.map((_, i) => {
    const window = _weekTotals.slice(Math.max(0, i - 3), i + 1);
    const nonZero = window.filter(v => v > 0);
    if (nonZero.length === 0) return null;
    return parseFloat((nonZero.reduce((a, b) => a + b, 0) / nonZero.length).toFixed(2));
  });

  // Bar datasets — one per distributor
  const salesDs = dists.map((d, i) => ({
    type: 'bar',
    label: d,
    data: _weeklyByDist[i],
    backgroundColor: distColour(d, i),
    stack: 'sales',
    order: 2,
  }));

  // Rolling 4-week average line — solid, distinct colour, spans full axis
  salesDs.push({
    type: 'line',
    label: '4-wk rolling avg',
    data: _rolling4,
    borderColor: '#000000',
    borderWidth: 2,
    backgroundColor: 'transparent',
    pointRadius: 2,
    tension: 0.4,
    spanGaps: false,
    order: 1,
  });

  const chartHtml = `<div class="chart-grid">
    <div class="chart-box"><h4>Price per Distributor</h4><canvas id="chart-price"></canvas></div>
    <div class="chart-box"><h4>Cheapest Price Trend (In-Stock Only)</h4><canvas id="chart-cheapest"></canvas></div>
    <div class="chart-box"><h4>Stock per Distributor</h4><canvas id="chart-stock"></canvas></div>
    <div class="chart-box"><h4>Estimated Sales per Distributor (Weekly)</h4><canvas id="chart-sales"></canvas></div>
  </div>`;
  el.innerHTML += chartHtml;

  const fmtDates = dates.map(fmtDate);
  const opts = (type, datasets, stacked, labels) => ({
    type, data: { labels: labels || fmtDates, datasets },
    options: { responsive:true, maintainAspectRatio:true, plugins:{legend:{labels:{font:{size:10}}}},
                scales: { x:{ticks:{font:{size:10}}}, y:{stacked: stacked||false, ticks:{font:{size:10}}} } }
  });

  new Chart(document.getElementById('chart-price'), opts('line', priceDs));
  new Chart(document.getElementById('chart-cheapest'), opts('line', cheapestDs));
  new Chart(document.getElementById('chart-stock'), opts('bar', stockDs, true));
  new Chart(document.getElementById('chart-sales'), {
    type: 'bar',
    data: { labels: _weekLabels, datasets: salesDs },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { labels: { font: { size: 10 } } } },
      scales: {
        x: { ticks: { font: { size: 10 } } },
        y: { stacked: true, ticks: { font: { size: 10 } } },
      },
    },
  });
}

// ── STIC pre-built reports ────────────────────────────────────────────────────
let currentSidebarBtn = null;
function loadReport(name, btn) {
  if (currentSidebarBtn) currentSidebarBtn.classList.remove('active');
  if (btn) { btn.classList.add('active'); currentSidebarBtn = btn; }
  showSticSection('report');
  document.getElementById('stic-report-content').innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/stic/report/' + name).then(r=>r.json()).then(data => {
    renderReport(name, data);
  });
}

const REPORT_TITLES = {
  no_channel_stock:   'No Channel Stock 5+ Days',
  back_in_stock:      'Back In Stock',
  single_distributor: 'Single Distributor Remaining',
  new_stock_arrival:  'New Stock Arrival',
  vip_out_on_price:   'VIP Out on Price',
  vip_static:         'VIP Static — Market Moving',
  vip_exclusive:      'VIP Exclusive',
  vip_price_gap:      'VIP Price Gap',
  never_stocked:      'No Channel Stock Ever',
  price_dropping:     'Price Dropping',
  price_rising:       'Price Rising',
  daily_changes:      'All Changes Since Yesterday',
};

const REPORT_HELP = {
  no_channel_stock: {
    title: 'No Channel Stock — 5+ Days',
    body: `<p>Shows products where <strong>no distributor has had any stock for at least 5 consecutive days</strong>. The report looks back across the last 5 dates in the database and only includes products where every distributor shows zero stock on every one of those dates.</p>
<p><strong>Floor £</strong> and <strong>VIP £</strong> show the last known prices where available, but may be blank if no price has been listed recently.</p>
<p><strong>How to use:</strong> These products are effectively out of the market. They may represent supply chain issues, end-of-life SKUs, or products that are exclusively held somewhere outside the channel. Worth reviewing whether VIP should be sourcing them independently.</p>`
  },
  back_in_stock: {
    title: 'Back In Stock',
    body: `<p>Products that had <strong>zero channel stock yesterday but have stock today</strong>. "Yesterday" means the most recent date before today in the database.</p>
<p><strong>Floor £</strong> is the cheapest price from any distributor that currently has stock. <strong>VIP £</strong> shows VIP's current price if they have stock.</p>
<p><strong>How to use:</strong> Fast-moving opportunity list. These products just became available again — if VIP is not yet priced competitively or doesn't have stock, this is the moment to act before competitors react.</p>`
  },
  single_distributor: {
    title: 'Single Distributor Remaining',
    body: `<p>Products where <strong>exactly one distributor has stock today</strong>. All other distributors show zero or no stock.</p>
<p><strong>VIP £</strong> tells you whether VIP is that sole supplier (blue = sole supplier and matches floor, red = VIP is the only one but priced above their own floor, which would only happen if multiple VIP rows exist) or whether a competitor holds it exclusively.</p>
<p><strong>How to use:</strong> Supply concentration risk. If VIP is the sole supplier, this is a pricing power opportunity. If a competitor is the sole supplier, stock availability for VIP's customers may be at risk.</p>`
  },
  new_stock_arrival: {
    title: 'New Stock Arrival',
    body: `<p>Products that <strong>had zero stock for 5 or more consecutive days and now have stock today</strong>. This is a stricter version of Back In Stock — the absence must have lasted at least 5 days, not just overnight.</p>
<p><strong>How to use:</strong> Significant restocks only. A product reappearing after 5+ days of absence often indicates a new shipment or an allocation being released. These are worth flagging to the sales team as fresh supply on previously unavailable lines.</p>`
  },
  vip_out_on_price: {
    title: 'VIP Out on Price',
    body: `<p>VIP <strong>has stock today but is not the cheapest</strong> in-stock distributor. The floor price is the cheapest price from any distributor that currently has units — zero-stock listings are ignored.</p>
<p>Sorted by <strong>VIP stock quantity descending</strong>, so the largest inventory exposure is at the top.</p>
<p><strong>How to use:</strong> These are live sales being lost right now. A customer comparing prices will find a cheaper option. The top of this list — high VIP stock, priced above the market — represents the biggest revenue risk. The gap column on VIP Price Gap shows the same data sorted differently.</p>`
  },
  vip_static: {
    title: 'VIP Static — Market Moving',
    body: `<p>VIP's stock level <strong>has not changed for 5 or more consecutive days</strong> while the rest of the market continues to move. Calculated by checking that VIP's qty is identical across all 5+ dates in the window.</p>
<p><strong>How to use:</strong> Either VIP is not selling this product at all (demand problem or pricing issue), or sales are perfectly matching replenishment (less likely). Cross-reference with VIP Out on Price — if a product appears on both lists, VIP has static stock AND is not the cheapest, which strongly suggests a pricing problem is suppressing sales.</p>`
  },
  vip_exclusive: {
    title: 'VIP Exclusive',
    body: `<p>VIP is the <strong>only distributor with stock today</strong>. All other distributors show zero stock or no listing.</p>
<p><strong>How to use:</strong> Pricing power opportunity. When VIP is the sole source of supply, there is no direct price competition and margin can potentially be protected or improved. Also useful for identifying which SKUs VIP should be promoting actively — customers who need these products have nowhere else to go in the channel.</p>`
  },
  vip_price_gap: {
    title: 'VIP Price Gap',
    body: `<p>VIP has stock but is priced <strong>above the cheapest in-stock competitor</strong>. Sorted by the <strong>absolute £ gap descending</strong> — the largest price difference is at the top.</p>
<p>The floor price only includes distributors who actually have stock. Zero-stock listings are excluded so the gap reflects a real alternative a customer could buy today.</p>
<p><strong>How to use:</strong> Spot where VIP pricing looks most anomalous. A large gap on a high-volume SKU is a strong signal of either an incorrect price loaded in the system or a competitor running a deep promotion. Unlike VIP Out on Price (sorted by VIP stock), this list highlights the SKUs where VIP's price looks most out of line, regardless of how much stock VIP holds.</p>`
  },
  never_stocked: {
    title: 'No Channel Stock Ever',
    body: `<p>Products that have <strong>never had any distributor price or stock</strong> across all dates in the database. No distributor has ever listed a price for these SKUs.</p>
<p><strong>How to use:</strong> Potential VIP exclusives or products not yet released into the channel. If VIP holds stock of these products, they may have an exclusive supply arrangement. Worth reviewing against VIP's own stock system to see which of these VIP actually holds — those would be exclusive sales opportunities with no channel competition at all.</p>`
  },
  price_dropping: {
    title: 'Price Dropping',
    body: `<p>Products where the <strong>cheapest available price today is lower than it was yesterday</strong>. Only compares prices from distributors with actual stock on both days.</p>
<p>Sorted by the size of the price drop (largest drop first).</p>
<p><strong>How to use:</strong> Early warning of price pressure. A distributor cutting price aggressively may be trying to clear stock, responding to a competitor, or reacting to a new product announcement. If VIP is on these products, a price review may be needed to stay competitive.</p>`
  },
  price_rising: {
    title: 'Price Rising',
    body: `<p>Products where prices have been <strong>rising across distributors over the last 7 days</strong>. Only products with measurable price movement (not flat or unchanged) are included.</p>
<p><strong>How to use:</strong> May indicate tightening supply, increased demand, or a cost increase being passed through the channel. If VIP's price has not risen in line with the market, VIP may be underselling. If VIP's price has risen ahead of the market, there may be a risk of losing sales.</p>`
  },
  daily_changes: {
    title: 'All Changes Since Yesterday',
    body: `<p>Every <strong>price move and stock change across all distributors since yesterday</strong>. Shows both the old and new value side by side.</p>
<p>A price change is flagged in <span style="color:#A4262C;font-weight:600">red</span> if the price went up, <span style="color:#107C10;font-weight:600">green</span> if it went down. Stock changes are shown in the Stock column.</p>
<p><strong>How to use:</strong> Full market activity log for the day. Useful for a quick morning review of what moved overnight before looking at specific reports. If a product you care about appears here, click through to the SKU drill-down to see the full price and stock history.</p>`
  },
};

// ── Info modal ────────────────────────────────────────────────────────────────
function showHelp(name) {
  const h = REPORT_HELP[name];
  if (!h) return;
  document.getElementById('modal-title').textContent = h.title;
  document.getElementById('modal-body').innerHTML = h.body;
  document.getElementById('info-modal').classList.add('open');
}
function closeHelp() {
  document.getElementById('info-modal').classList.remove('open');
}

// ── Product edit modal ─────────────────────────────────────────────────────────
let _editCallback = null;   // called after a successful save to refresh the parent view

function _openProductEditFocus(productId, focusFieldId) {
  _openProductEdit(productId, false, focusFieldId);
}

function _openProductEdit(productId, focusMsrp, focusFieldId) {
  fetch(`/api/catalogue/product/${productId}`)
    .then(r => r.json()).then(p => {
      document.getElementById('ep-product-id').value    = p.product_id;
      document.getElementById('ep-model-no').value      = p.model_no        || '';
      document.getElementById('ep-manufacturer').value  = p.manufacturer    || '';
      document.getElementById('ep-product-group').value = p.product_group   || 'PROD_VIDEO';
      document.getElementById('ep-description').value   = p.description     || '';
      document.getElementById('ep-chipset').value       = p.chipset         || '';
      document.getElementById('ep-ean').value           = p.ean             || '';
      document.getElementById('ep-msrp').value          = p.msrp != null ? p.msrp : '';
      // Retailer IDs & URLs
      document.getElementById('ep-amazon-asin').value   = p.amazon_asin  || '';
      document.getElementById('ep-currys-sku').value    = p.currys_sku   || '';
      document.getElementById('ep-argos-sku').value     = p.argos_sku    || '';
      document.getElementById('ep-ocuk-code').value     = p.ocuk_code    || '';
      document.getElementById('ep-very-sku').value      = p.very_sku     || '';
      document.getElementById('ep-scan-ln').value       = p.scan_ln      || '';
      document.getElementById('ep-very-url').value      = p.very_url     || '';
      document.getElementById('ep-scan-url').value      = p.scan_url     || '';
      document.getElementById('ep-awdit-url').value     = p.awdit_url    || '';
      document.getElementById('ep-ccl-url').value       = p.ccl_url      || '';
      document.getElementById('ep-box-url').value       = p.box_url      || '';
      document.getElementById('ep-msg').textContent     = '';
      document.getElementById('edit-modal-title').textContent = `Edit Product — ${p.product_id}`;

      // Highlight the appropriate field
      const targetId = focusFieldId || (focusMsrp ? 'ep-msrp' : 'ep-model-no');
      ['ep-msrp','ep-ean','ep-chipset'].forEach(id =>
        document.getElementById(id).classList.remove('highlight'));
      if (targetId !== 'ep-model-no') document.getElementById(targetId).classList.add('highlight');
      document.getElementById('edit-modal').classList.add('open');
      setTimeout(() => {
        const f = document.getElementById(targetId) || document.getElementById('ep-model-no');
        f.focus(); if (f.select) f.select();
      }, 80);
    });
}

function _closeEditModal() {
  document.getElementById('edit-modal').classList.remove('open');
  _editCallback = null;
}

function _saveProduct() {
  const pid  = document.getElementById('ep-product-id').value;
  const msrpRaw = document.getElementById('ep-msrp').value.trim();
  const payload = {
    model_no:      document.getElementById('ep-model-no').value.trim(),
    manufacturer:  document.getElementById('ep-manufacturer').value.trim(),
    product_group: document.getElementById('ep-product-group').value,
    description:   document.getElementById('ep-description').value.trim(),
    chipset:       document.getElementById('ep-chipset').value.trim(),
    ean:           document.getElementById('ep-ean').value.trim(),
    msrp:          msrpRaw === '' ? null : parseFloat(msrpRaw),
    // Retailer IDs & URLs
    amazon_asin:   document.getElementById('ep-amazon-asin').value.trim() || null,
    currys_sku:    document.getElementById('ep-currys-sku').value.trim()  || null,
    argos_sku:     document.getElementById('ep-argos-sku').value.trim()   || null,
    ocuk_code:     document.getElementById('ep-ocuk-code').value.trim()   || null,
    very_sku:      document.getElementById('ep-very-sku').value.trim()    || null,
    scan_ln:       document.getElementById('ep-scan-ln').value.trim()     || null,
    very_url:      document.getElementById('ep-very-url').value.trim()    || null,
    scan_url:      document.getElementById('ep-scan-url').value.trim()    || null,
    awdit_url:     document.getElementById('ep-awdit-url').value.trim()   || null,
    ccl_url:       document.getElementById('ep-ccl-url').value.trim()     || null,
    box_url:       document.getElementById('ep-box-url').value.trim()     || null,
  };
  const msg = document.getElementById('ep-msg');
  msg.style.color = '#605E5C';
  msg.textContent = 'Saving…';
  fetch(`/api/catalogue/product/${pid}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).then(d => {
    if (d.error) {
      msg.style.color = '#A4262C'; msg.textContent = d.error;
    } else {
      msg.style.color = '#107C10'; msg.textContent = '✓ Saved';
      if (_editCallback) _editCallback(pid, payload);
      setTimeout(_closeEditModal, 700);
    }
  }).catch(() => { msg.style.color='#A4262C'; msg.textContent='Save failed — check connection.'; });
}

let _reportCache = { name: null, rows: [] };

function buildReportFilterBar(rows) {
  const manufacturers  = [...new Set(rows.map(r=>r.manufacturer).filter(Boolean))].sort();
  const groups         = [...new Set(rows.map(r=>r.product_group).filter(Boolean))].sort();
  const mOpts  = ['<option value="">All Manufacturers</option>', ...manufacturers.map(m=>`<option>${m}</option>`)].join('');
  const gOpts  = ['<option value="">All Groups</option>',        ...groups.map(g=>`<option>${g}</option>`)].join('');
  return `<div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    <select id="filter-mfr"   onchange="applyReportFilters()" style="padding:6px 10px;border:1px solid #C8C6C4;border-radius:4px;font-size:13px;min-width:180px">${mOpts}</select>
    <select id="filter-group" onchange="applyReportFilters()" style="padding:6px 10px;border:1px solid #C8C6C4;border-radius:4px;font-size:13px;min-width:180px">${gOpts}</select>
  </div>`;
}

function applyReportFilters() {
  const mfr   = document.getElementById('filter-mfr')?.value   || '';
  const grp   = document.getElementById('filter-group')?.value || '';
  const filtered = _reportCache.rows.filter(r =>
    (!mfr || r.manufacturer   === mfr) &&
    (!grp || r.product_group  === grp)
  );
  renderReportTable(_reportCache.name, filtered);
}

function renderReport(name, rows) {
  _reportCache = { name, rows };
  const title = REPORT_TITLES[name] || name;
  if (!rows.length) {
    document.getElementById('stic-report-content').innerHTML =
      `<div class="section-title">${title}</div><p style="color:#A19F9D;padding:20px">No items match this report.</p>`;
    return;
  }
  renderReportTable(name, rows);
}

function renderReportTable(name, rows) {
  const title = REPORT_TITLES[name] || name;

  let cols, rowFn;

  if (name === 'vip_out_on_price') {
    cols = ['Product','Model','Manufacturer','Channel Stock','VIP Stock','Floor £','VIP £','Suggested Cost £'];
    rowFn = r => {
      const sug = r.min_price ? '£'+(r.min_price*0.96).toFixed(2) : '—';
      return `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
        <td>${(r.total_stock||0).toLocaleString()}</td>
        <td>${(r.vip_stock||0).toLocaleString()}</td>
        <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
        <td>${vipCell(r.vip_price, r.min_price)}</td>
        <td style="font-weight:600;color:#107C10">${sug}</td>
      </tr>`;
    };
  } else if (name === 'vip_price_gap') {
    cols = ['Product','Model','Manufacturer','VIP £','Floor £','Gap £'];
    rowFn = r => `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
      <td>${r.vip_price ? '£'+r.vip_price.toFixed(2) : '—'}</td>
      <td>${r.floor_price ? '£'+r.floor_price.toFixed(2) : '—'}</td>
      <td><span class="badge badge-red">+£${r.gap.toFixed(2)}</span></td>
    </tr>`;
  } else if (name === 'daily_changes') {
    cols = ['Product','Model','Distributor','Yesterday £','Today £','Change','Stock'];
    rowFn = r => {
      const diff = r.price_today !== null && r.price_yesterday !== null ? r.price_today - r.price_yesterday : null;
      const badge = diff === null ? '' : diff > 0 ? `<span class="badge badge-red">+£${diff.toFixed(2)}</span>` : `<span class="badge badge-green">£${diff.toFixed(2)}</span>`;
      return `<tr><td>${r.product_id}</td><td>${r.model_no}</td><td>${r.distributor}</td>
        <td>${r.price_yesterday ? '£'+r.price_yesterday.toFixed(2) : '—'}</td>
        <td>${r.price_today ? '£'+r.price_today.toFixed(2) : '—'}</td>
        <td>${badge}</td><td>${r.qty_today ?? '—'}</td></tr>`;
    };
  } else if (name === 'price_dropping' || name === 'price_rising') {
    cols = ['Product','Model','Manufacturer','Yesterday £','Today £','Change'];
    rowFn = r => {
      const diff = (r.price_today||0) - (r.price_yesterday||0);
      const badge = diff > 0 ? `<span class="badge badge-red">+£${diff.toFixed(2)}</span>` : `<span class="badge badge-green">£${diff.toFixed(2)}</span>`;
      return `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
        <td>${r.price_yesterday ? '£'+r.price_yesterday.toFixed(2) : '—'}</td>
        <td>${r.price_today ? '£'+r.price_today.toFixed(2) : '—'}</td>
        <td>${badge}</td></tr>`;
    };
  } else {
    cols = ['Product','Model','Manufacturer','Channel Stock','Floor £','VIP £'];
    rowFn = r => `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
      <td>${(r.total_stock||0).toLocaleString()}</td>
      <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
      <td>${vipCell(r.vip_price, r.min_price)}</td>
    </tr>`;
  }

  const filterBar = (name === 'vip_out_on_price') ? buildReportFilterBar(_reportCache.rows) : '';
  const savedMfr  = document.getElementById('filter-mfr')?.value   || '';
  const savedGrp  = document.getElementById('filter-group')?.value || '';

  let html = `<div class="section-title">${title} <span style="font-size:12px;font-weight:400;color:#605E5C">(${rows.length} items)</span><button class="info-btn" onclick="showHelp('${name}')" title="How this report works">ⓘ</button></div>
    ${filterBar}<div class="tbl-wrap"><table><thead><tr>${cols.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>`;
  rows.forEach(r => { html += rowFn(r); });
  html += '</tbody></table></div>';
  const _sticReportEl = document.getElementById('stic-report-content');
  _sticReportEl.innerHTML = html;
  makeSortableAll(_sticReportEl);

  // Restore filter selections after re-render
  if (savedMfr  && document.getElementById('filter-mfr'))   document.getElementById('filter-mfr').value   = savedMfr;
  if (savedGrp  && document.getElementById('filter-group')) document.getElementById('filter-group').value = savedGrp;
}

function loadInvestigateReport(btn) {
  if (btn) {
    document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  }
  document.querySelectorAll('#main-stic .content-section').forEach(s=>s.classList.remove('active'));
  document.getElementById('stic-investigate').classList.add('active');
  const el = document.getElementById('stic-investigate-content');
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/investigate').then(r=>r.json()).then(data => {
    let html = '<h2 style="margin:0 0 16px">Investigate</h2>';

    // No STIC page
    html += '<div class="inv-subsection"><div class="inv-subsection-title">No STIC Page (' + data.no_stic_page.length + ')</div>';
    if (!data.no_stic_page.length) {
      html += '<div class="inv-empty">None — all active SKUs found on STIC</div>';
    } else {
      html += '<div class="tbl-wrap"><table><thead><tr><th>SKU</th><th>Model</th><th>Manufacturer</th><th></th></tr></thead><tbody>';
      data.no_stic_page.forEach(r => {
        html += `<tr>
          <td><a href="#" onclick="loadSticSku(${r.product_id},'investigate');return false">${r.product_id}</a></td>
          <td><a href="#" onclick="loadSticSku(${r.product_id},'investigate');return false">${r.model_no}</a></td>
          <td>${r.manufacturer}</td>
          <td>${eolBtnHtml(r.product_id)}</td></tr>`;
      });
      html += '</tbody></table></div>';
    }
    html += '</div>';

    // Missing EAN
    html += '<div class="inv-subsection"><div class="inv-subsection-title">Missing EAN (' + data.missing_ean.length + ')</div>';
    if (!data.missing_ean.length) {
      html += '<div class="inv-empty">None — all active SKUs have EAN codes</div>';
    } else {
      html += '<div class="tbl-wrap"><table><thead><tr><th>SKU</th><th>Model</th><th>Manufacturer</th></tr></thead><tbody>';
      data.missing_ean.forEach(r => {
        html += `<tr>
          <td><a href="#" onclick="loadSticSku(${r.product_id},'investigate');return false">${r.product_id}</a></td>
          <td><a href="#" onclick="loadSticSku(${r.product_id},'investigate');return false">${r.model_no}</a></td>
          <td>${r.manufacturer}</td></tr>`;
      });
      html += '</tbody></table></div>';
    }
    html += '</div>';

    // Data bleeds
    html += '<div class="inv-subsection"><div class="inv-subsection-title">Data Bleeds — ' + (data.latest_date||'') + ' (' + data.data_bleeds.length + ')</div>';
    if (!data.data_bleeds.length) {
      html += '<div class="inv-empty">✅ No data bleed suspects today</div>';
    } else {
      html += '<div class="tbl-wrap"><table><thead><tr><th>SKU A</th><th>Model A</th><th>SKU B</th><th>Model B</th><th>Matching distis</th></tr></thead><tbody>';
      data.data_bleeds.forEach(r => {
        html += `<tr>
          <td><a href="#" onclick="loadSticSku(${r.product_id},'investigate');return false">${r.product_id}</a></td>
          <td>${r.model_no}</td>
          <td><a href="#" onclick="loadSticSku(${r.matched_to},'investigate');return false">${r.matched_to}</a></td>
          <td>${r.matched_model}</td>
          <td>${r.matching_rows}</td>
        </tr>`;
      });
      html += '</tbody></table></div>';
    }
    html += '</div>';

    el.innerHTML = html + '</div>';
    makeSortableAll(el);
    _refreshAllEolBtns();
  });
}

function loadProbeSkus(btn) {
  if (btn) {
    document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  }
  document.querySelectorAll('#main-stic .content-section').forEach(s=>s.classList.remove('active'));
  document.getElementById('stic-probe').classList.add('active');
  const el = document.getElementById('stic-probe-content');
  el.innerHTML = `
    <h2 style="margin:0 0 6px">Probe SKUs</h2>
    <p style="font-size:12px;color:#605E5C;margin:0 0 14px">Track competitor or evaluation products not in the main catalogue.
    Auto-assigned IDs from 990000+. Scraper navigates directly to the STIC URL you provide.
    EOL when done; promote to a real SKU via Catalogue if you decide to list.</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:16px;padding:12px;background:#F3F2F1;border-radius:3px">
      <div style="display:flex;flex-direction:column;gap:3px">
        <label style="font-size:11px;color:#605E5C;font-weight:600">Model / Name *</label>
        <input id="probe-model" type="text" placeholder="e.g. RX 9070 XT CHALLENGER OC 16G"
          style="width:260px;padding:5px 8px;font-size:12px;border:1px solid #C8C6C4;border-radius:2px;font-family:inherit"/>
      </div>
      <div style="display:flex;flex-direction:column;gap:3px">
        <label style="font-size:11px;color:#605E5C;font-weight:600">Manufacturer</label>
        <input id="probe-mfr" type="text" placeholder="e.g. ASRock"
          style="width:130px;padding:5px 8px;font-size:12px;border:1px solid #C8C6C4;border-radius:2px;font-family:inherit"/>
      </div>
      <div style="display:flex;flex-direction:column;gap:3px">
        <label style="font-size:11px;color:#605E5C;font-weight:600">STIC URL *</label>
        <input id="probe-url" type="text" placeholder="https://www.stockinthechannel.co.uk/Product/…"
          style="width:400px;padding:5px 8px;font-size:12px;border:1px solid #C8C6C4;border-radius:2px;font-family:inherit"/>
      </div>
      <button onclick="_addProbe()" style="padding:5px 14px;background:#0078D4;color:#fff;border:none;border-radius:2px;cursor:pointer;font-size:12px;font-weight:600;align-self:flex-end">+ Add</button>
    </div>
    <div id="probe-list-container"><div class="spinner">Loading…</div></div>`;
  _loadProbeList();
}

function _loadProbeList() {
  const target = document.getElementById('probe-list-container');
  if (target) target.innerHTML = '<div class="spinner">Loading…</div>';

  fetch('/api/probe/list').then(r=>r.json()).then(probes => {
    if (!target) return;
    if (!probes.length) {
      target.innerHTML = '<div class="inv-empty">No probe SKUs yet — add one above.</div>';
      return;
    }
    let tbl = `<div class="tbl-wrap"><table><thead><tr>
      <th>Product</th><th>Model</th><th>Manufacturer</th>
      <th>Channel Stock</th><th>Sold 7d</th><th>Stock</th><th>Floor</th><th></th>
    </tr></thead><tbody>`;
    probes.forEach(p => {
      const eolStyle = p.eol ? 'opacity:0.5' : '';
      const channelStock = p.channel_stock ?? 0;
      const sold7d = p.sold_7d ?? 0;
      const floor = p.floor_price != null ? '£' + p.floor_price.toFixed(2) : '—';
      // Per-distributor stock breakdown — only show distributors with actual data (qty not null)
      const activeDists = (p.snapshot || []).filter(s => s.qty !== null && s.qty !== undefined);
      const stockLines = activeDists.length
        ? activeDists.map(s =>
            `<span style="font-size:11px;display:block">${s.distributor}: ${s.qty}</span>`
          ).join('')
        : '<span style="color:#A19F9D;font-size:11px">No data</span>';
      tbl += `<tr style="${eolStyle}">
        <td><a href="#" onclick="loadSticSku(${p.product_id},'probe');return false">${p.product_id}</a></td>
        <td style="font-weight:500">${p.model_no}</td>
        <td>${p.manufacturer||'—'}</td>
        <td style="text-align:right">${channelStock}</td>
        <td style="text-align:right">${sold7d || '—'}</td>
        <td>${stockLines}</td>
        <td>${floor}</td>
        <td style="white-space:nowrap">${eolBtnHtml(p.product_id)}</td>
      </tr>`;
    });
    tbl += '</tbody></table></div>';
    target.innerHTML = tbl;
    makeSortableAll(target);
    _refreshAllEolBtns();
  });
}

function _addProbe() {
  const model = document.getElementById('probe-model')?.value.trim();
  const mfr   = document.getElementById('probe-mfr')?.value.trim();
  const url   = document.getElementById('probe-url')?.value.trim();
  if (!model) { alert('Model / Name is required.'); return; }
  if (!url || !url.includes('/Product/')) { alert('A valid STIC /Product/ URL is required.'); return; }
  fetch('/api/probe/add', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ model_no: model, manufacturer: mfr || null, stic_url: url })
  }).then(r=>r.json()).then(d => {
    if (d.error) { alert('Failed: ' + d.error); return; }
    // Clear form and reload probe list
    document.getElementById('probe-model').value = '';
    document.getElementById('probe-mfr').value   = '';
    document.getElementById('probe-url').value   = '';
    _loadProbeList();
  }).catch(() => alert('Network error.'));
}

function loadCatEOL(btn) {
  showCatSection('eol', btn);
  const el = document.getElementById('cat-eol-content');
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/eol').then(r=>r.json()).then(data => {
    let html = '<h2 style="margin:0 0 4px">EOL Products</h2>';
    html += '<p style="color:#605E5C;margin:0 0 16px;font-size:13px">These SKUs are marked End of Life. The scraper skips them. Click ✕ EOL to restore and resume scraping on the next cycle.</p>';
    if (!data.products.length) {
      html += '<div class="inv-empty">No products currently marked EOL.</div>';
    } else {
      html += '<div class="tbl-wrap"><table><thead><tr><th>SKU</th><th>Model</th><th>Manufacturer</th><th>Group</th><th>EAN</th><th></th></tr></thead><tbody>';
      data.products.forEach(r => {
        html += `<tr>
          <td>${r.product_id}</td>
          <td>${r.model_no}</td>
          <td>${r.manufacturer}</td>
          <td>${r.product_group||'—'}</td>
          <td>${r.ean||'—'}</td>
          <td>${eolBtnHtml(r.product_id)}</td>
        </tr>`;
      });
      html += '</tbody></table></div>';
    }
    el.innerHTML = html;
    makeSortableAll(el);
    _refreshAllEolBtns();
  });
}

// ── Scrape Groups ─────────────────────────────────────────────────────────────
let _scrapeGroupsRunning = {};   // label → true while a trigger is in-flight

function loadScrapeGroups(btn) {
  if (btn) {
    document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  document.querySelectorAll('#main-stic .content-section').forEach(s => s.classList.remove('active'));
  document.getElementById('stic-scrape').classList.add('active');
  const el = document.getElementById('stic-scrape-content');
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/scrape/groups').then(r => r.json()).then(data => {
    let html = '<h2 style="margin:0 0 4px">Refresh SKUs</h2>';
    html += '<p style="color:#605E5C;margin:0 0 16px;font-size:13px">Manually trigger a scrape run for any group. Runs in the background — a Telegram message will confirm when complete. Last scraped dates show when this group was last successfully processed.</p>';
    html += '<div class="tbl-wrap"><table><thead><tr><th>Group</th><th>Active SKUs</th><th>Last Scraped</th><th></th></tr></thead><tbody>';
    data.forEach(g => {
      const running = _scrapeGroupsRunning[g.label];
      const btnHtml = running
        ? `<button class="scrape-trigger-btn" disabled style="opacity:0.5;cursor:default">⏳ Running…</button>`
        : `<button class="scrape-trigger-btn" onclick="triggerScrapeGroup('${g.label.replace(/'/g,"\\'")}',this)">▶ Run</button>`;
      html += `<tr>
        <td><strong>${g.label}</strong></td>
        <td>${g.sku_count}</td>
        <td>${g.last_scraped ? fmtDate(g.last_scraped) : '<span style="color:#A19F9D">Never</span>'}</td>
        <td>${btnHtml}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    el.innerHTML = html;
    makeSortableAll(el);
  });
}

function triggerScrapeGroup(label, btn) {
  if (!confirm(`Start scrape for "${label}"?\\n\\nThis will run in the background. You\\'ll get a Telegram notification when done.`)) return;
  btn.disabled = true;
  btn.textContent = '⏳ Launching…';
  _scrapeGroupsRunning[label] = true;
  fetch('/api/scrape/group', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({label})
  }).then(r => r.json()).then(data => {
    if (!data.started) {
      btn.disabled = false;
      btn.textContent = '▶ Run';
      _scrapeGroupsRunning[label] = false;
      alert('Failed to start: ' + (data.error || 'unknown error'));
      return;
    }
    btn.textContent = '⏳ Running…';
    // Poll every 10s until the process exits
    const enc = encodeURIComponent(label);
    const interval = setInterval(() => {
      fetch('/api/scrape/group/status?label=' + enc).then(r => r.json()).then(s => {
        if (s.done) {
          clearInterval(interval);
          _scrapeGroupsRunning[label] = false;
          _onScrapeGroupDone(label, btn);
        }
      }).catch(() => clearInterval(interval));
    }, 10000);
    // Safety cap — stop polling after 90 minutes
    setTimeout(() => {
      clearInterval(interval);
      _scrapeGroupsRunning[label] = false;
      _onScrapeGroupDone(label, btn);
    }, 5400000);
  }).catch(() => {
    btn.disabled = false;
    btn.textContent = '▶ Run';
    _scrapeGroupsRunning[label] = false;
    alert('Network error — could not start scrape.');
  });
}

function _onScrapeGroupDone(label, btn) {
  // Refresh whichever view is currently visible
  const scrapeSection = document.getElementById('stic-scrape');
  if (scrapeSection && scrapeSection.classList.contains('active')) {
    loadScrapeGroups();   // reloads scrape page table — button resets naturally
  } else {
    // On overview (or anywhere else) — just refresh the status cards in place
    if (btn) { btn.disabled = false; btn.textContent = '▶ Run'; }
    _refreshScrapeStatusCards();
  }
}

function _refreshScrapeStatusCards() {
  // Re-render the unified overview grid (scrape group cards portion)
  // Fetch fresh group data and re-trigger the full overview card render
  fetch('/api/scrape/groups').then(r=>r.json()).then(groups => {
    const container = document.getElementById('stic-overview-cards');
    if (!container) return;
    const today = new Date().toISOString().slice(0,10);
    const yesterday = new Date(Date.now()-864e5).toISOString().slice(0,10);
    // Replace only the group cards (keep first 3 KPI cards)
    const existingKpi = Array.from(container.querySelectorAll('.kpi-card')).slice(0,3);
    let html = existingKpi.map(el => el.outerHTML).join('');
    groups.forEach(g => {
      const pct = g.sku_count > 0 ? g.scraped_count / g.sku_count : 0;
      const isToday = g.last_scraped === today;
      const dotCls = !isToday ? 'tl-red' : pct >= 0.90 ? 'tl-green' : pct >= 0.75 ? 'tl-amber' : 'tl-red';
      const dateLabel = !g.last_scraped ? 'Never' : isToday ? 'Today'
        : g.last_scraped === yesterday ? 'Yesterday' : fmtDate(g.last_scraped);
      const failed = g.sku_count - g.scraped_count;
      const failTxt = failed > 0 ? `<span style="color:#D13438">(${failed} failed)</span>` : '';
      html += `<div class="kpi-card">
        <span class="tl-dot ${dotCls}"></span>
        <div class="label">${g.label}</div>
        <div class="value" style="font-size:16px">${dateLabel}</div>
        <div class="sub">${g.scraped_count} / ${g.sku_count} SKUs ${failTxt}</div>
      </div>`;
    });
    container.innerHTML = html;
  });
}

function loadMissingResults(btn) {
  if (btn) {
    document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  }
  document.querySelectorAll('#main-stic .content-section').forEach(s=>s.classList.remove('active'));
  document.getElementById('stic-missing').classList.add('active');
  const el = document.getElementById('stic-missing-content');
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/scrape/missing').then(r=>r.json()).then(data => {
    if (!data.length) {
      el.innerHTML = '<h2 style="margin:0 0 12px">Missing Results</h2><div class="inv-empty">✅ No missing SKUs — all groups fully scraped on their last run.</div>';
      return;
    }
    let html = `<h2 style="margin:0 0 4px">Missing Results</h2>
      <p style="font-size:12px;color:#605E5C;margin:0 0 16px">SKUs that returned no data on their group's last scrape. Save a corrected STIC URL then use Scrape Now on the SKU page to fix.</p>`;

    // Group by scrape label
    const byGroup = {};
    data.forEach(r => {
      if (!byGroup[r.label]) byGroup[r.label] = { last_scraped: r.last_scraped, rows: [] };
      byGroup[r.label].rows.push(r);
    });
    Object.entries(byGroup).forEach(([label, grp]) => {
      const btnId = 'miss-scrape-btn-' + label.replace(/[^a-zA-Z0-9]/g,'-');
      const safeLabel = label.replace(/'/g,"\\'");
      html += `<div class="inv-subsection">
        <div class="inv-subsection-title" style="display:flex;align-items:center;justify-content:space-between">
          <span>${label} — last scraped ${fmtDate(grp.last_scraped)} — ${grp.rows.length} missing</span>
          <button id="${btnId}" onclick="scrapeMissingGroup('${safeLabel}',this)"
            style="padding:3px 12px;font-size:11px;font-weight:600;background:#107C10;color:#fff;border:none;border-radius:2px;cursor:pointer;text-transform:none;letter-spacing:0">▶ Scrape Missing</button>
        </div>
        <div class="tbl-wrap"><table><thead><tr>
          <th>SKU</th><th>Model</th><th>Manufacturer</th><th>STIC URL</th><th></th>
        </tr></thead><tbody>`;
      grp.rows.forEach(r => {
        const urlVal = r.stic_url ? r.stic_url : '';
        html += `<tr>
          <td><a href="#" onclick="loadSticSku(${r.product_id},'missing');return false">${r.product_id}</a></td>
          <td>${r.model_no}</td>
          <td>${r.manufacturer||'—'}</td>
          <td><input id="miss-url-${r.product_id}" type="text" value="${urlVal}"
            placeholder="Paste STIC /Product/ URL…"
            style="width:340px;padding:2px 6px;font-size:11px;border:1px solid #C8C6C4;border-radius:2px;font-family:inherit"/></td>
          <td><button onclick="saveSticUrl(${r.product_id})"
            style="padding:2px 10px;font-size:11px;background:#0078D4;color:#fff;border:none;border-radius:2px;cursor:pointer">Save</button></td>
        </tr>`;
      });
      html += '</tbody></table></div></div>';
    });
    el.innerHTML = html;
    makeSortableAll(el);
  });
}

function scrapeMissingGroup(label, btn) {
  btn.disabled = true;
  btn.textContent = '⏳ Launching…';
  fetch('/api/scrape/missing-group', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({label})
  }).then(r=>r.json()).then(data => {
    if (!data.started) {
      btn.disabled = false; btn.textContent = '▶ Scrape Missing';
      alert('Failed to start: ' + (data.error || 'unknown'));
      return;
    }
    btn.textContent = `⏳ Running… (${data.count} SKUs)`;
    const enc = encodeURIComponent(label);
    const interval = setInterval(() => {
      fetch('/api/scrape/missing-group/status?label=' + enc).then(r=>r.json()).then(s => {
        if (s.done) {
          clearInterval(interval);
          btn.disabled = false; btn.textContent = '▶ Scrape Missing';
          loadMissingResults(null);   // refresh the page — shows updated missing list
          _refreshScrapeStatusCards();
        }
      }).catch(() => clearInterval(interval));
    }, 5000);
    setTimeout(() => { clearInterval(interval); btn.disabled=false; btn.textContent='▶ Scrape Missing'; loadMissingResults(null); }, 3600000);
  }).catch(() => { btn.disabled=false; btn.textContent='▶ Scrape Missing'; alert('Network error.'); });
}

// ── Catalogue: Products view ──────────────────────────────────────────────────
function loadCatProducts(btn) {
  if (!catProductsLoaded) catProductsLoaded = true;
  showCatSection('products', btn);
  const el = document.getElementById('cat-products-content');
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/catalogue/products').then(r=>r.json()).then(data => {
    let html = '<h2 style="margin:0 0 4px">Products</h2>';
    html += `<p style="color:#605E5C;margin:0 0 12px;font-size:13px">${data.products.length} active SKUs · <a href="#" onclick="loadCatEOL();return false">View EOL</a></p>`;
    html += `<div style="display:flex;gap:8px;margin-bottom:12px">
      <input id="cat-prod-search" type="text" placeholder="Search model, description, manufacturer…"
        style="flex:1;padding:6px 10px;border:1px solid #EDEBE9;border-radius:2px;font-size:13px"
        oninput="_catProdFilter()">
    </div>`;
    html += '<p style="color:#605E5C;font-size:12px;margin:-8px 0 10px">Click any row to edit.</p>';
    html += '<div class="tbl-wrap"><table id="cat-prod-tbl"><thead><tr><th>Product</th><th>Model</th><th>Manufacturer</th><th>Group</th><th>Chipset</th><th>EAN</th><th>MSRP</th></tr></thead><tbody>';
    data.products.forEach(r => {
      const eanFlag = !r.ean ? ' style="color:#A4262C"' : '';
      html += `<tr class="clickable-row" onclick="_openProductEdit(${r.product_id}, false)">
        <td>${r.product_id}</td>
        <td>${r.model_no||'—'}</td>
        <td>${r.manufacturer||'—'}</td>
        <td>${r.product_group||'—'}</td>
        <td>${r.chipset||'—'}</td>
        <td${eanFlag}>${r.ean||'missing'}</td>
        <td>${r.msrp ? '£'+r.msrp.toFixed(2) : '<span style="color:#A4262C">—</span>'}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    el.innerHTML = html;
    makeSortable('cat-prod-tbl');
    // Set callback: update the row in-place after save
    _editCallback = (pid, p) => {
      const rows = document.querySelectorAll('#cat-prod-tbl tbody tr');
      rows.forEach(row => {
        if (row.cells[0].textContent == pid) {
          row.cells[1].textContent = p.model_no      || '—';
          row.cells[2].textContent = p.manufacturer  || '—';
          row.cells[3].textContent = p.product_group || '—';
          row.cells[4].textContent = p.chipset       || '—';
          row.cells[5].textContent = p.ean           || 'missing';
          row.cells[5].style.color = p.ean ? '' : '#A4262C';
          row.cells[6].innerHTML   = p.msrp != null ? `£${parseFloat(p.msrp).toFixed(2)}` : '<span style="color:#A4262C">—</span>';
        }
      });
      // Re-run the filter so rows that no longer match the search term disappear
      _catProdFilter();
    };
  });
}

function _catProdFilter() {
  const q = document.getElementById('cat-prod-search').value.toLowerCase();
  document.querySelectorAll('#cat-prod-tbl tbody tr').forEach(row => {
    row.style.display = (!q || row.textContent.toLowerCase().includes(q)) ? '' : 'none';
  });
}

// ── Catalogue: Retailer IDs view ──────────────────────────────────────────────
let _catRetRows = [];
let _catRetSort = { col: 'model_no', dir: 1 };  // default: model name ascending

const _CAT_RET_COLS = [
  { key: 'product_id', label: 'Product' },
  { key: 'model_no',   label: 'Model' },
  { key: 'amazon_asin', label: 'Amazon ASIN' },
  { key: 'currys_sku',  label: 'Currys SKU' },
  { key: 'very_sku',    label: 'Very SKU' },
  { key: 'argos_sku',   label: 'Argos SKU' },
  { key: 'ocuk_code',   label: 'OCUK Code' },
  { key: 'scan_ln',     label: 'Scan LN' },
  { key: 'scan_url',    label: 'Scan URL',   link: true },
  { key: 'awdit_url',   label: 'AWD-IT URL', link: true },
  { key: 'ccl_url',     label: 'CCL URL',    link: true },
  { key: 'box_url',     label: 'Box URL',    link: true },
  { key: 'very_url',    label: 'Very URL',   link: true },
];

function loadCatRetailerIds(btn) {
  showCatSection('retailer-ids', btn);
  const el = document.getElementById('cat-retailer-ids-content');
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/catalogue/retailer-ids').then(r=>r.json()).then(data => {
    _catRetRows = data.rows;
    let html = '<h2 style="margin:0 0 4px">Retailer IDs</h2>';
    html += `<p style="color:#605E5C;margin:0 0 12px;font-size:13px">${data.rows.length} products · IDs used by the retailer scraper to locate products on each site.</p>`;
    html += `<div style="display:flex;gap:8px;margin-bottom:12px">
      <input id="cat-ret-search" type="text" placeholder="Search product, model, ASIN, URL…"
        style="flex:1;padding:6px 10px;border:1px solid #EDEBE9;border-radius:2px;font-size:13px"
        oninput="_catRetRender()">
    </div>`;
    html += '<div class="tbl-wrap"><table id="cat-ret-tbl"><thead><tr>';
    _CAT_RET_COLS.forEach(c => {
      const active = _catRetSort.col === c.key;
      const arrow  = active ? (_catRetSort.dir === 1 ? ' ▲' : ' ▼') : '';
      html += `<th style="cursor:pointer;user-select:none;white-space:nowrap${active ? ';color:#0078D4' : ''}" onclick="_catRetSortBy('${c.key}')">${c.label}${arrow}</th>`;
    });
    html += '</tr></thead><tbody id="cat-ret-tbody"></tbody></table></div>';
    el.innerHTML = html;
    _catRetRender();
  });
}

function _catRetSortBy(col) {
  if (_catRetSort.col === col) {
    _catRetSort.dir *= -1;
  } else {
    _catRetSort.col = col;
    _catRetSort.dir = 1;
  }
  // Refresh header arrows
  const ths = document.querySelectorAll('#cat-ret-tbl thead th');
  _CAT_RET_COLS.forEach((c, i) => {
    const active = _catRetSort.col === c.key;
    const arrow  = active ? (_catRetSort.dir === 1 ? ' ▲' : ' ▼') : '';
    ths[i].textContent = c.label + arrow;
    ths[i].style.color = active ? '#0078D4' : '';
  });
  _catRetRender();
}

function _catRetRender() {
  const q = (document.getElementById('cat-ret-search')?.value || '').toLowerCase();
  const { col, dir } = _catRetSort;
  const rows = _catRetRows
    .filter(r => !q || Object.values(r).some(v => v && String(v).toLowerCase().includes(q)))
    .slice()
    .sort((a, b) => {
      const av = (a[col] || ''), bv = (b[col] || '');
      return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
    });
  const tbody = document.getElementById('cat-ret-tbody');
  if (!tbody) return;
  tbody.innerHTML = rows.map(r => `<tr>
    ${_CAT_RET_COLS.map(c => c.link
      ? `<td>${r[c.key] ? `<a href="${r[c.key]}" target="_blank">↗</a>` : '—'}</td>`
      : `<td>${r[c.key] || '—'}</td>`).join('')}
  </tr>`).join('');
}

// kept for compatibility if called elsewhere
function _catRetFilter() { _catRetRender(); }

// ── Catalogue: Import / Export ────────────────────────────────────────────────

// Tool registry — add new tools here as they are built
const IE_TOOLS = [
  {
    id:          'new-skus',
    icon:        '📥',
    name:        'Add / Update SKUs',
    desc:        'Import a CSV to add new SKUs or update existing product details. EOL status is not touched.',
    type:        'import',
    hasTemplate: true,
    headers:     'Product,model_no,manufacturer,product_group,description,chipset,ean',
  },
  {
    id:          'eol-status',
    icon:        '🔄',
    name:        'Update EOL Status',
    desc:        'Import a CSV to bulk-update EOL flags from your product status data.',
    type:        'import',
    hasTemplate: true,
    headers:     'Product,Product_Status',
  },
  {
    id:   'export-skus',
    icon: '📤',
    name: 'Export Active SKUs',
    desc: 'Download all active (non-EOL) SKUs as CSV — useful for bulk review or editing before re-importing.',
    type: 'export',
  },
  {
    id:          'retailer-ids-import',
    icon:        '📥',
    name:        'Import Retailer IDs',
    desc:        'Import a CSV to add or update retailer-specific IDs (ASINs, SKUs, URLs) for each product.',
    type:        'import',
    hasTemplate: true,
    headers:     'Product,amazon_asin,currys_sku,very_sku,argos_sku,ccl_url,awdit_url,scan_ln,scan_url,ocuk_code,box_url',
  },
  {
    id:   'retailer-ids-export',
    icon: '📤',
    name: 'Export Retailer IDs',
    desc: 'Download all retailer IDs as CSV — edit and re-import to update codes in bulk.',
    type: 'export',
  },
  {
    id:          'msrp-by-vip',
    icon:        '💷',
    name:        'MSRP — by VIP Code',
    desc:        'CSV columns: Product, MSRP. Matches on VIP product code. Use for supplier sheets that include your VIP codes.',
    type:        'import',
    hasTemplate: true,
    headers:     'Product,MSRP',
  },
  {
    id:          'msrp-by-ean',
    icon:        '💷',
    name:        'MSRP — by EAN',
    desc:        'CSV columns: EAN, MSRP. Matches on EAN/barcode. Use when the supplier sheet uses EAN codes.',
    type:        'import',
    hasTemplate: true,
    headers:     'EAN,MSRP',
  },
  {
    id:          'msrp-by-model',
    icon:        '💷',
    name:        'MSRP — by Model',
    desc:        'CSV columns: Model, MSRP. Matches on model number. Use when the supplier sheet lists product model names.',
    type:        'import',
    hasTemplate: true,
    headers:     'Model,MSRP',
  },
];

// ── Catalogue: Missing MSRP report ───────────────────────────────────────────
function loadMissingMsrp(btn) {
  showCatSection('missing-msrp', btn);
  _missingMsrpRender();
}

function _missingMsrpRender() {
  const el = document.getElementById('cat-missing-msrp-content');
  const mfr   = document.getElementById('mm-mfr')   ? document.getElementById('mm-mfr').value   : '';
  const grp   = document.getElementById('mm-grp')   ? document.getElementById('mm-grp').value   : '';
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch(`/api/catalogue/missing-msrp?mfr=${encodeURIComponent(mfr)}&grp=${encodeURIComponent(grp)}`)
    .then(r => r.json()).then(data => {
      const grpLabel = { PROD_VIDEO: 'GPU', PROD_MBRD: 'Motherboard', PROD_MBRDS: 'Server/Pro' };
      let html = '<h2 style="margin:0 0 4px">Missing MSRP Report</h2>';

      // Summary table
      html += '<div style="margin-bottom:16px">';
      html += '<table style="border-collapse:collapse;font-size:13px"><thead><tr>'
        + '<th style="text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #EDEBE9">Manufacturer</th>'
        + '<th style="text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #EDEBE9">Group</th>'
        + '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid #EDEBE9">Total</th>'
        + '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid #EDEBE9">Has MSRP</th>'
        + '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid #EDEBE9;color:#A4262C">Missing</th>'
        + '</tr></thead><tbody>';
      data.summary.forEach(s => {
        const pct = s.total ? Math.round(100 * s.has_msrp / s.total) : 0;
        const bar = `<span style="display:inline-block;width:${pct}px;max-width:80px;height:6px;background:#107C10;border-radius:2px;vertical-align:middle"></span>`;
        html += `<tr>
          <td style="padding:4px 12px 4px 0">${s.manufacturer}</td>
          <td style="padding:4px 12px 4px 0">${grpLabel[s.product_group]||s.product_group}</td>
          <td style="text-align:right;padding:4px 8px">${s.total}</td>
          <td style="text-align:right;padding:4px 8px">${bar} ${s.has_msrp}</td>
          <td style="text-align:right;padding:4px 8px;color:${s.missing>0?'#A4262C':'#107C10'};font-weight:${s.missing>0?'600':'400'}">${s.missing||'✓'}</td>
        </tr>`;
      });
      html += '</tbody></table></div>';

      // Filters
      const mfrs = [...new Set(data.summary.map(s => s.manufacturer))].sort();
      const grps = [...new Set(data.summary.map(s => s.product_group))].sort();
      html += `<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
        <select id="mm-mfr" onchange="_missingMsrpRender()" style="padding:5px 8px;border:1px solid #EDEBE9;border-radius:2px;font-size:13px">
          <option value="">All Manufacturers</option>
          ${mfrs.map(m => `<option value="${m}"${m===mfr?' selected':''}>${m}</option>`).join('')}
        </select>
        <select id="mm-grp" onchange="_missingMsrpRender()" style="padding:5px 8px;border:1px solid #EDEBE9;border-radius:2px;font-size:13px">
          <option value="">All Groups</option>
          ${grps.map(g => `<option value="${g}"${g===grp?' selected':''}>${grpLabel[g]||g}</option>`).join('')}
        </select>
        <span style="font-size:12px;color:#605E5C">${data.products.length} product${data.products.length!==1?'s':''} missing MSRP</span>
        <button onclick="_exportTableCsv('mm-tbl','missing-msrp.csv')"
          style="margin-left:auto;padding:5px 12px;border:1px solid #C8C6C4;background:#fff;border-radius:2px;font-size:12px;cursor:pointer">
          📥 Export CSV
        </button>
      </div>`;

      // Product rows
      if (data.products.length === 0) {
        html += '<p style="color:#107C10;font-weight:600">✓ All products have an MSRP for the selected filter.</p>';
      } else {
        html += '<p style="color:#605E5C;font-size:12px;margin:-4px 0 10px">Click any row to add MSRP or edit fields.</p>';
        html += '<div class="tbl-wrap"><table id="mm-tbl"><thead><tr>'
          + '<th>Product</th><th>Model</th><th>Manufacturer</th><th>Group</th><th>Chipset</th><th>EAN</th>'
          + '</tr></thead><tbody>';
        data.products.forEach(r => {
          const eanStyle = !r.ean ? 'color:#A4262C' : '';
          html += `<tr class="clickable-row" onclick="_openProductEdit(${r.product_id}, true)">
            <td>${r.product_id}</td>
            <td>${r.model_no||'—'}</td>
            <td>${r.manufacturer||'—'}</td>
            <td>${grpLabel[r.product_group]||r.product_group||'—'}</td>
            <td>${r.chipset||'—'}</td>
            <td style="${eanStyle}">${r.ean||'missing'}</td>
          </tr>`;
        });
        html += '</tbody></table></div>';
      }
      el.innerHTML = html;
      makeSortable('mm-tbl');
      // After save: remove the row from the missing list and refresh summary
      _editCallback = (pid, p) => {
        if (p.msrp != null && p.msrp > 0) {
          const rows = document.querySelectorAll('#mm-tbl tbody tr');
          rows.forEach(row => { if (row.cells[0].textContent == pid) row.remove(); });
          // Update count text
          const remaining = document.querySelectorAll('#mm-tbl tbody tr').length;
          const countEl = document.querySelector('#cat-missing-msrp-content span[data-mm-count]');
          if (countEl) countEl.textContent = `${remaining} product${remaining!==1?'s':''} missing MSRP`;
          // Refresh summary
          _missingMsrpRender();
        }
      };
    });
}

// ── Catalogue: Missing EAN report ────────────────────────────────────────────
function loadMissingEan(btn) {
  showCatSection('missing-ean', btn);
  _missingEanRender();
}

function _missingEanRender() {
  const el  = document.getElementById('cat-missing-ean-content');
  const mfr = document.getElementById('me-mfr') ? document.getElementById('me-mfr').value : '';
  const grp = document.getElementById('me-grp') ? document.getElementById('me-grp').value : '';
  el.innerHTML = '<div class="spinner">Loading…</div>';
  fetch(`/api/catalogue/missing-ean?mfr=${encodeURIComponent(mfr)}&grp=${encodeURIComponent(grp)}`)
    .then(r => r.json()).then(data => {
      const grpLabel = { PROD_VIDEO: 'GPU', PROD_MBRD: 'Motherboard', PROD_MBRDS: 'Server/Pro' };
      let html = '<h2 style="margin:0 0 4px">Missing EAN Report</h2>';

      // Summary table
      html += '<div style="margin-bottom:16px">';
      html += '<table style="border-collapse:collapse;font-size:13px"><thead><tr>'
        + '<th style="text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #EDEBE9">Manufacturer</th>'
        + '<th style="text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #EDEBE9">Group</th>'
        + '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid #EDEBE9">Total</th>'
        + '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid #EDEBE9">Has EAN</th>'
        + '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid #EDEBE9;color:#A4262C">Missing</th>'
        + '</tr></thead><tbody>';
      data.summary.forEach(s => {
        const pct = s.total ? Math.round(80 * s.has_ean / s.total) : 0;
        const bar = `<span style="display:inline-block;width:${pct}px;max-width:80px;height:6px;background:#107C10;border-radius:2px;vertical-align:middle"></span>`;
        html += `<tr>
          <td style="padding:4px 12px 4px 0">${s.manufacturer}</td>
          <td style="padding:4px 12px 4px 0">${grpLabel[s.product_group]||s.product_group}</td>
          <td style="text-align:right;padding:4px 8px">${s.total}</td>
          <td style="text-align:right;padding:4px 8px">${bar} ${s.has_ean}</td>
          <td style="text-align:right;padding:4px 8px;color:${s.missing>0?'#A4262C':'#107C10'};font-weight:${s.missing>0?'600':'400'}">${s.missing||'✓'}</td>
        </tr>`;
      });
      html += '</tbody></table></div>';

      // Filters
      const mfrs = [...new Set(data.summary.map(s => s.manufacturer))].sort();
      const grps = [...new Set(data.summary.map(s => s.product_group))].sort();
      html += `<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
        <select id="me-mfr" onchange="_missingEanRender()" style="padding:5px 8px;border:1px solid #EDEBE9;border-radius:2px;font-size:13px">
          <option value="">All Manufacturers</option>
          ${mfrs.map(m => `<option value="${m}"${m===mfr?' selected':''}>${m}</option>`).join('')}
        </select>
        <select id="me-grp" onchange="_missingEanRender()" style="padding:5px 8px;border:1px solid #EDEBE9;border-radius:2px;font-size:13px">
          <option value="">All Groups</option>
          ${grps.map(g => `<option value="${g}"${g===grp?' selected':''}>${grpLabel[g]||g}</option>`).join('')}
        </select>
        <span style="font-size:12px;color:#605E5C">${data.products.length} product${data.products.length!==1?'s':''} missing EAN</span>
        <button onclick="_exportTableCsv('me-tbl','missing-ean.csv')"
          style="margin-left:auto;padding:5px 12px;border:1px solid #C8C6C4;background:#fff;border-radius:2px;font-size:12px;cursor:pointer">
          📥 Export CSV
        </button>
      </div>`;

      // Product rows
      if (data.products.length === 0) {
        html += '<p style="color:#107C10;font-weight:600">✓ All products have an EAN for the selected filter.</p>';
      } else {
        html += '<p style="color:#605E5C;font-size:12px;margin:-4px 0 10px">Click any row to add EAN or edit fields.</p>';
        html += '<div class="tbl-wrap"><table id="me-tbl"><thead><tr>'
          + '<th>Product</th><th>Model</th><th>Manufacturer</th><th>Group</th><th>Chipset</th><th>MSRP</th>'
          + '</tr></thead><tbody>';
        data.products.forEach(r => {
          html += `<tr class="clickable-row" onclick="_openProductEditFocus(${r.product_id}, 'ep-ean')">
            <td>${r.product_id}</td>
            <td>${r.model_no||'—'}</td>
            <td>${r.manufacturer||'—'}</td>
            <td>${grpLabel[r.product_group]||r.product_group||'—'}</td>
            <td>${r.chipset||'—'}</td>
            <td>${r.msrp != null ? '£'+parseFloat(r.msrp).toFixed(2) : '<span style="color:#A19F9D">—</span>'}</td>
          </tr>`;
        });
        html += '</tbody></table></div>';
      }
      el.innerHTML = html;
      makeSortable('me-tbl');

      // After save: remove the row if EAN now set
      _editCallback = (pid, p) => {
        if (p.ean) {
          document.querySelectorAll('#me-tbl tbody tr').forEach(row => {
            if (row.cells[0].textContent == pid) row.remove();
          });
          _missingEanRender();
        }
      };
    });
}

let _ieCurrentTool = null;    // tool id currently open
let _iePreviewRows  = [];     // validated rows from last preview, ready to confirm

function loadCatImportExport(btn, toolId) {
  showCatSection('import-export', btn);
  _ieCurrentTool = null;
  if (toolId) {
    _ieOpenTool(toolId);
  } else {
    _renderIeToolCards();
  }
}

function _renderIeToolCards() {
  const el = document.getElementById('cat-import-export-content');
  let html = '<h2 style="margin:0 0 4px">Import / Export</h2>';
  html += '<p style="color:#605E5C;margin:0 0 16px;font-size:13px">Select a tool below. Import tools accept a CSV file; export tools download data directly. New tools will be added here over time.</p>';
  html += '<div class="ie-tool-cards">';
  IE_TOOLS.forEach(t => {
    if (t.type === 'export') {
      html += `<div class="ie-tool-card" onclick="_ieExport('${t.id}')">
        <div class="ie-icon">${t.icon}</div>
        <div class="ie-name">${t.name}</div>
        <div class="ie-desc">${t.desc}</div>
        <span class="ie-tag ie-tag-export">Export</span>
      </div>`;
    } else {
      html += `<div class="ie-tool-card" onclick="_ieOpenTool('${t.id}')">
        <div class="ie-icon">${t.icon}</div>
        <div class="ie-name">${t.name}</div>
        <div class="ie-desc">${t.desc}</div>
        <span class="ie-tag ie-tag-import">Import</span>
      </div>`;
    }
  });
  html += '</div>';
  el.innerHTML = html;
}

function _ieOpenTool(toolId) {
  _ieCurrentTool = toolId;
  _iePreviewRows  = [];
  const tool = IE_TOOLS.find(t => t.id === toolId);
  if (!tool) return;
  const el = document.getElementById('cat-import-export-content');
  const templateBtn = tool.hasTemplate
    ? `<a href="/api/import/template/${toolId}" download class="ie-btn ie-btn-secondary" style="text-decoration:none">↓ Download CSV Template</a>`
    : '';
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
      <button class="ie-btn ie-btn-secondary" onclick="_renderIeToolCards()">← Back</button>
      <h2 style="margin:0">${tool.icon} ${tool.name}</h2>
    </div>
    <p style="color:#605E5C;margin:0 0 14px;font-size:13px">${tool.desc}</p>

    ${tool.headers ? `<div style="margin-bottom:14px;background:#fff;border:1px solid #EDEBE9;border-radius:2px;padding:10px 12px">
      <div style="font-size:11px;font-weight:600;color:#A19F9D;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px">Required CSV Headers</div>
      <div style="display:flex;align-items:center;gap:8px">
        <code id="ie-headers-${toolId}" style="flex:1;font-size:12px;background:#F3F2F1;padding:5px 9px;border-radius:2px;border:1px solid #EDEBE9;color:#323130;user-select:all;cursor:text">${tool.headers}</code>
        <button class="ie-btn ie-btn-secondary" onclick="_ieCopyHeaders('${toolId}')" id="ie-copy-btn-${toolId}" style="white-space:nowrap;flex-shrink:0">Copy</button>
      </div>
      <div style="font-size:11px;color:#A19F9D;margin-top:5px">${tool.headersNote || 'Paste this as the first row of your CSV, or use the template below — column order doesn\'t matter as long as the names match exactly.'}</div>
    </div>` : ''}

    ${tool.hasTemplate ? `<div style="margin-bottom:14px">${templateBtn}
      <span style="font-size:11px;color:#A19F9D;margin-left:10px">Opens in Excel pre-formatted. Fill in your data, save as CSV, then upload below.</span>
    </div>` : ''}

    <div class="ie-dropzone" id="ie-dz-${toolId}"
         onclick="document.getElementById('ie-file-${toolId}').click()"
         ondragover="event.preventDefault();this.classList.add('drag-over')"
         ondragleave="this.classList.remove('drag-over')"
         ondrop="_ieDrop(event,'${toolId}')">
      <input type="file" id="ie-file-${toolId}" accept=".csv,.xlsx,.xls,text/csv"
             onchange="_ieFileSelected(this,'${toolId}')">
      <div class="dz-icon">📂</div>
      <div class="dz-text">Click to browse or drag &amp; drop a file here</div>
      <div class="dz-hint">CSV or Excel (.xlsx / .xls) · Max 5 000 rows</div>
    </div>
    <div id="ie-preview-${toolId}" style="margin-top:16px"></div>
  `;
}

function _ieCopyHeaders(toolId) {
  const code = document.getElementById('ie-headers-' + toolId);
  const btn  = document.getElementById('ie-copy-btn-' + toolId);
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = '✓ Copied';
    btn.style.color = '#107C10';
    btn.style.borderColor = '#107C10';
    setTimeout(() => { btn.textContent = 'Copy'; btn.style.color = ''; btn.style.borderColor = ''; }, 2000);
  }).catch(() => {
    // Fallback for older browsers
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(code);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    sel.removeAllRanges();
    btn.textContent = '✓ Copied';
    setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
  });
}

function _ieDrop(event, toolId) {
  event.preventDefault();
  document.getElementById('ie-dz-' + toolId).classList.remove('drag-over');
  const file = event.dataTransfer.files[0];
  if (file) _ieReadFile(file, toolId);
}

function _ieFileSelected(input, toolId) {
  if (input.files[0]) _ieReadFile(input.files[0], toolId);
}

function _ieReadFile(file, toolId) {
  const dz = document.getElementById('ie-dz-' + toolId);
  dz.querySelector('.dz-text').textContent = `Reading: ${file.name}…`;
  const ext = file.name.split('.').pop().toLowerCase();
  const reader = new FileReader();
  reader.onerror = () => { dz.querySelector('.dz-text').textContent = 'Error reading file.'; };

  if (ext === 'xlsx' || ext === 'xls') {
    // Excel: read as ArrayBuffer, convert first sheet → CSV via SheetJS
    reader.onload = e => {
      try {
        const wb  = XLSX.read(new Uint8Array(e.target.result), { type: 'array' });
        const ws  = wb.Sheets[wb.SheetNames[0]];
        const csv = XLSX.utils.sheet_to_csv(ws, { blankrows: false });
        _ieUploadForPreview(toolId, csv, file.name);
      } catch(err) {
        dz.querySelector('.dz-text').textContent = 'Failed to parse Excel file.';
        console.error('SheetJS error:', err);
      }
    };
    reader.readAsArrayBuffer(file);
  } else {
    // CSV: read as plain text; server strips BOM
    reader.onload = e => _ieUploadForPreview(toolId, e.target.result, file.name);
    reader.readAsText(file);
  }
}

function _ieUploadForPreview(toolId, csvText, filename) {
  const previewEl = document.getElementById('ie-preview-' + toolId);
  previewEl.innerHTML = '<div class="spinner">Parsing…</div>';
  fetch('/api/import/' + toolId + '/preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({csv: csvText, filename})
  })
  .then(r => r.json())
  .then(data => _ieRenderPreview(toolId, data))
  .catch(() => {
    previewEl.innerHTML = '<p style="color:#A4262C;padding:12px">Network error — could not parse file.</p>';
  });
}

function _ieRenderPreview(toolId, data) {
  // ── MSRP import tools: delegate to shared renderer ───────────────────────────
  if (['msrp-by-vip','msrp-by-ean','msrp-by-model'].includes(toolId)) {
    return _ieRenderMsrpPreview(toolId, data);
  }

  const previewEl = document.getElementById('ie-preview-' + toolId);
  if (data.error) {
    previewEl.innerHTML = `<p style="color:#A4262C;padding:12px">❌ ${data.error}</p>`;
    return;
  }
  const s = data.summary;
  _iePreviewRows = data.valid_rows;

  // ── eol-status tool: custom summary and table layout ────────────────────────
  if (toolId === 'eol-status') {
    let html = `<div class="ie-summary">
      <span>Total rows: <strong>${s.total}</strong></span>
      <span style="color:#107C10">→ Set Active: <strong>${s.set_active||0}</strong></span>
      <span style="color:#A4262C">→ Set EOL: <strong>${s.set_eol||0}</strong></span>
      ${s.no_change ? `<span style="color:#A19F9D">No change: <strong>${s.no_change}</strong></span>` : ''}
      ${s.not_found ? `<span style="color:#A19F9D">Not in DB: <strong>${s.not_found}</strong></span>` : ''}
      ${s.errors  ? `<span style="color:#A4262C">⚠ Errors: <strong>${s.errors}</strong></span>` : ''}
    </div>`;

    if (_iePreviewRows.length) {
      html += `<div class="ie-action-bar">
        <button class="ie-btn ie-btn-success" id="ie-confirm-btn-${toolId}"
                onclick="_ieConfirm('${toolId}')">✓ Confirm Update (${_iePreviewRows.length} rows)</button>
        <button class="ie-btn ie-btn-secondary" onclick="_ieOpenTool('${toolId}')">✕ Cancel</button>
      </div>`;
    }

    const allRows = [...(data.valid_rows||[]), ...(data.error_rows||[])];
    if (allRows.length) {
      html += `<div class="tbl-wrap"><table>
        <thead><tr>
          <th>Action</th><th>VIP Code</th><th>Model</th><th>Product Status</th>
          <th>Current EOL</th><th>New EOL</th><th>Note</th>
        </tr></thead><tbody>`;
      allRows.forEach(r => {
        const actionPill =
            r._action === 'set_active'  ? '<span class="ie-new">SET ACTIVE</span>'
          : r._action === 'set_eol'     ? '<span class="ie-error">SET EOL</span>'
          : r._action === 'no_change'   ? '<span style="color:#A19F9D;font-size:11px">no change</span>'
          : r._action === 'not_found'   ? '<span style="color:#A19F9D;font-size:11px">not in DB</span>'
          :                               '<span class="ie-error">ERROR</span>';
        const curEol = r.current_eol === 1 ? '<span class="ie-error">EOL</span>'
                     : r.current_eol === 0 ? '<span class="ie-new">Active</span>' : '—';
        const newEol = r.new_eol === 1 ? '<span class="ie-error">EOL</span>'
                     : r.new_eol === 0 ? '<span class="ie-new">Active</span>' : '—';
        html += `<tr>
          <td>${actionPill}</td>
          <td>${r.product_id||'—'}</td>
          <td style="color:#605E5C">${r.model_no||'—'}</td>
          <td style="text-align:center">${r.product_status??'—'}</td>
          <td style="text-align:center">${curEol}</td>
          <td style="text-align:center">${newEol}</td>
          <td style="color:#605E5C;font-size:11px">${r._note||''}</td>
        </tr>`;
      });
      html += '</tbody></table></div>';
    }

    if (!_iePreviewRows.length && !data.error_rows?.length) {
      html += '<p style="color:#A19F9D;padding:12px">No valid rows found in the file.</p>';
    }
    previewEl.innerHTML = html;
    return;
  }

  // ── retailer-ids-import tool: custom preview layout ─────────────────────────
  if (toolId === 'retailer-ids-import') {
    const inserts = _iePreviewRows.filter(r => r.action === 'insert').length;
    const updates = _iePreviewRows.filter(r => r.action === 'update').length;
    let html = `<div class="ie-summary">
      <span>Total rows: <strong>${s.total}</strong></span>
      <span style="color:#107C10">✚ New: <strong>${inserts}</strong></span>
      <span style="color:#0078D4">↻ Update: <strong>${updates}</strong></span>
      ${s.errors ? `<span style="color:#A4262C">⚠ Errors: <strong>${s.errors}</strong></span>` : ''}
    </div>`;
    if (data.errors?.length) {
      html += `<div style="padding:8px 12px;background:#FDF3F2;border:1px solid #F1B8B3;border-radius:2px;margin-bottom:12px;font-size:12px;color:#A4262C">
        ${data.errors.slice(0,5).map(e=>`<div>${e}</div>`).join('')}
        ${data.errors.length > 5 ? `<div>…and ${data.errors.length-5} more</div>` : ''}
      </div>`;
    }
    if (_iePreviewRows.length) {
      html += `<div class="ie-action-bar">
        <button class="ie-btn ie-btn-success" id="ie-confirm-btn-${toolId}"
                onclick="_ieConfirm('${toolId}')">✓ Confirm Update (${_iePreviewRows.length} rows)</button>
        <button class="ie-btn ie-btn-secondary" onclick="_ieOpenTool('${toolId}')">✕ Cancel</button>
      </div>`;
      html += '<div class="tbl-wrap"><table><thead><tr><th>Status</th><th>Product</th><th>Fields Updated</th></tr></thead><tbody>';
      _iePreviewRows.slice(0, 100).forEach(r => {
        const tag = r.action === 'insert' ? '<span class="ie-new">NEW</span>' : '<span class="ie-update">UPDATE</span>';
        const fields = Object.keys(r.updates).join(', ');
        html += `<tr><td>${tag}</td><td>${r.product_id}</td><td style="font-size:11px;color:#605E5C">${fields}</td></tr>`;
      });
      html += '</tbody></table></div>';
    } else {
      html += '<p style="color:#A19F9D;padding:12px">No valid rows found in the file.</p>';
    }
    previewEl.innerHTML = html;
    return;
  }

  // ── Default preview layout (new-skus and future tools) ──────────────────────
  let html = `<div class="ie-summary">
    <span>Total rows: <strong>${s.total}</strong></span>
    <span style="color:#107C10">✚ New: <strong>${s.new}</strong></span>
    <span style="color:#0078D4">↻ Update: <strong>${s.update}</strong></span>
    ${s.errors ? `<span style="color:#A4262C">⚠ Errors: <strong>${s.errors}</strong></span>` : ''}
    ${s.warnings ? `<span style="color:#8A4B00">⚠ Warnings: <strong>${s.warnings}</strong></span>` : ''}
  </div>`;

  if (_iePreviewRows.length) {
    html += `<div class="ie-action-bar">
      <button class="ie-btn ie-btn-success" id="ie-confirm-btn-${toolId}"
              onclick="_ieConfirm('${toolId}')">✓ Confirm Import (${_iePreviewRows.length} rows)</button>
      <button class="ie-btn ie-btn-secondary" onclick="_ieOpenTool('${toolId}')">✕ Cancel</button>
    </div>`;
  }

  // Preview table — all rows including errors
  const allRows = [...(data.valid_rows || []), ...(data.error_rows || [])];
  if (allRows.length) {
    html += `<div class="tbl-wrap"><table>
      <thead><tr>
        <th>Status</th><th>VIP Code</th><th>Model</th><th>Manufacturer</th>
        <th>Group</th><th>Description</th><th>Chipset</th><th>EAN</th><th>Note</th>
      </tr></thead><tbody>`;
    allRows.forEach(r => {
      const stPill = r._status === 'new'    ? '<span class="ie-new">NEW</span>'
                   : r._status === 'update' ? '<span class="ie-update">UPDATE</span>'
                   : r._status === 'warn'   ? '<span class="ie-warn">WARN</span>'
                   :                          '<span class="ie-error">ERROR</span>';
      html += `<tr>
        <td>${stPill}</td>
        <td>${r.product_id||'—'}</td>
        <td>${r.model_no||'—'}</td>
        <td>${r.manufacturer||'—'}</td>
        <td>${r.product_group||'—'}</td>
        <td>${r.description||''}</td>
        <td>${r.chipset||''}</td>
        <td>${r.ean||''}</td>
        <td style="color:#605E5C;font-size:11px">${r._note||''}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  }

  if (!_iePreviewRows.length && !data.error_rows?.length) {
    html += '<p style="color:#A19F9D;padding:12px">No valid rows found in the file.</p>';
  }

  previewEl.innerHTML = html;
}

function _ieConfirm(toolId) {
  if (!_iePreviewRows.length) return;
  const btn = document.getElementById('ie-confirm-btn-' + toolId);
  btn.disabled = true;
  btn.textContent = '⏳ Importing…';
  fetch('/api/import/' + toolId + '/confirm', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rows: _iePreviewRows})
  })
  .then(r => r.json())
  .then(data => {
    const previewEl = document.getElementById('ie-preview-' + toolId);
    if (data.error) {
      previewEl.innerHTML = `<p style="color:#A4262C;padding:12px">❌ Import failed: ${data.error}</p>`;
      return;
    }
    let resultHtml = '<div class="ie-summary" style="border-color:#107C10">';
    if (toolId === 'eol-status') {
      resultHtml += `
        <span style="color:#107C10;font-weight:600;font-size:14px">✓ EOL status updated</span>
        <span style="color:#107C10">Set Active: <strong>${data.set_active}</strong></span>
        <span style="color:#A4262C">Set EOL: <strong>${data.set_eol}</strong></span>
        ${data.skipped ? `<span style="color:#A19F9D">Skipped: <strong>${data.skipped}</strong></span>` : ''}`;
    } else if (toolId === 'retailer-ids-import') {
      resultHtml += `
        <span style="color:#107C10;font-weight:600;font-size:14px">✓ Retailer IDs updated</span>
        <span style="color:#0078D4">Updated: <strong>${data.updated}</strong></span>`;
    } else if (['msrp-by-vip','msrp-by-ean','msrp-by-model'].includes(toolId)) {
      resultHtml += `
        <span style="color:#107C10;font-weight:600;font-size:14px">✓ MSRPs updated</span>
        <span style="color:#107C10">Set: <strong>${data.updated}</strong></span>
        ${data.skipped ? `<span style="color:#A19F9D">No change: <strong>${data.skipped}</strong></span>` : ''}
        ${data.not_found ? `<span style="color:#A19F9D">Not matched: <strong>${data.not_found}</strong></span>` : ''}`;
    } else {
      resultHtml += `
        <span style="color:#107C10;font-weight:600;font-size:14px">✓ Import complete</span>
        <span style="color:#107C10">Added: <strong>${data.added}</strong></span>
        <span style="color:#0078D4">Updated: <strong>${data.updated}</strong></span>
        ${data.skipped ? `<span style="color:#A19F9D">Skipped: <strong>${data.skipped}</strong></span>` : ''}`;
    }
    resultHtml += `</div>
      <p style="color:#605E5C;font-size:12px;padding:0 0 12px">Changes are live immediately.</p>
      <button class="ie-btn ie-btn-secondary" onclick="_renderIeToolCards()">← Back to tools</button>`;
    previewEl.innerHTML = resultHtml;
    _iePreviewRows = [];
  })
  .catch(() => {
    btn.disabled = false;
    btn.textContent = '✓ Confirm Import';
    alert('Network error — import may not have completed.');
  });
}

function _ieExport(toolId) {
  if (toolId === 'export-skus')         window.location.href = '/api/export/skus';
  if (toolId === 'retailer-ids-export') window.location.href = '/api/export/retailer-ids';
}

// ── MSRP import preview renderer (shared for all 3 MSRP tools) ───────────────
function _ieRenderMsrpPreview(toolId, data) {
  const previewEl = document.getElementById('ie-preview-' + toolId);
  if (data.error) {
    previewEl.innerHTML = `<p style="color:#A4262C;padding:12px">❌ ${data.error}</p>`;
    return;
  }
  const s = data.summary;
  _iePreviewRows = data.valid_rows;

  const keyLabel = toolId === 'msrp-by-vip'   ? 'VIP Code'
                 : toolId === 'msrp-by-ean'   ? 'EAN'
                 :                              'Model';

  let html = `<div class="ie-summary">
    <span>Total rows: <strong>${s.total}</strong></span>
    <span style="color:#107C10">✔ Matched: <strong>${s.matched}</strong></span>
    ${s.no_change  ? `<span style="color:#A19F9D">No change: <strong>${s.no_change}</strong></span>` : ''}
    ${s.not_found  ? `<span style="color:#A19F9D">Not found: <strong>${s.not_found}</strong></span>` : ''}
    ${s.bad_value  ? `<span style="color:#A4262C">⚠ Bad value: <strong>${s.bad_value}</strong></span>` : ''}
  </div>`;

  if (_iePreviewRows.length) {
    html += `<div class="ie-action-bar">
      <button class="ie-btn ie-btn-success" id="ie-confirm-btn-${toolId}"
              onclick="_ieConfirm('${toolId}')">✓ Confirm — Set ${_iePreviewRows.length} MSRPs</button>
      <button class="ie-btn ie-btn-secondary" onclick="_ieOpenTool('${toolId}')">✕ Cancel</button>
    </div>`;
  }

  const allRows = [...(data.valid_rows||[]), ...(data.error_rows||[])];
  if (allRows.length) {
    html += `<div class="tbl-wrap"><table>
      <thead><tr>
        <th>${keyLabel}</th><th>Model</th><th>Manufacturer</th>
        <th style="text-align:right">Current MSRP</th>
        <th style="text-align:right">New MSRP</th>
        <th>Status</th>
      </tr></thead><tbody>`;
    allRows.forEach(r => {
      const status =
          r._action === 'update'    ? '<span class="ie-new">UPDATE</span>'
        : r._action === 'no_change' ? '<span style="color:#A19F9D;font-size:11px">no change</span>'
        : r._action === 'not_found' ? '<span style="color:#A19F9D;font-size:11px">not found</span>'
        : r._action === 'bad_value' ? '<span class="ie-error">bad value</span>'
        :                             '<span class="ie-error">error</span>';
      const curMsrp = r.current_msrp != null ? `£${parseFloat(r.current_msrp).toFixed(2)}` : '—';
      const newMsrpParsed = r._action === 'bad_value' ? r.new_msrp : parseFloat(r.new_msrp);
      const newMsrp = r.new_msrp != null
        ? (r._action === 'bad_value' ? `<span style="color:#A4262C;font-size:11px">${r.new_msrp}</span>` : `£${newMsrpParsed.toFixed(2)}`)
        : '—';
      html += `<tr>
        <td style="font-family:monospace;font-size:12px">${r.key_value||'—'}</td>
        <td>${r.model_no||'—'}</td>
        <td style="color:#605E5C">${r.manufacturer||'—'}</td>
        <td style="text-align:right;color:#605E5C">${curMsrp}</td>
        <td style="text-align:right;font-weight:600">${newMsrp}</td>
        <td>${status}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  } else {
    html += '<p style="color:#A19F9D;padding:12px">No rows could be matched.</p>';
  }

  previewEl.innerHTML = html;
}

// ── Retailer KPI ──────────────────────────────────────────────────────────────
let retailerKpiLoaded = false;
function loadRetailerKpi() {
  retailerKpiLoaded = true;
  fetch('/api/retailer/kpi').then(r=>r.json()).then(data => {
    const el = document.getElementById('retailer-kpi');
    el.innerHTML = `
      <div class="kpi-card"><div class="label">SKUs Tracked</div><div class="value">${data.total_skus}</div></div>
      <div class="kpi-card"><div class="label">Below MSRP Today</div><div class="value">${data.below_msrp_today}</div><div class="sub">prices below MSRP</div></div>
      <div class="kpi-card"><div class="label">Prices Scraped Today</div><div class="value">${data.prices_today}</div></div>
      <div class="kpi-card"><div class="label">Last Scraped</div><div class="value" style="font-size:16px">${fmtDate(data.latest_date)}</div></div>
    `;
  });
}

// ── Retailer search ───────────────────────────────────────────────────────────
function doRetSearch() {
  const q = document.getElementById('ret-search-input').value.trim();
  if (!q) return;
  showRetSection('search');
  document.getElementById('ret-search-results').innerHTML = '<div class="spinner">Searching…</div>';
  fetch('/api/retailer/search?q=' + encodeURIComponent(q)).then(r=>r.json()).then(rows => {
    if (!rows.length) { document.getElementById('ret-search-results').innerHTML = '<p style="color:#A19F9D;padding:20px">No results</p>'; return; }
    let html = '<div class="section-title">Results (' + rows.length + ')</div><div class="tbl-wrap"><table><thead><tr><th>Product</th><th>Model</th><th>Manufacturer</th><th>Lowest Price</th><th>Below MSRP</th></tr></thead><tbody>';
    rows.forEach(r => {
      html += `<tr class="clickable" onclick="loadRetSku(${r.product_id},'search')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
        <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
        <td>${r.below_msrp_count > 0 ? '<span class="badge badge-red">Yes ('+r.below_msrp_count+')</span>' : '—'}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    const _retSearchEl = document.getElementById('ret-search-results');
    _retSearchEl.innerHTML = html;
    makeSortableAll(_retSearchEl);
  });
}

// ── Retailer SKU drill-down ───────────────────────────────────────────────────
let _retSkuChart = null;
let _retSkuHistory = [];
let _retSkuLinks  = {};
let _retSkuProductId = null;

function loadRetSku(productId, backSection) {
  showRetSection('sku');
  const backBtn = document.getElementById('ret-sku-back');
  backBtn.dataset.back = backSection || 'overview';
  backBtn.onclick = () => showRetSection(backSection || 'overview');
  document.getElementById('ret-sku-content').innerHTML = '<div class="spinner">Loading…</div>';
  _retSkuProductId = productId;
  if (_retSkuChart) { _retSkuChart.destroy(); _retSkuChart = null; }
  Promise.all([
    fetch('/api/retailer/sku/'   + productId).then(r=>r.json()),
    fetch('/api/retailer/links/' + productId).then(r=>r.json()).catch(()=>({}))
  ]).then(([data, links]) => {
    _retSkuLinks   = links  || {};
    _retSkuHistory = data.price_history || [];
    renderRetSku(data);
  });
}

function renderRetSku(data) {
  const el = document.getElementById('ret-sku-content');
  const { info, snapshot } = data;

  let html = `<h3 style="margin-bottom:8px">${info.manufacturer} ${info.model_no}</h3>
    <p style="color:#605E5C;margin-bottom:16px">Product: ${info.product_id} | MSRP: ${info.msrp ? '£'+info.msrp.toFixed(2) : '—'}</p>`;

  html += `<div class="section-title">Current Snapshot</div>
    <p style="font-size:12px;color:#A19F9D;margin:-8px 0 10px">Click a retailer name to open the scraped listing. Use Purge to wipe bad price history.</p>
    <div class="tbl-wrap"><table><thead><tr><th>Retailer</th><th>Price</th><th>vs MSRP</th><th>In Stock</th><th></th></tr></thead><tbody>`;

  snapshot.forEach(r => {
    const ret       = r.retailer;
    const retEsc    = ret.replace(/'/g,"\\'");
    const belowBadge = r.below_msrp === 1
      ? '<span class="badge badge-red">Below MSRP</span>'
      : (r.price ? '<span class="badge badge-green">Above MSRP</span>' : '');
    const stockCell  = r.in_stock === 1 ? '<span style="color:#107C10">✓ In Stock</span>'
                     : r.in_stock === 0 ? '<span style="color:#A4262C">✗ OOS</span>' : '—';
    const linkStyle  = _retSkuLinks[ret]
      ? 'cursor:pointer;color:#0078D4;text-decoration:underline'
      : 'color:inherit';
    const linkClick  = _retSkuLinks[ret]
      ? `onclick="window.open('${_retSkuLinks[ret].replace(/'/g,"\\'")}','_blank')" title="Open ${ret} listing"`
      : '';
    const btnStyle   = 'background:none;border:1px solid #C8C6C4;border-radius:2px;padding:2px 7px;cursor:pointer;font-size:11px;color:#605E5C';
    const actions    = `<span style="display:flex;gap:4px">
      <button style="${btnStyle}" onclick="purgeRetailerData(${info.product_id},'${retEsc}')" title="Purge all history for ${ret}">🗑 All</button>
      <button style="${btnStyle}" onclick="openPurgeDatesModal(${info.product_id},'${retEsc}')" title="Choose specific dates to purge">📅 Dates</button>
    </span>`;
    html += `<tr>
      <td><span style="${linkStyle}" ${linkClick}>${ret}</span></td>
      <td>${r.price ? '£'+r.price.toFixed(2) : '<span style="color:#A19F9D">No data</span>'}</td>
      <td>${belowBadge}</td>
      <td>${stockCell}</td>
      <td>${actions}</td></tr>`;
  });
  html += '</tbody></table></div>';

  // Chart container + range buttons
  html += `<div class="chart-box" style="margin-bottom:20px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <h4 style="margin:0">Price Trend by Retailer</h4>
      <div style="display:flex;gap:4px">
        ${[30,90,180,365,0].map(d => {
          const label = d===0?'All':d===365?'1y':d+'d';
          const active = d===0 ? 'background:#0078D4;color:#fff;border-color:#0078D4' : '';
          return `<button class="ret-range-btn" data-days="${d}" onclick="setRetChartRange(${d},this)"
            style="font-size:11px;padding:3px 8px;border:1px solid #C8C6C4;border-radius:2px;cursor:pointer;${active}">${label}</button>`;
        }).join('')}
      </div>
    </div>
    <canvas id="ret-chart-price" style="max-height:260px"></canvas>
  </div>`;

  el.innerHTML = html;
  makeSortableAll(el);
  buildRetSkuChart(0);   // default: All history
}

function buildRetSkuChart(days) {
  const history = _retSkuHistory;
  let filtered  = history;
  if (days > 0) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    const cutoffStr = cutoff.toISOString().split('T')[0];
    filtered = history.filter(r => r.date >= cutoffStr);
  }
  const retailers = [...new Set(history.map(r => r.retailer))];
  const dates     = [...new Set(filtered.map(r => r.date))].sort();
  const palette   = ['#0078D4','#E88C1A','#8A8886','#FFB900','#107C10','#D13438','#00B7C3','#8764B8','#69797E'];
  const datasets  = retailers.map((ret, i) => ({
    label: ret,
    data: dates.map(dt => { const row = filtered.find(r => r.retailer===ret && r.date===dt); return row?.price ?? null; }),
    borderColor: palette[i % palette.length], backgroundColor: 'transparent',
    tension: 0.2, spanGaps: true, pointRadius: 2,
  }));
  const canvas = document.getElementById('ret-chart-price');
  if (!canvas) return;
  if (_retSkuChart) { _retSkuChart.destroy(); _retSkuChart = null; }
  _retSkuChart = new Chart(canvas, {
    type: 'line',
    data: { labels: dates.map(fmtDate), datasets },
    options: { responsive:true, maintainAspectRatio:true,
               plugins:{legend:{labels:{font:{size:10}}}},
               scales:{x:{ticks:{font:{size:10}}}, y:{ticks:{font:{size:10}}}} }
  });
}

function setRetChartRange(days, btn) {
  document.querySelectorAll('.ret-range-btn').forEach(b => {
    b.style.background=''; b.style.color=''; b.style.borderColor='#C8C6C4';
  });
  if (btn) { btn.style.background='#0078D4'; btn.style.color='#fff'; btn.style.borderColor='#0078D4'; }
  buildRetSkuChart(days);
}

// ── Retailer purge (all history) ──────────────────────────────────────────────
function purgeRetailerData(productId, retailer) {
  const msg = `Purge ALL historical price data for "${retailer}" on product ${productId}?\n\nEvery date's data for this retailer will be deleted. The retailer ID is kept — remap when ready.\n\nThis cannot be undone.`;
  if (!confirm(msg)) return;
  fetch('/api/retailer/purge', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ product_id: productId, retailer })
  }).then(r => r.json()).then(d => {
    if (d.error) { alert('Purge failed: ' + d.error); return; }
    alert(`Done — ${d.deleted_rows} row${d.deleted_rows !== 1 ? 's' : ''} deleted for ${retailer}.`);
    _retReportCache = { name: null, rows: [] };   // invalidate — report must re-fetch
    retailerKpiLoaded = false; loadRetailerKpi(); // refresh KPI tile counts
    const back = document.getElementById('ret-sku-back').dataset.back || 'overview';
    loadRetSku(productId, back);
  }).catch(() => alert('Purge failed — check connection.'));
}

// ── Retailer purge (selected dates) ──────────────────────────────────────────
let _pdm = { product_id: null, retailer: null };

function openPurgeDatesModal(productId, retailer) {
  _pdmMode = 'retail';
  _pdm = { product_id: productId, retailer };
  document.getElementById('pdm-title').textContent = `Purge dates — ${retailer}`;
  document.getElementById('pdm-desc').textContent  = 'Select individual dates to delete. Retailer ID is left untouched.';
  document.getElementById('pdm-dates').innerHTML   = '<div class="spinner">Loading…</div>';
  document.getElementById('purge-dates-modal').classList.add('open');
  // Use already-loaded history if available, otherwise fetch
  const rows = _retSkuHistory.filter(r => r.retailer === retailer && r.price != null);
  _pdmRenderDates(rows);
}

function _pdmRenderDates(rows) {
  if (!rows.length) {
    document.getElementById('pdm-dates').innerHTML = '<p style="color:#A19F9D;font-size:13px">No price data for this retailer.</p>';
    return;
  }
  const sorted = [...rows].sort((a,b) => b.date.localeCompare(a.date));
  document.getElementById('pdm-dates').innerHTML = sorted.map(r =>
    `<label style="display:flex;align-items:center;gap:10px;padding:5px 2px;border-bottom:1px solid #F3F2F1;cursor:pointer">
      <input type="checkbox" class="pdm-cb" value="${r.date}" style="cursor:pointer">
      <span style="min-width:70px;font-size:13px">${fmtDate(r.date)}</span>
      <span style="color:#605E5C;font-size:13px">£${r.price.toFixed(2)}</span>
    </label>`
  ).join('');
}

function pdmSelectAll(checked) {
  document.querySelectorAll('.pdm-cb').forEach(cb => cb.checked = checked);
}

function closePurgeDatesModal() {
  document.getElementById('purge-dates-modal').classList.remove('open');
}

let _pdmMode = 'retail';   // 'retail' | 'stic'
let _sticPdm = {};         // { product_id, dates[] }

function confirmPurgeDates() {
  if (_pdmMode === 'stic') { confirmSticPurgeDates(); return; }
  const dates = [...document.querySelectorAll('.pdm-cb:checked')].map(cb => cb.value);
  if (!dates.length) { alert('No dates selected.'); return; }
  if (!confirm(`Delete ${dates.length} date(s) of ${_pdm.retailer} data? This cannot be undone.`)) return;
  fetch('/api/retailer/purge-dates', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ product_id: _pdm.product_id, retailer: _pdm.retailer, dates })
  }).then(r=>r.json()).then(d => {
    if (d.error) { alert('Failed: ' + d.error); return; }
    closePurgeDatesModal();
    alert(`Done — ${d.deleted_rows} row${d.deleted_rows!==1?'s':''} deleted.`);
    _retReportCache = { name: null, rows: [] };   // invalidate — report must re-fetch
    retailerKpiLoaded = false; loadRetailerKpi(); // refresh KPI tile counts
    const back = document.getElementById('ret-sku-back').dataset.back || 'overview';
    loadRetSku(_pdm.product_id, back);
  }).catch(() => alert('Purge failed — check connection.'));
}

// ── STIC purge (selected days) ────────────────────────────────────────────────
function openSticPurgeDatesModal(productId) {
  _pdmMode = 'stic';
  _sticPdm = { product_id: productId };
  document.getElementById('pdm-title').textContent = `Purge STIC days — Product ${productId}`;
  document.getElementById('pdm-desc').textContent  = 'Select days to delete. All distributor rows for that day are removed — re-scrape afterwards to replace with clean data.';
  document.getElementById('pdm-dates').innerHTML   = '<div class="spinner">Loading…</div>';
  document.getElementById('purge-dates-modal').classList.add('open');
  fetch('/api/stic/sku/' + productId).then(r => r.json()).then(data => {
    const ph = data.price_history || [];
    const dateSet = [...new Set(ph.map(r => r.date))].sort((a,b) => b.localeCompare(a));
    // Build price summary per date (cheapest in-stock for label)
    const cheapByDate = {};
    (data.cheapest_history || []).forEach(r => { cheapByDate[r.date] = r.price; });
    if (!dateSet.length) {
      document.getElementById('pdm-dates').innerHTML = '<p style="color:#A19F9D;font-size:13px">No data found.</p>';
      return;
    }
    document.getElementById('pdm-dates').innerHTML = dateSet.map(dt => {
      const price = cheapByDate[dt] ? `£${cheapByDate[dt].toFixed(2)}` : '—';
      return `<label style="display:flex;align-items:center;gap:10px;padding:5px 2px;border-bottom:1px solid #F3F2F1;cursor:pointer">
        <input type="checkbox" class="pdm-cb" value="${dt}" style="cursor:pointer">
        <span style="min-width:70px;font-size:13px">${fmtDate(dt)}</span>
        <span style="color:#605E5C;font-size:13px">cheapest: ${price}</span>
      </label>`;
    }).join('');
  });
}

function confirmSticPurgeDates() {
  const dates = [...document.querySelectorAll('.pdm-cb:checked')].map(cb => cb.value);
  if (!dates.length) { alert('No dates selected.'); return; }
  if (!confirm(`Delete all STIC distributor data for ${dates.length} day(s) on product ${_sticPdm.product_id}?\n\nThis cannot be undone.`)) return;
  fetch('/api/stic/purge-dates', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ product_id: _sticPdm.product_id, dates })
  }).then(r => r.json()).then(d => {
    if (d.error) { alert('Failed: ' + d.error); return; }
    closePurgeDatesModal();
    loadSticSku(_sticPdm.product_id, sticSkuBackSection || 'overview');
  }).catch(() => alert('Purge failed — check connection.'));
}

// ── Retailer link click-through (for report tables) ──────────────────────────
function openRetailerLink(productId, retailer, event) {
  if (event) event.stopPropagation();
  fetch('/api/retailer/links/' + productId)
    .then(r=>r.json())
    .then(links => {
      const url = links[retailer];
      if (url) window.open(url, '_blank');
      else alert(`No URL stored for ${retailer} on product ${productId}.`);
    })
    .catch(() => alert('Could not fetch retailer link.'));
}

// ── Retailer section management ───────────────────────────────────────────────
let currentRetBtn = null;
function showRetSection(name, btn) {
  document.querySelectorAll('#main-retailer .content-section').forEach(s => s.classList.remove('active'));
  document.getElementById('ret-' + name).classList.add('active');
  document.querySelectorAll('#sidebar-retailer .sidebar-btn').forEach(b => b.classList.remove('active'));
  if (btn) { btn.classList.add('active'); currentRetBtn = btn; }
  else if (name === 'overview') document.getElementById('ret-btn-overview')?.classList.add('active');
}

// ── Retailer reports ──────────────────────────────────────────────────────────
let _retReportCache = { name: null, rows: [] };

const RET_REPORT_TITLES = {
  out_of_stock:   'Out of Stock Today',
  back_in_stock:  'Back in Stock',
  never_listed:   'Never Listed at Any Retailer',
  price_trends:   'Price Trends — Top Movers (14 Days)',
  price_dropping: 'Price Dropping',
  price_rising:   'Price Rising',
  price_gaps:     'Price Gaps Between Retailers',
  daily_changes:  'All Changes Since Yesterday',
  below_msrp:     'Below MSRP',
  above_msrp:     'All Retailers Above MSRP',
  msrp_gap:       'Furthest from MSRP',
};

const RET_REPORT_HELP = {
  out_of_stock: {
    title: 'Out of Stock Today',
    body: `<p>Shows every SKU where at least one retailer is reporting <strong>out of stock</strong> today. Each retailer gets its own column — <span style="color:#107C10">✓</span> in stock, <span style="color:#A4262C;font-weight:600">✗</span> out of stock, — no data scraped.</p>
<p><strong>Retailer filter:</strong> select a retailer to narrow the list to only SKUs that are OOS <em>at that specific retailer</em>. Manufacturer and Product Group filters apply on top.</p>
<p><strong>How to use:</strong> Spot gaps in retail availability at a glance. Cross-reference with the STIC distributor data to see whether OOS is a supply problem or a listing problem.</p>`
  },
  back_in_stock: {
    title: 'Back in Stock',
    body: `<p>Products that were <strong>out of stock yesterday but are in stock today</strong> at at least one retailer. One row per SKU/retailer transition.</p>
<p><strong>How to use:</strong> Fast-moving signal — a SKU becoming available again can indicate a new delivery, re-listing, or end of a promotion that cleared stock. Check the price column to see whether it relisted at a different price point.</p>`
  },
  never_listed: {
    title: 'Never Listed at Any Retailer',
    body: `<p>Products that have <strong>never had a price or stock record at any retailer</strong> across all dates in the database — the scraper found no data.</p>
<p><strong>How to use:</strong> These SKUs may be missing retailer IDs (ASIN, SKU codes), may not be stocked by this retail channel, or may be very new. Review the Retailer IDs in the Catalogue tab to see what's missing.</p>`
  },
  price_trends: {
    title: 'Price Trends — Top Movers (14 Days)',
    body: `<p>Products with the <strong>largest absolute price movement</strong> per retailer over the past 14 days — comparing the oldest available date in the 14-day window against today. Sorted by the size of the move.</p>
<p>Includes both risers and fallers in a single ranked list, with change £ and change % columns.</p>
<p><strong>How to use:</strong> Spot sustained promotions (large drops held over two weeks) vs overnight corrections. A large drop at one retailer while others hold price often signals a targeted promotional activity.</p>`
  },
  price_dropping: {
    title: 'Price Dropping',
    body: `<p>Products where at least one retailer's price is <strong>lower today than yesterday</strong>. Sorted by the size of the drop (largest first). Shows change £ and change % per retailer.</p>
<p><strong>How to use:</strong> Early warning of promotional activity or price competition starting. If the drop takes a product below MSRP, cross-reference with the Below MSRP report.</p>`
  },
  price_rising: {
    title: 'Price Rising',
    body: `<p>Products where at least one retailer's price is <strong>higher today than yesterday</strong>. Sorted by the size of the rise.</p>
<p><strong>How to use:</strong> May signal post-promo normalisation, tightening supply, or cost increases being passed through. Cross-reference with STIC distributor data to see if channel stock is also dropping.</p>`
  },
  price_gaps: {
    title: 'Price Gaps Between Retailers',
    body: `<p>For each SKU with prices from <strong>two or more retailers</strong>, shows the spread between the cheapest and dearest, expressed as £ and %. Sorted by the widest gap % first.</p>
<p>Also shows the cheapest retail price vs MSRP where MSRP is set — green if above MSRP, red if below.</p>
<p><strong>How to use:</strong> A wide spread indicates one retailer is discounting aggressively or another is out of step with the market. Use to spot outlier retailers on specific SKUs — the gap % tells you how anomalous the cheapest price is relative to the rest of the market.</p>`
  },
  daily_changes: {
    title: 'All Changes Since Yesterday',
    body: `<p>Every <strong>price move and in-stock status change across all retailers since yesterday</strong>. Price rises in <span style="color:#A4262C;font-weight:600">red</span>, drops in <span style="color:#107C10;font-weight:600">green</span>. Stock transitions shown as before → after.</p>
<p><strong>How to use:</strong> Full activity log for the day. Good for a morning review of what moved overnight before drilling into specific reports. Click any row to open the SKU drill-down and see the full history.</p>`
  },
  below_msrp: {
    title: 'Below MSRP',
    body: `<p>Products where <strong>at least one retailer is currently pricing below MSRP</strong>. The <em># Below MSRP</em> column counts how many retailers are under, and the <em>Retailers Below</em> column names them.</p>
<p><strong>How to use:</strong> MSRP breaches can indicate promotional clearance, aggressive discounting, or an MSRP that needs reviewing. Filter by manufacturer or product group to focus a category review.</p>`
  },
  above_msrp: {
    title: 'All Retailers Above MSRP',
    body: `<p>Products where <strong>every retailer that has a price today is at or above MSRP</strong>. The cheapest price available is shown for reference.</p>
<p><strong>How to use:</strong> Confirms which SKUs are holding their recommended price across all of retail. May indicate strong demand, tight supply, or premium positioning. Useful to flag to the brand that pricing is intact.</p>`
  },
  msrp_gap: {
    title: 'Furthest from MSRP',
    body: `<p>Individual retailer rows sorted by <strong>how far the retail price is from MSRP as a percentage</strong>. Biggest discounts (furthest below MSRP) float to the top.</p>
<p>Green badge = above MSRP, red badge = below MSRP. The gap % is (price / MSRP − 1) × 100.</p>
<p><strong>How to use:</strong> The deepest discounts relative to MSRP. These are the retailer/SKU combinations where pricing has moved furthest from recommended. Use alongside the Below MSRP report — this one ranks by severity rather than by count.</p>`
  },
};

function loadRetReport(name, btn) {
  if (currentRetBtn) currentRetBtn.classList.remove('active');
  if (btn) { btn.classList.add('active'); currentRetBtn = btn; }
  showRetSection('report');
  document.getElementById('ret-report-content').innerHTML = '<div class="spinner">Loading…</div>';
  fetch('/api/retailer/report/' + name).then(r=>r.json()).then(data => {
    renderRetReport(name, data);
  });
}

function buildRetFilterBar(rows, includeRetailer) {
  const manufacturers = [...new Set(rows.map(r=>r.manufacturer).filter(Boolean))].sort();
  const groups        = [...new Set(rows.map(r=>r.product_group).filter(Boolean))].sort();
  const retailers     = [...new Set(rows.map(r=>r.retailer).filter(Boolean))].sort();
  const mOpts = ['<option value="">All Manufacturers</option>', ...manufacturers.map(m=>`<option>${m}</option>`)].join('');
  const gOpts = ['<option value="">All Groups</option>',        ...groups.map(g=>`<option>${g}</option>`)].join('');
  const rOpts = ['<option value="">All Retailers</option>',     ...retailers.map(r=>`<option>${r}</option>`)].join('');
  const retSel = includeRetailer
    ? `<select id="ret-filter-retailer" onchange="applyRetFilters()" style="padding:6px 10px;border:1px solid #C8C6C4;border-radius:4px;font-size:13px;min-width:160px">${rOpts}</select>`
    : '';
  return `<div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
    ${retSel}
    <select id="ret-filter-mfr"   onchange="applyRetFilters()" style="padding:6px 10px;border:1px solid #C8C6C4;border-radius:4px;font-size:13px;min-width:180px">${mOpts}</select>
    <select id="ret-filter-group" onchange="applyRetFilters()" style="padding:6px 10px;border:1px solid #C8C6C4;border-radius:4px;font-size:13px;min-width:180px">${gOpts}</select>
  </div>`;
}

function applyRetFilters() {
  // OOS matrix handles retailer filter internally (it's a product-level pivot, not row-level)
  if (_retReportCache.name === 'out_of_stock') {
    renderRetReportTable('out_of_stock', _retReportCache.rows);
    return;
  }
  const ret = document.getElementById('ret-filter-retailer')?.value || '';
  const mfr = document.getElementById('ret-filter-mfr')?.value     || '';
  const grp = document.getElementById('ret-filter-group')?.value   || '';
  const filtered = _retReportCache.rows.filter(r =>
    (!ret || r.retailer      === ret) &&
    (!mfr || r.manufacturer  === mfr) &&
    (!grp || r.product_group === grp)
  );
  renderRetReportTable(_retReportCache.name, filtered);
}

function renderRetReport(name, rows) {
  _retReportCache = { name, rows };
  const title = RET_REPORT_TITLES[name] || name;
  if (!rows.length) {
    document.getElementById('ret-report-content').innerHTML =
      `<div class="section-title">${title}</div><p style="color:#A19F9D;padding:20px">No items match this report.</p>`;
    return;
  }
  renderRetReportTable(name, rows);
}

function renderRetReportTable(name, rows) {
  const title    = RET_REPORT_TITLES[name] || name;
  const savedRet = document.getElementById('ret-filter-retailer')?.value || '';
  const savedMfr = document.getElementById('ret-filter-mfr')?.value     || '';
  const savedGrp = document.getElementById('ret-filter-group')?.value   || '';

  // ── Out of Stock: pivot to SKU × Retailer matrix ──────────────────────────
  if (name === 'out_of_stock') {
    const filteredRetailer = savedRet;
    const filteredMfr      = savedMfr;
    const filteredGrp      = savedGrp;
    const retailerCols = [...new Set(_retReportCache.rows.map(r=>r.retailer).filter(Boolean))].sort();

    // Build product map, applying mfr / group filters at row level
    const byProduct = {};
    _retReportCache.rows.forEach(r => {
      if (filteredMfr && r.manufacturer  !== filteredMfr) return;
      if (filteredGrp && r.product_group !== filteredGrp) return;
      if (!byProduct[r.product_id]) byProduct[r.product_id] = {
        product_id: r.product_id, model_no: r.model_no,
        manufacturer: r.manufacturer, product_group: r.product_group, retailers: {}
      };
      byProduct[r.product_id].retailers[r.retailer] = r.in_stock;
    });

    // Apply retailer filter: show only products OOS at that retailer
    let products = Object.values(byProduct);
    if (filteredRetailer) {
      products = products.filter(p => p.retailers[filteredRetailer] === 0);
    }
    products.sort((a,b) => a.model_no.localeCompare(b.model_no));

    const filterBar = buildRetFilterBar(_retReportCache.rows, true);
    const cols = ['Product', 'Model', 'Manufacturer', ...retailerCols];
    let html = `<div class="section-title">${title} <span style="font-size:12px;font-weight:400;color:#605E5C">(${products.length} SKUs)</span>
      <button class="info-btn" onclick="showRetHelp('${name}')" title="How this report works">ⓘ</button></div>
      ${filterBar}<div class="tbl-wrap"><table><thead><tr>${cols.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>`;
    products.forEach(p => {
      const retCells = retailerCols.map(ret => {
        const s = p.retailers[ret];
        if (s === 1) return `<td style="color:#107C10;text-align:center" title="${ret}: In Stock">✓</td>`;
        if (s === 0) return `<td style="color:#A4262C;font-weight:600;text-align:center" title="${ret}: Out of Stock">✗</td>`;
        return `<td style="color:#C8C6C4;text-align:center" title="${ret}: No data">—</td>`;
      });
      html += `<tr class="clickable" onclick="loadRetSku(${p.product_id},'report')">
        <td>${p.product_id}</td><td>${p.model_no}</td><td>${p.manufacturer||'—'}</td>${retCells.join('')}</tr>`;
    });
    html += '</tbody></table></div>';
    const _retRptEl1 = document.getElementById('ret-report-content');
    _retRptEl1.innerHTML = html;
    makeSortableAll(_retRptEl1);
    if (savedRet && document.getElementById('ret-filter-retailer')) document.getElementById('ret-filter-retailer').value = savedRet;
    if (savedMfr && document.getElementById('ret-filter-mfr'))      document.getElementById('ret-filter-mfr').value     = savedMfr;
    if (savedGrp && document.getElementById('ret-filter-group'))    document.getElementById('ret-filter-group').value   = savedGrp;
    return;
  }

  // ── All other reports ─────────────────────────────────────────────────────
  const hasRetCol  = ['price_dropping','price_rising','daily_changes','back_in_stock','msrp_gap','price_trends'].includes(name);
  const hasFilters = !['price_gaps','never_listed'].includes(name);
  const filterBar  = hasFilters ? buildRetFilterBar(_retReportCache.rows, hasRetCol) : '';

  let cols, rowFn;

  // Helper: retailer cell with click-through link
  const retCell = (r) => {
    const esc = r.retailer.replace(/'/g,"\\'");
    return `<td onclick="openRetailerLink(${r.product_id},'${esc}',event)"
      style="cursor:pointer;color:#0078D4;text-decoration:underline" title="Open ${r.retailer} listing">${r.retailer}</td>`;
  };

  if (name === 'daily_changes') {
    cols = ['Product','Model','Manufacturer','Retailer','Yesterday £','Today £','Change','Stock'];
    rowFn = r => {
      const diff  = (r.price_today != null && r.price_yesterday != null) ? r.price_today - r.price_yesterday : null;
      const badge = diff === null ? '' : diff > 0
        ? `<span class="badge badge-red">+£${diff.toFixed(2)}</span>`
        : `<span class="badge badge-green">£${diff.toFixed(2)}</span>`;
      const stockPrev = r.stock_yesterday === 1 ? '✓' : r.stock_yesterday === 0 ? '✗' : '?';
      const stockNow  = r.stock_today    === 1 ? '✓' : r.stock_today    === 0 ? '✗' : '?';
      const stockCell = (r.stock_yesterday !== r.stock_today && r.stock_yesterday !== null && r.stock_today !== null)
        ? `${stockPrev}→${stockNow}` : '—';
      return `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>${retCell(r)}
        <td>${r.price_yesterday != null ? '£'+r.price_yesterday.toFixed(2) : '—'}</td>
        <td>${r.price_today     != null ? '£'+r.price_today.toFixed(2)     : '—'}</td>
        <td>${badge}</td><td style="text-align:center">${stockCell}</td></tr>`;
    };
  } else if (name === 'price_dropping' || name === 'price_rising') {
    cols = ['Product','Model','Manufacturer','Retailer','Prev £','Today £','Change £','Change %'];
    rowFn = r => {
      const diff    = (r.price_today||0) - (r.price_yesterday||0);
      const diffPct = r.price_yesterday ? ((diff / r.price_yesterday) * 100).toFixed(1) : '—';
      const badge   = diff > 0
        ? `<span class="badge badge-red">+£${diff.toFixed(2)}</span>`
        : `<span class="badge badge-green">£${diff.toFixed(2)}</span>`;
      const pBadge  = diffPct !== '—'
        ? (diff > 0 ? `<span class="badge badge-red">+${diffPct}%</span>` : `<span class="badge badge-green">${diffPct}%</span>`)
        : '—';
      return `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>${retCell(r)}
        <td>${r.price_yesterday != null ? '£'+r.price_yesterday.toFixed(2) : '—'}</td>
        <td>${r.price_today     != null ? '£'+r.price_today.toFixed(2)     : '—'}</td>
        <td>${badge}</td><td>${pBadge}</td></tr>`;
    };
  } else if (name === 'price_trends') {
    cols = ['Product','Model','Manufacturer','Retailer','14d Ago £','Today £','Change £','Change %'];
    rowFn = r => {
      const diff    = (r.price_today||0) - (r.price_14d||0);
      const diffPct = r.price_14d ? ((diff / r.price_14d) * 100).toFixed(1) : '—';
      const badge   = diff > 0
        ? `<span class="badge badge-red">+£${diff.toFixed(2)}</span>`
        : `<span class="badge badge-green">£${diff.toFixed(2)}</span>`;
      const pBadge  = diffPct !== '—'
        ? (diff > 0 ? `<span class="badge badge-red">+${diffPct}%</span>` : `<span class="badge badge-green">${diffPct}%</span>`)
        : '—';
      return `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>${retCell(r)}
        <td>${r.price_14d   != null ? '£'+r.price_14d.toFixed(2)   : '—'}</td>
        <td>${r.price_today != null ? '£'+r.price_today.toFixed(2) : '—'}</td>
        <td>${badge}</td><td>${pBadge}</td></tr>`;
    };
  } else if (name === 'back_in_stock') {
    cols = ['Product','Model','Manufacturer','Retailer','Price Today'];
    rowFn = r => `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>${retCell(r)}
      <td>${r.price != null ? '£'+r.price.toFixed(2) : '—'}</td></tr>`;
  } else if (name === 'price_gaps') {
    cols = ['Product','Model','Manufacturer','Cheapest Retailer','Cheapest £','Dearest Retailer','Dearest £','Gap £','Gap %','MSRP','vs MSRP %'];
    rowFn = r => {
      const gap    = (r.max_price != null && r.min_price != null) ? r.max_price - r.min_price : null;
      const gapPct = (gap != null && r.min_price) ? ((gap / r.min_price) * 100).toFixed(1) : '—';
      const msrpPct= (r.msrp && r.min_price) ? (((r.min_price / r.msrp) - 1) * 100).toFixed(1) : '—';
      const gapBadge  = gapPct !== '—' ? `<span class="badge ${parseFloat(gapPct)>10?'badge-red':'badge-green'}">${gapPct}%</span>` : '—';
      const msrpBadge = msrpPct !== '—'
        ? (parseFloat(msrpPct) < 0 ? `<span class="badge badge-red">${msrpPct}%</span>` : `<span class="badge badge-green">+${msrpPct}%</span>`)
        : '—';
      return `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>
        <td>${r.cheapest_retailer||'—'}</td>
        <td>${r.min_price != null ? '£'+r.min_price.toFixed(2) : '—'}</td>
        <td>${r.dearest_retailer||'—'}</td>
        <td>${r.max_price != null ? '£'+r.max_price.toFixed(2) : '—'}</td>
        <td>${gap != null ? '£'+gap.toFixed(2) : '—'}</td>
        <td>${gapBadge}</td>
        <td>${r.msrp != null ? '£'+r.msrp.toFixed(2) : '—'}</td>
        <td>${msrpBadge}</td></tr>`;
    };
  } else if (name === 'below_msrp') {
    cols = ['Product','Model','Manufacturer','MSRP','Lowest Price','# Below MSRP','Retailers Below'];
    rowFn = r => `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>
      <td>${r.msrp  != null ? '£'+r.msrp.toFixed(2)      : '—'}</td>
      <td>${r.min_price != null ? '£'+r.min_price.toFixed(2) : '—'}</td>
      <td><span class="badge badge-red">${r.below_count}</span></td>
      <td style="font-size:12px;color:#605E5C">${r.retailers_below||'—'}</td></tr>`;
  } else if (name === 'above_msrp') {
    cols = ['Product','Model','Manufacturer','MSRP','Cheapest Price','# Retailers'];
    rowFn = r => `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>
      <td>${r.msrp  != null ? '£'+r.msrp.toFixed(2)      : '—'}</td>
      <td>${r.min_price != null ? '£'+r.min_price.toFixed(2) : '—'}</td>
      <td>${r.retailer_count}</td></tr>`;
  } else if (name === 'msrp_gap') {
    cols = ['Product','Model','Manufacturer','Retailer','Price','MSRP','Gap £','Gap %'];
    rowFn = r => {
      const gap    = (r.msrp && r.price) ? r.price - r.msrp : null;
      const gapPct = (r.msrp && r.price) ? (((r.price / r.msrp) - 1) * 100).toFixed(1) : '—';
      const badge  = gapPct !== '—'
        ? (parseFloat(gapPct) < 0 ? `<span class="badge badge-red">${gapPct}%</span>` : `<span class="badge badge-green">+${gapPct}%</span>`)
        : '—';
      return `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td>${retCell(r)}
        <td>${r.price != null ? '£'+r.price.toFixed(2) : '—'}</td>
        <td>${r.msrp  != null ? '£'+r.msrp.toFixed(2)  : '—'}</td>
        <td>${gap != null ? '£'+gap.toFixed(2) : '—'}</td>
        <td>${badge}</td></tr>`;
    };
  } else if (name === 'never_listed') {
    cols = ['Product','Model','Manufacturer','Product Group'];
    rowFn = r => `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td><td>${r.product_group||'—'}</td></tr>`;
  } else {
    cols = ['Product','Model','Manufacturer'];
    rowFn = r => `<tr class="clickable" onclick="loadRetSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer||'—'}</td></tr>`;
  }

  let html = `<div class="section-title">${title} <span style="font-size:12px;font-weight:400;color:#605E5C">(${rows.length} items)</span>
    <button class="info-btn" onclick="showRetHelp('${name}')" title="How this report works">ⓘ</button></div>
    ${filterBar}<div class="tbl-wrap"><table><thead><tr>${cols.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>`;
  rows.forEach(r => { html += rowFn(r); });
  html += '</tbody></table></div>';
  const _retRptEl2 = document.getElementById('ret-report-content');
  _retRptEl2.innerHTML = html;
  makeSortableAll(_retRptEl2);

  if (savedRet && document.getElementById('ret-filter-retailer')) document.getElementById('ret-filter-retailer').value = savedRet;
  if (savedMfr && document.getElementById('ret-filter-mfr'))      document.getElementById('ret-filter-mfr').value     = savedMfr;
  if (savedGrp && document.getElementById('ret-filter-group'))    document.getElementById('ret-filter-group').value   = savedGrp;
}

function showRetHelp(name) {
  const h = RET_REPORT_HELP[name];
  if (!h) return;
  document.getElementById('modal-title').textContent = h.title;
  document.getElementById('modal-body').innerHTML = h.body;
  document.getElementById('info-modal').classList.add('open');
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  loadWatchlist();
  loadEOLState();
  loadSticOverview();
});
</script>
</body>
</html>"""


# ── API routes — STIC ─────────────────────────────────────────────────────────

@app.route("/api/stic/kpi")
def stic_kpi():
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify({"total_skus": 0, "channel_stock_today": 0, "skus_in_stock": 0,
                        "skus_no_stock": 0, "latest_date": "—", "dates_tracked": 0})

    total_skus = qry_one("SELECT COUNT(DISTINCT product_id) AS n FROM stic_prices")["n"]
    dates_tracked = qry_one("SELECT COUNT(DISTINCT date) AS n FROM stic_prices")["n"]

    r = qry_one(
        "SELECT COALESCE(SUM(qty),0) AS stock FROM stic_prices WHERE date=?", (latest,)
    )
    channel_stock = r["stock"]

    skus_in = qry_one(
        "SELECT COUNT(DISTINCT product_id) AS n FROM stic_prices WHERE date=? AND qty>0", (latest,)
    )["n"]
    skus_no = qry_one(
        """SELECT COUNT(*) AS n FROM (
             SELECT product_id FROM stic_prices WHERE date=?
             GROUP BY product_id
             HAVING SUM(COALESCE(qty,0)) = 0
           )""", (latest,)
    )["n"]

    return jsonify({
        "total_skus": total_skus,
        "channel_stock_today": int(channel_stock),
        "skus_in_stock": skus_in,
        "skus_no_stock": skus_no,
        "latest_date": latest,
        "dates_tracked": dates_tracked,
    })


@app.route("/api/stic/chipset-overview")
def stic_chipset_overview():
    group = request.args.get("group", "mbrd")
    group_filter = {
        "mbrd":   "p.product_group = 'PROD_MBRD'",
        "server": "p.product_group = 'PROD_MBRDS'",
        "gpu":    "p.product_group = 'PROD_VIDEO'",
    }.get(group, "p.product_group = 'PROD_MBRD'")

    latest = latest_date_for_group(group_filter)
    if not latest:
        return jsonify([])

    rows = qry(
        f"""SELECT sp.product_id, sp.model_no,
               COALESCE(p.chipset, sp.chipset) AS chipset,
               sp.distributor, sp.price, sp.qty
           FROM stic_prices sp
           JOIN products p ON p.product_id = sp.product_id
           WHERE sp.date=? AND {group_filter}""", (latest,)
    )

    chipset_fn = extract_gpu_chipset if group == "gpu" else extract_chipset

    chipset_data = {}
    for r in rows:
        cs = r["chipset"] or chipset_fn(r["model_no"])
        if cs not in chipset_data:
            chipset_data[cs] = {"vip_skus": set(), "floor_prices": [], "vip_prices": [], "stock": 0}
        d = chipset_data[cs]
        d["vip_skus"].add(r["product_id"])
        if r["price"]:
            d["floor_prices"].append(r["price"])
        if r["distributor"] == "VIP" and r["price"]:
            d["vip_prices"].append(r["price"])
        if r["qty"]:
            d["stock"] += r["qty"]

    result = []
    for cs, d in sorted(chipset_data.items()):
        result.append({
            "chipset": cs,
            "vip_skus": len(d["vip_skus"]),
            "floor_price": min(d["floor_prices"]) if d["floor_prices"] else None,
            "vip_price": min(d["vip_prices"]) if d["vip_prices"] else None,
            "channel_stock": d["stock"],
        })
    return jsonify(result)


@app.route("/api/stic/chipset-skus")
def stic_chipset_skus():
    group   = request.args.get("group", "mbrd")
    chipset = request.args.get("chipset", "").strip()
    if not chipset:
        return jsonify([])

    group_filter = {
        "mbrd":   "p.product_group = 'PROD_MBRD'",
        "server": "p.product_group = 'PROD_MBRDS'",
        "gpu":    "p.product_group = 'PROD_VIDEO'",
    }.get(group, "p.product_group = 'PROD_MBRD'")

    latest = latest_date_for_group(group_filter)
    if not latest:
        return jsonify([])

    rows = qry(
        f"""SELECT sp.product_id, sp.model_no, sp.manufacturer,
               COALESCE(p.chipset, sp.chipset) AS chipset,
               sp.distributor, sp.price, sp.qty
           FROM stic_prices sp
           JOIN products p ON p.product_id = sp.product_id
           WHERE sp.date=? AND {group_filter}""", (latest,)
    )

    chipset_fn = extract_gpu_chipset if group == "gpu" else extract_chipset

    products = {}
    for r in rows:
        cs = r["chipset"] or chipset_fn(r["model_no"])
        if cs != chipset:
            continue
        pid = r["product_id"]
        if pid not in products:
            products[pid] = {
                "product_id":    pid,
                "model_no":      r["model_no"],
                "manufacturer":  r["manufacturer"],
                "floor_prices":  [],
                "vip_price":     None,
                "vip_stock":     0,
                "channel_stock": 0,
            }
        p = products[pid]
        if r["price"] and r["qty"] and r["qty"] > 0:
            p["floor_prices"].append(r["price"])
        if r["distributor"] == "VIP":
            if r["price"]:
                p["vip_price"] = r["price"]
            p["vip_stock"] = r["qty"] or 0
        if r["qty"]:
            p["channel_stock"] += r["qty"]

    result = []
    for p in sorted(products.values(), key=lambda x: x["model_no"]):
        result.append({
            "product_id":    p["product_id"],
            "model_no":      p["model_no"],
            "manufacturer":  p["manufacturer"],
            "floor_price":   min(p["floor_prices"]) if p["floor_prices"] else None,
            "vip_price":     p["vip_price"],
            "vip_stock":     p["vip_stock"],
            "channel_stock": p["channel_stock"],
        })
    return jsonify(result)


@app.route("/api/stic/search")
def stic_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    like = f"%{q}%"
    # Use each product's most recent date within the last 7 days — handles days where the
    # morning scrape failed (only partial data) or a SKU was manually rescraped mid-day.
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    rows = qry(
        """SELECT s.product_id, s.model_no, s.manufacturer,
               SUM(s.qty) AS total_stock,
               MAX(CASE WHEN s.distributor='VIP' THEN COALESCE(s.qty,0) END) AS vip_stock,
               MIN(CASE WHEN s.price > 0 THEN s.price END) AS min_price,
               MAX(CASE WHEN s.distributor='VIP' THEN s.price END) AS vip_price
           FROM stic_prices s
           INNER JOIN (
               SELECT product_id, MAX(date) AS latest_for_product
               FROM stic_prices
               WHERE date >= ?
               GROUP BY product_id
           ) lp ON lp.product_id = s.product_id AND s.date = lp.latest_for_product
           WHERE (CAST(s.product_id AS TEXT) LIKE ? OR s.model_no LIKE ? OR s.manufacturer LIKE ?)
           GROUP BY s.product_id, s.model_no, s.manufacturer
           ORDER BY s.model_no
           LIMIT 100""",
        (cutoff, like, like, like)
    )
    return jsonify(rows)


@app.route("/api/stic/sku/<int:product_id>")
def stic_sku(product_id):
    latest = latest_date("stic_prices")

    info = qry_one(
        """SELECT s.product_id, s.model_no, s.manufacturer, s.product_group,
               p.stic_url, p.ean, p.description, p.notes
           FROM stic_prices s
           LEFT JOIN products p ON p.product_id = s.product_id
           WHERE s.product_id=? LIMIT 1""",
        (product_id,)
    )

    # Fallback: product exists in catalogue but has no stic_prices data yet (e.g. new probe SKU)
    if not info:
        info = qry_one(
            """SELECT product_id, model_no, manufacturer, product_group,
                      stic_url, ean, description, notes
               FROM products WHERE product_id=? LIMIT 1""",
            (product_id,)
        )

    if not info:
        return jsonify({})

    snapshot = qry(
        "SELECT distributor, price, qty FROM stic_prices WHERE date=? AND product_id=? ORDER BY distributor",
        (latest, product_id)
    )

    price_history = qry(
        "SELECT date, distributor, price FROM stic_prices WHERE product_id=? ORDER BY date, distributor",
        (product_id,)
    )

    stock_history = qry(
        "SELECT date, distributor, qty FROM stic_prices WHERE product_id=? ORDER BY date, distributor",
        (product_id,)
    )

    cheapest_history = qry(
        """SELECT date, distributor, MIN(price) AS price
           FROM stic_prices WHERE product_id=? AND price IS NOT NULL AND qty > 0
           GROUP BY date ORDER BY date""",
        (product_id,)
    )

    return jsonify({
        "info": info,
        "snapshot": snapshot,
        "price_history": price_history,
        "stock_history": stock_history,
        "cheapest_history": cheapest_history,
    })


@app.route("/api/stic/report/<name>")
def stic_report(name):
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify([])
    prev = prev_date("stic_prices", latest)

    STOCK_SUM  = "SUM(COALESCE(qty,0))"
    PRICE_MIN  = "MIN(CASE WHEN price>0 THEN price END)"
    PRICE_FLOOR = "MIN(CASE WHEN price>0 AND qty>0 THEN price END)"

    if name == "no_channel_stock":
        cutoff_date = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 5)"
        )["d"]
        rows = qry(
            f"""SELECT product_id, model_no, manufacturer,
                   {PRICE_FLOOR} AS min_price, {STOCK_SUM} AS total_stock,
                   MAX(CASE WHEN distributor='VIP' THEN price END) AS vip_price
                FROM stic_prices
                WHERE date >= ?
                GROUP BY product_id, model_no, manufacturer
                HAVING MAX(COALESCE(qty,0)) = 0
                ORDER BY model_no LIMIT 200""",
            (cutoff_date,)
        )

    elif name == "back_in_stock":
        if not prev:
            return jsonify([])
        rows = qry(
            f"""SELECT t.product_id, t.model_no, t.manufacturer,
                   SUM(t.qty) AS total_stock,
                   MIN(CASE WHEN t.price>0 AND t.qty>0 THEN t.price END) AS min_price,
                   MAX(CASE WHEN t.distributor='VIP' THEN t.price END) AS vip_price
                FROM stic_prices t
                WHERE t.date = ?
                GROUP BY t.product_id, t.model_no, t.manufacturer
                HAVING SUM(t.qty) > 0
                  AND t.product_id IN (
                    SELECT product_id FROM stic_prices WHERE date=? GROUP BY product_id HAVING {STOCK_SUM}=0
                  )
                ORDER BY total_stock DESC LIMIT 100""",
            (latest, prev)
        )

    elif name == "single_distributor":
        rows = qry(
            f"""SELECT product_id, model_no, manufacturer,
                   {PRICE_FLOOR} AS min_price, {STOCK_SUM} AS total_stock,
                   MAX(CASE WHEN distributor='VIP' THEN price END) AS vip_price
                FROM stic_prices WHERE date=?
                GROUP BY product_id, model_no, manufacturer
                HAVING COUNT(CASE WHEN qty>0 THEN 1 END) = 1
                ORDER BY total_stock DESC LIMIT 200""",
            (latest,)
        )

    elif name == "new_stock_arrival":
        cutoff_date = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 6)"
        )["d"]
        if not prev:
            return jsonify([])
        rows = qry(
            f"""SELECT t.product_id, t.model_no, t.manufacturer,
                   SUM(t.qty) AS total_stock,
                   MIN(CASE WHEN t.price>0 AND t.qty>0 THEN t.price END) AS min_price,
                   MAX(CASE WHEN t.distributor='VIP' THEN t.price END) AS vip_price
                FROM stic_prices t
                WHERE t.date = ?
                GROUP BY t.product_id, t.model_no, t.manufacturer
                HAVING SUM(t.qty) > 0
                  AND t.product_id IN (
                    SELECT product_id FROM stic_prices WHERE date >= ? AND date < ?
                    GROUP BY product_id HAVING MAX(COALESCE(qty,0))=0
                  )
                ORDER BY total_stock DESC LIMIT 100""",
            (latest, cutoff_date, latest)
        )

    elif name == "vip_out_on_price":
        rows = qry(
            f"""SELECT a.product_id, a.model_no, a.manufacturer, a.product_group,
                   {STOCK_SUM.replace('qty', 'a.qty')} AS total_stock,
                   MAX(CASE WHEN a.distributor='VIP' THEN COALESCE(a.qty,0) END) AS vip_stock,
                   MIN(CASE WHEN a.price>0 AND a.qty>0 THEN a.price END) AS min_price,
                   MAX(CASE WHEN a.distributor='VIP' THEN a.price END) AS vip_price
                FROM stic_prices a
                WHERE a.date=?
                GROUP BY a.product_id, a.model_no, a.manufacturer, a.product_group
                HAVING MAX(CASE WHEN a.distributor='VIP' AND a.qty>0 THEN 1 ELSE 0 END) = 1
                   AND MAX(CASE WHEN a.distributor='VIP' THEN a.price END) IS NOT NULL
                   AND MAX(CASE WHEN a.distributor='VIP' THEN a.price END)
                       > MIN(CASE WHEN a.price>0 AND a.qty>0 THEN a.price END)
                ORDER BY MAX(CASE WHEN a.distributor='VIP' THEN COALESCE(a.qty,0) END) DESC
                LIMIT 200""",
            (latest,)
        )

    elif name == "vip_static":
        cutoff = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 7)"
        )["d"]
        rows = qry(
            f"""SELECT a.product_id, a.model_no, a.manufacturer,
                   {STOCK_SUM.replace('qty', 'a.qty')} AS total_stock,
                   MIN(CASE WHEN a.price>0 AND a.qty>0 THEN a.price END) AS min_price,
                   MAX(CASE WHEN a.distributor='VIP' THEN a.price END) AS vip_price
                FROM stic_prices a
                WHERE a.date=?
                GROUP BY a.product_id, a.model_no, a.manufacturer
                HAVING MAX(CASE WHEN a.distributor='VIP' THEN COALESCE(a.qty,0) END) > 0
                   AND a.product_id IN (
                     SELECT product_id FROM stic_prices
                     WHERE distributor='VIP' AND date >= ?
                     GROUP BY product_id
                     HAVING MAX(COALESCE(qty,0)) = MIN(COALESCE(qty,0))
                        AND COUNT(DISTINCT date) >= 5
                   )
                ORDER BY MAX(CASE WHEN a.distributor='VIP' THEN COALESCE(a.qty,0) END) DESC
                LIMIT 100""",
            (latest, cutoff)
        )

    elif name == "vip_exclusive":
        rows = qry(
            f"""SELECT product_id, model_no, manufacturer,
                   {PRICE_FLOOR} AS min_price,
                   SUM(CASE WHEN distributor='VIP' THEN COALESCE(qty,0) ELSE 0 END) AS total_stock,
                   MAX(CASE WHEN distributor='VIP' THEN price END) AS vip_price
                FROM stic_prices WHERE date=?
                GROUP BY product_id, model_no, manufacturer
                HAVING COUNT(CASE WHEN qty>0 THEN 1 END) = 1
                   AND SUM(CASE WHEN distributor='VIP' AND qty>0 THEN 1 ELSE 0 END) > 0
                ORDER BY total_stock DESC LIMIT 100""",
            (latest,)
        )

    elif name == "vip_price_gap":
        rows = qry(
            f"""SELECT v.product_id, v.model_no, v.manufacturer,
                   v.price AS vip_price, f.floor_price AS floor_price,
                   (v.price - f.floor_price) AS gap
                FROM stic_prices v
                JOIN (SELECT product_id,
                             MIN(CASE WHEN price>0 AND qty>0 THEN price END) AS floor_price
                      FROM stic_prices WHERE date=? GROUP BY product_id) f
                  ON f.product_id = v.product_id
                WHERE v.date=? AND v.distributor='VIP' AND v.qty>0
                  AND v.price IS NOT NULL AND f.floor_price IS NOT NULL
                  AND v.price > f.floor_price
                ORDER BY gap DESC LIMIT 100""",
            (latest, latest)
        )

    elif name == "never_stocked":
        rows = qry(
            f"""SELECT product_id, model_no, manufacturer,
                   0 AS total_stock, NULL AS min_price, NULL AS vip_price
                FROM stic_prices
                GROUP BY product_id, model_no, manufacturer
                HAVING MAX(COALESCE(qty,0)) = 0 AND MAX(price) IS NULL
                ORDER BY model_no LIMIT 200"""
        )

    elif name == "price_dropping":
        if not prev:
            return jsonify([])
        rows = qry(
            f"""SELECT t.product_id, t.model_no, t.manufacturer,
                   MIN(CASE WHEN t.price>0 THEN t.price END) AS price_today,
                   MIN(CASE WHEN y.price>0 THEN y.price END) AS price_yesterday
                FROM stic_prices t
                JOIN stic_prices y ON y.product_id=t.product_id AND y.date=?
                WHERE t.date=?
                GROUP BY t.product_id, t.model_no, t.manufacturer
                HAVING price_today < price_yesterday
                ORDER BY (price_today - price_yesterday) ASC LIMIT 100""",
            (prev, latest)
        )

    elif name == "price_rising":
        cutoff = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 7)"
        )["d"]
        rows = qry(
            f"""SELECT t.product_id, t.model_no, t.manufacturer,
                   MIN(CASE WHEN t.price>0 THEN t.price END) AS price_today,
                   MIN(CASE WHEN y.price>0 THEN y.price END) AS price_yesterday
                FROM stic_prices t
                JOIN stic_prices y ON y.product_id=t.product_id AND y.date=?
                WHERE t.date=?
                  AND t.product_id IN (
                    SELECT product_id FROM stic_prices WHERE date >= ?
                    GROUP BY product_id
                    HAVING MIN(CASE WHEN price>0 THEN price END) > 0
                      AND MIN(CASE WHEN price>0 THEN price END) <= MAX(CASE WHEN price>0 THEN price END) * 0.95
                  )
                GROUP BY t.product_id, t.model_no, t.manufacturer
                HAVING price_today > price_yesterday
                ORDER BY (price_today - price_yesterday) DESC LIMIT 100""",
            (prev, latest, cutoff)
        )

    elif name == "daily_changes":
        if not prev:
            return jsonify([])
        rows = qry(
            """SELECT t.product_id, t.model_no, t.distributor,
                   y.price AS price_yesterday, t.price AS price_today,
                   y.qty AS qty_yesterday, t.qty AS qty_today
                FROM stic_prices t
                JOIN stic_prices y ON y.product_id=t.product_id AND y.distributor=t.distributor AND y.date=?
                WHERE t.date=?
                  AND (t.price != y.price OR COALESCE(t.qty,0) != COALESCE(y.qty,0))
                ORDER BY t.model_no, t.distributor LIMIT 500""",
            (prev, latest)
        )

    else:
        return jsonify([])

    return jsonify([dict(r) for r in rows])


# ── API routes — Retailer ─────────────────────────────────────────────────────

@app.route("/api/retailer/kpi")
def retailer_kpi():
    latest = latest_date("retailer_prices")
    if not latest:
        return jsonify({"total_skus": 0, "below_msrp_today": 0, "prices_today": 0, "latest_date": "—"})

    total = qry_one("SELECT COUNT(DISTINCT product_id) AS n FROM retailer_prices")["n"]
    below = qry_one(
        "SELECT COUNT(*) AS n FROM retailer_prices WHERE date=? AND below_msrp=1", (latest,)
    )["n"]
    prices = qry_one(
        "SELECT COUNT(*) AS n FROM retailer_prices WHERE date=? AND price IS NOT NULL", (latest,)
    )["n"]
    return jsonify({"total_skus": total, "below_msrp_today": below, "prices_today": prices, "latest_date": latest})


@app.route("/api/retailer/search")
def retailer_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    latest = latest_date("retailer_prices")
    if not latest:
        return jsonify([])

    like = f"%{q}%"
    rows = qry(
        """SELECT DISTINCT r.product_id, r.model_no, r.manufacturer,
               MIN(CASE WHEN r.price>0 THEN r.price END) AS min_price,
               SUM(r.below_msrp) AS below_msrp_count
           FROM retailer_prices r
           WHERE r.date=?
             AND (CAST(r.product_id AS TEXT) LIKE ? OR r.model_no LIKE ? OR r.description LIKE ?)
           GROUP BY r.product_id, r.model_no, r.manufacturer
           ORDER BY r.model_no LIMIT 100""",
        (latest, like, like, like)
    )
    return jsonify(rows)


@app.route("/api/retailer/sku/<int:product_id>")
def retailer_sku(product_id):
    latest = latest_date("retailer_prices")
    if not latest:
        return jsonify({})

    info = qry_one(
        "SELECT product_id, model_no, manufacturer, description, msrp FROM retailer_prices WHERE product_id=? LIMIT 1",
        (product_id,)
    ) or {}

    snapshot = qry(
        "SELECT retailer, price, below_msrp FROM retailer_prices WHERE date=? AND product_id=? ORDER BY retailer",
        (latest, product_id)
    )

    price_history = qry(
        "SELECT date, retailer, price, below_msrp FROM retailer_prices WHERE product_id=? ORDER BY date, retailer",
        (product_id,)
    )

    return jsonify({"info": info, "snapshot": snapshot, "price_history": price_history})


@app.route("/api/retailer/links/<int:product_id>")
def retailer_links(product_id):
    """Return a {retailer: url} map built from retailer_ids for this product."""
    row = qry_one("SELECT * FROM retailer_ids WHERE product_id = ?", (product_id,)) or {}
    links = {}
    if row.get("amazon_asin"):
        links["Amazon"]      = f"https://www.amazon.co.uk/dp/{row['amazon_asin']}"
    if row.get("currys_sku"):
        links["Currys"]      = f"https://www.currys.co.uk/search?q={row['currys_sku']}"
    if row.get("very_url"):
        links["Very"]        = row["very_url"]
    if row.get("argos_sku"):
        argos_id = str(row["argos_sku"]).replace(" ", "%20")
        links["Argos"]       = f"https://www.argos.co.uk/product/{argos_id}/"
    if row.get("ccl_url"):
        links["CCL Online"]  = row["ccl_url"]
    if row.get("awdit_url"):
        links["AWD-IT"]      = row["awdit_url"]
    if row.get("scan_url"):
        links["Scan"]        = row["scan_url"]
    if row.get("ocuk_code"):
        links["Overclockers"] = f"https://www.overclockers.co.uk/?query={row['ocuk_code']}"
    if row.get("box_url"):
        links["Box"]         = row["box_url"]
    return jsonify(links)


@app.route("/api/stic/purge-dates", methods=["POST"])
def stic_purge_dates():
    """Delete all stic_prices rows for a product on the given dates (all distributors)."""
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    dates      = data.get("dates", [])
    if not product_id or not dates:
        return jsonify({"error": "product_id and dates are required"}), 400
    if not isinstance(dates, list) or len(dates) > 366:
        return jsonify({"error": "dates must be a list of up to 366 date strings"}), 400
    placeholders = ",".join("?" * len(dates))
    db = get_db()
    cur = db.execute(
        f"DELETE FROM stic_prices WHERE product_id = ? AND date IN ({placeholders})",
        [product_id] + list(dates)
    )
    deleted = cur.rowcount
    db.commit()
    db.close()
    return jsonify({"deleted_rows": deleted, "product_id": product_id})


@app.route("/api/retailer/purge-dates", methods=["POST"])
def retailer_purge_dates():
    """Delete retailer_prices rows for a specific set of dates."""
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    retailer   = data.get("retailer", "").strip()
    dates      = data.get("dates", [])
    if not product_id or not retailer or not dates:
        return jsonify({"error": "product_id, retailer and dates are required"}), 400
    if not isinstance(dates, list) or len(dates) > 366:
        return jsonify({"error": "dates must be a list of up to 366 date strings"}), 400

    placeholders = ",".join("?" * len(dates))
    db = get_db()
    cur = db.execute(
        f"DELETE FROM retailer_prices WHERE product_id = ? AND retailer = ? AND date IN ({placeholders})",
        [product_id, retailer] + list(dates)
    )
    deleted = cur.rowcount
    db.commit()
    db.close()
    return jsonify({"deleted_rows": deleted, "product_id": product_id, "retailer": retailer})


@app.route("/api/retailer/purge", methods=["POST"])
def retailer_purge():
    """Delete all retailer_prices rows for a given product/retailer pair.
    Retailer IDs are left untouched — caller can remap when ready."""
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    retailer   = data.get("retailer", "").strip()
    if not product_id or not retailer:
        return jsonify({"error": "product_id and retailer are required"}), 400

    db = get_db()
    cur = db.execute(
        "DELETE FROM retailer_prices WHERE product_id = ? AND retailer = ?",
        (product_id, retailer)
    )
    deleted = cur.rowcount
    db.commit()
    db.close()
    return jsonify({"deleted_rows": deleted, "product_id": product_id, "retailer": retailer})


@app.route("/api/retailer/report/<name>")
def retailer_report(name):
    latest = latest_date("retailer_prices")
    if not latest:
        return jsonify([])
    prev = prev_date("retailer_prices", latest)

    if name == "out_of_stock":
        # Return all retailer rows for today; frontend pivots into matrix
        rows = qry(
            """SELECT r.product_id, r.model_no, r.manufacturer, r.product_group,
                      r.retailer, r.price, r.in_stock
               FROM retailer_prices r
               WHERE r.date = ?
               ORDER BY r.model_no, r.retailer""",
            (latest,)
        )
        # Keep only products that have at least one OOS entry
        oos_ids = {r["product_id"] for r in rows if r["in_stock"] == 0}
        rows = [r for r in rows if r["product_id"] in oos_ids]

    elif name == "back_in_stock":
        if not prev:
            return jsonify([])
        rows = qry(
            """SELECT t.product_id, t.model_no, t.manufacturer, t.product_group,
                      t.retailer, t.price
               FROM retailer_prices t
               JOIN retailer_prices y
                 ON y.product_id = t.product_id AND y.retailer = t.retailer AND y.date = ?
               WHERE t.date = ? AND t.in_stock = 1 AND y.in_stock = 0
               ORDER BY t.model_no, t.retailer LIMIT 300""",
            (prev, latest)
        )

    elif name == "never_listed":
        rows = qry(
            """SELECT product_id, model_no, manufacturer, product_group
               FROM retailer_prices
               GROUP BY product_id, model_no, manufacturer, product_group
               HAVING MAX(price) IS NULL
               ORDER BY model_no LIMIT 300"""
        )

    elif name == "price_dropping":
        if not prev:
            return jsonify([])
        rows = qry(
            """SELECT t.product_id, t.model_no, t.manufacturer, t.product_group,
                      t.retailer, y.price AS price_yesterday, t.price AS price_today
               FROM retailer_prices t
               JOIN retailer_prices y
                 ON y.product_id = t.product_id AND y.retailer = t.retailer AND y.date = ?
               WHERE t.date = ?
                 AND t.price IS NOT NULL AND y.price IS NOT NULL AND t.price < y.price
               ORDER BY (y.price - t.price) DESC LIMIT 300""",
            (prev, latest)
        )

    elif name == "price_rising":
        if not prev:
            return jsonify([])
        rows = qry(
            """SELECT t.product_id, t.model_no, t.manufacturer, t.product_group,
                      t.retailer, y.price AS price_yesterday, t.price AS price_today
               FROM retailer_prices t
               JOIN retailer_prices y
                 ON y.product_id = t.product_id AND y.retailer = t.retailer AND y.date = ?
               WHERE t.date = ?
                 AND t.price IS NOT NULL AND y.price IS NOT NULL AND t.price > y.price
               ORDER BY (t.price - y.price) DESC LIMIT 300""",
            (prev, latest)
        )

    elif name == "price_trends":
        # Compare oldest date in 14-day window against today
        cutoff = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM retailer_prices ORDER BY date DESC LIMIT 14)"
        )
        cutoff = cutoff["d"] if cutoff else None
        if not cutoff or cutoff == latest:
            return jsonify([])
        rows = qry(
            """SELECT t.product_id, t.model_no, t.manufacturer, t.product_group,
                      t.retailer, o.price AS price_14d, t.price AS price_today
               FROM retailer_prices t
               JOIN retailer_prices o
                 ON o.product_id = t.product_id AND o.retailer = t.retailer AND o.date = ?
               WHERE t.date = ?
                 AND t.price IS NOT NULL AND o.price IS NOT NULL AND t.price != o.price
               ORDER BY ABS(t.price - o.price) / o.price DESC LIMIT 300""",
            (cutoff, latest)
        )

    elif name == "price_gaps":
        # Pull all prices for today; compute min/max per product in Python for clean retailer names
        # Floor at £50 to exclude obvious scraping artefacts (placeholder/accessory prices)
        all_prices = qry(
            """SELECT product_id, model_no, manufacturer, product_group, msrp, retailer, price
               FROM retailer_prices
               WHERE date = ? AND price >= 50
               ORDER BY product_id, price""",
            (latest,)
        )
        from collections import defaultdict
        by_product = defaultdict(list)
        for r in all_prices:
            by_product[r["product_id"]].append(r)

        rows = []
        for pid, price_rows in by_product.items():
            if len(price_rows) < 2:
                continue
            cheapest = price_rows[0]   # already sorted ASC by price
            dearest  = price_rows[-1]
            if dearest["price"] <= cheapest["price"]:
                continue
            rows.append({
                "product_id":        pid,
                "model_no":          cheapest["model_no"],
                "manufacturer":      cheapest["manufacturer"],
                "product_group":     cheapest["product_group"],
                "msrp":              cheapest["msrp"],
                "min_price":         cheapest["price"],
                "max_price":         dearest["price"],
                "cheapest_retailer": cheapest["retailer"],
                "dearest_retailer":  dearest["retailer"],
            })
        rows.sort(
            key=lambda r: (r["max_price"] - r["min_price"]) / r["min_price"] if r["min_price"] else 0,
            reverse=True
        )
        rows = rows[:300]

    elif name == "daily_changes":
        if not prev:
            return jsonify([])
        rows = qry(
            """SELECT t.product_id, t.model_no, t.manufacturer, t.product_group,
                      t.retailer,
                      y.price    AS price_yesterday, t.price    AS price_today,
                      y.in_stock AS stock_yesterday, t.in_stock AS stock_today
               FROM retailer_prices t
               JOIN retailer_prices y
                 ON y.product_id = t.product_id AND y.retailer = t.retailer AND y.date = ?
               WHERE t.date = ?
                 AND (
                   COALESCE(t.price,    -999) != COALESCE(y.price,    -999)
                   OR COALESCE(t.in_stock, -1) != COALESCE(y.in_stock, -1)
                 )
               ORDER BY t.model_no, t.retailer LIMIT 500""",
            (prev, latest)
        )

    elif name == "below_msrp":
        rows = qry(
            """SELECT r.product_id, r.model_no, r.manufacturer, r.product_group, r.msrp,
                      MIN(CASE WHEN r.price > 0 THEN r.price END) AS min_price,
                      SUM(CASE WHEN r.below_msrp = 1 THEN 1 ELSE 0 END) AS below_count,
                      GROUP_CONCAT(CASE WHEN r.below_msrp = 1 THEN r.retailer END) AS retailers_below
               FROM retailer_prices r
               WHERE r.date = ?
               GROUP BY r.product_id, r.model_no, r.manufacturer, r.product_group, r.msrp
               HAVING SUM(CASE WHEN r.below_msrp = 1 THEN 1 ELSE 0 END) > 0
               ORDER BY below_count DESC, r.model_no LIMIT 300""",
            (latest,)
        )

    elif name == "above_msrp":
        rows = qry(
            """SELECT r.product_id, r.model_no, r.manufacturer, r.product_group, r.msrp,
                      MIN(CASE WHEN r.price > 0 THEN r.price END) AS min_price,
                      COUNT(CASE WHEN r.price > 0 THEN 1 END) AS retailer_count
               FROM retailer_prices r
               WHERE r.date = ? AND r.msrp IS NOT NULL
               GROUP BY r.product_id, r.model_no, r.manufacturer, r.product_group, r.msrp
               HAVING COUNT(CASE WHEN r.price > 0 THEN 1 END) > 0
                  AND SUM(CASE WHEN r.below_msrp = 1 THEN 1 ELSE 0 END) = 0
               ORDER BY r.model_no LIMIT 300""",
            (latest,)
        )

    elif name == "msrp_gap":
        rows = qry(
            """SELECT r.product_id, r.model_no, r.manufacturer, r.product_group,
                      r.retailer, r.price, r.msrp
               FROM retailer_prices r
               WHERE r.date = ? AND r.price IS NOT NULL AND r.msrp IS NOT NULL AND r.msrp > 0
               ORDER BY ABS(r.price - r.msrp) / r.msrp DESC LIMIT 300""",
            (latest,)
        )

    else:
        return jsonify([])

    return jsonify(rows)


# ── Main page ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


# ═══════════════════════════════════════════════════════════════════════════════
# WATCHLIST
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/watchlist", methods=["GET"])
def watchlist_get():
    rows = qry("SELECT product_id FROM watchlist ORDER BY added_date DESC")
    return jsonify({"ids": [r["product_id"] for r in rows]})


@app.route("/api/watchlist/<int:product_id>", methods=["POST"])
def watchlist_add(product_id):
    from datetime import date as _date
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO watchlist(product_id, added_date) VALUES(?,?)",
        (product_id, _date.today().isoformat())
    )
    db.commit()
    db.close()
    return jsonify({"watched": True})


@app.route("/api/watchlist/<int:product_id>", methods=["DELETE"])
def watchlist_remove(product_id):
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE product_id=?", (product_id,))
    db.commit()
    db.close()
    return jsonify({"watched": False})


@app.route("/api/watchlist/report")
def watchlist_report():
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify([])

    watched = qry("SELECT product_id FROM watchlist ORDER BY added_date DESC")
    if not watched:
        return jsonify([])

    pids     = [r["product_id"] for r in watched]
    prev     = prev_date("stic_prices", latest)
    ph       = ",".join("?" * len(pids))

    today_rows = qry(
        f"SELECT product_id, model_no, manufacturer, distributor, qty "
        f"FROM stic_prices WHERE date=? AND product_id IN ({ph})",
        (latest, *pids)
    )
    yest_rows = qry(
        f"SELECT product_id, distributor, qty "
        f"FROM stic_prices WHERE date=? AND product_id IN ({ph})",
        (prev, *pids)
    ) if prev else []

    # Build per-SKU dict
    skus = {}
    for pid in pids:
        skus[pid] = {"product_id": pid, "model_no": "", "manufacturer": "",
                     "today": {}, "yesterday": {}}

    for r in today_rows:
        pid = r["product_id"]
        skus[pid]["model_no"]     = r["model_no"]
        skus[pid]["manufacturer"] = r["manufacturer"]
        skus[pid]["today"][r["distributor"]] = r["qty"] or 0

    for r in yest_rows:
        pid = r["product_id"]
        if pid in skus:
            skus[pid]["yesterday"][r["distributor"]] = r["qty"] or 0

    # All distributors present across all rows (ordered)
    dist_order = ["M2M Direct", "TD Synnex", "Target", "VIP", "Westcoast"]
    all_dists  = dist_order + [d for d in
                     sorted({r["distributor"] for r in today_rows})
                     if d not in dist_order]

    result = []
    for pid in pids:
        s = skus[pid]
        if not s["model_no"]:   # not in DB for latest date — skip
            continue
        total_today = sum(s["today"].values())
        total_yest  = sum(s["yesterday"].values())
        dist_data   = {d: {"today": s["today"].get(d, 0),
                           "yesterday": s["yesterday"].get(d, 0)}
                       for d in all_dists if d in s["today"] or d in s["yesterday"]}
        result.append({
            "product_id":    pid,
            "model_no":      s["model_no"],
            "manufacturer":  s["manufacturer"],
            "distributors":  dist_data,
            "total_today":   total_today,
            "total_yest":    total_yest,
            "delta":         total_today - total_yest,
        })

    return jsonify({"rows": result, "distributors": all_dists,
                    "date": latest, "prev_date": prev})


# ═══════════════════════════════════════════════════════════════════════════════
# INVESTIGATE + EOL
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/probe/list")
def probe_list():
    """Return all active probe SKUs (product_id >= 990000) with latest STIC snapshot + summary metrics."""
    import datetime as _dt
    db = get_db()
    products = db.execute(
        "SELECT product_id, model_no, manufacturer, description, stic_url, eol "
        "FROM products WHERE product_id >= 990000 ORDER BY product_id"
    ).fetchall()
    latest = db.execute(
        "SELECT MAX(date) AS d FROM stic_prices"
    ).fetchone()["d"]
    cutoff_7d = ((_dt.date.today() - _dt.timedelta(days=7)).isoformat())
    result = []
    for p in products:
        pid = p["product_id"]
        snapshot = []
        if latest:
            snapshot = db.execute(
                "SELECT distributor, price, qty FROM stic_prices "
                "WHERE product_id=? AND date=? ORDER BY distributor",
                (pid, latest)
            ).fetchall()

        # Channel stock = sum of all distributor qty from latest snapshot
        channel_stock = sum(r["qty"] or 0 for r in snapshot)

        # Floor price = cheapest in-stock price from latest snapshot
        in_stock = [r for r in snapshot if (r["qty"] or 0) > 0 and r["price"]]
        floor_price = min(r["price"] for r in in_stock) if in_stock else None

        # Sold 7d = sum of stock drops per distributor over last 7 days
        hist = db.execute(
            "SELECT date, distributor, qty FROM stic_prices "
            "WHERE product_id=? ORDER BY distributor, date",
            (pid,)
        ).fetchall()
        from collections import defaultdict
        dist_rows = defaultdict(list)
        for r in hist:
            dist_rows[r["distributor"]].append((r["date"], r["qty"] or 0))
        sold_7d = 0
        for rows in dist_rows.values():
            rows.sort()
            for i in range(1, len(rows)):
                date_str, qty = rows[i]
                prev_qty = rows[i - 1][1]
                if date_str >= cutoff_7d and qty < prev_qty:
                    sold_7d += prev_qty - qty

        result.append({
            "product_id":    pid,
            "model_no":      p["model_no"],
            "manufacturer":  p["manufacturer"],
            "description":   p["description"],
            "stic_url":      p["stic_url"],
            "eol":           p["eol"],
            "snapshot":      [dict(r) for r in snapshot],
            "latest_date":   latest,
            "channel_stock": channel_stock,
            "floor_price":   floor_price,
            "sold_7d":       sold_7d,
        })
    db.close()
    return jsonify(result)


@app.route("/api/probe/add", methods=["POST"])
def probe_add():
    """Add a new probe SKU. Auto-allocates product_id >= 990000."""
    data = request.get_json(silent=True) or {}
    model_no    = (data.get("model_no") or "").strip()
    stic_url    = (data.get("stic_url") or "").strip()
    manufacturer = (data.get("manufacturer") or "").strip() or None
    description  = (data.get("description") or "").strip() or None
    if not model_no:
        return jsonify({"error": "model_no is required"}), 400
    if not stic_url or "/Product/" not in stic_url:
        return jsonify({"error": "A valid STIC /Product/ URL is required"}), 400
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(MAX(product_id), 989999) + 1 AS next_id "
        "FROM products WHERE product_id >= 990000"
    ).fetchone()
    next_id = row["next_id"]
    db.execute(
        "INSERT INTO products (product_id, model_no, manufacturer, product_group, "
        "description, eol, stic_url) VALUES (?,?,?,?,?,0,?)",
        (next_id, model_no, manufacturer, "PROBE", description, stic_url)
    )
    db.commit()
    db.close()
    return jsonify({"added": True, "product_id": next_id, "model_no": model_no})


@app.route("/api/investigate")
def investigate():
    # Last 3 dates with stic data
    recent = [r["date"] for r in qry(
        "SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 3"
    )]

    # No STIC page: scraped on all 3 recent dates, every row has NULL price and NULL qty
    no_stic = []
    if len(recent) >= 3:
        ph = ",".join("?" * len(recent))
        no_stic = qry(f"""
            SELECT product_id, MAX(model_no) AS model_no, MAX(manufacturer) AS manufacturer
            FROM stic_prices
            WHERE date IN ({ph})
            GROUP BY product_id
            HAVING COUNT(DISTINCT date) >= 3
            AND SUM(CASE WHEN price IS NOT NULL OR qty IS NOT NULL THEN 1 ELSE 0 END) = 0
        """, recent)

    # Missing EAN: active (non-EOL) products in template with no EAN
    products = read_template_products()
    missing_ean = [
        {"product_id": p["product_id"], "model_no": p["model_no"],
         "manufacturer": p["manufacturer"]}
        for p in products if not p["eol"] and not p.get("ean")
    ]

    # Data bleeds: same-day pairs with 3+ distributors sharing identical price+qty
    latest = recent[0] if recent else None
    bleeds = []
    if latest:
        bleeds = qry("""
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
        """, (latest,))

    return jsonify({
        "no_stic_page": no_stic,
        "missing_ean":  missing_ean,
        "data_bleeds":  bleeds,
        "latest_date":  latest,
    })


@app.route("/api/eol", methods=["GET"])
def eol_get():
    products = read_template_products()
    eol_list = [p for p in products if p["eol"]]
    return jsonify({"products": eol_list})


@app.route("/api/eol/<int:product_id>", methods=["POST", "DELETE"])
def eol_set(product_id):
    mark = (request.method == "POST")
    updated = write_eol_to_template(product_id, mark)
    return jsonify({"eol": mark, "product_id": product_id, "updated": updated})


@app.route("/api/scrape/groups")
def scrape_groups():
    """Return the list of scrape groups with active SKU counts and last-scraped dates."""
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
    ]
    db = get_db()
    result = []
    for manufacturer, product_group, label in SCRAPE_GROUPS:
        # Count active SKUs for this group
        if manufacturer:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM products WHERE eol=0 AND manufacturer=? AND product_group=?",
                (manufacturer, product_group)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM products WHERE eol=0 AND product_group=?",
                (product_group,)
            ).fetchone()
        sku_count = row["c"] if row else 0

        # Last date this group had any data in stic_prices
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

        # Count how many distinct SKUs actually got data on the last_scraped date
        scraped_count = 0
        if last_scraped:
            if manufacturer:
                sc = db.execute(
                    "SELECT COUNT(DISTINCT product_id) AS c FROM stic_prices "
                    "WHERE date=? AND manufacturer=? AND product_group=?",
                    (last_scraped, manufacturer, product_group)
                ).fetchone()
            else:
                sc = db.execute(
                    "SELECT COUNT(DISTINCT product_id) AS c FROM stic_prices "
                    "WHERE date=? AND product_group=?",
                    (last_scraped, product_group)
                ).fetchone()
            scraped_count = sc["c"] if sc else 0

        result.append({
            "label":         label,
            "manufacturer":  manufacturer,
            "product_group": product_group,
            "sku_count":     sku_count,
            "last_scraped":  last_scraped,
            "scraped_count": scraped_count,
        })
    db.close()
    return jsonify(result)


@app.route("/api/scrape/missing")
def scrape_missing():
    """Return all active SKUs that have no data on their group's last-scraped date."""
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
    ]
    db = get_db()
    result = []
    for manufacturer, product_group, label in SCRAPE_GROUPS:
        # Find last scrape date for this group
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

        # Find active SKUs in this group that have NO data on last_scraped date
        if manufacturer:
            missing = db.execute(
                """SELECT p.product_id, p.model_no, p.manufacturer, p.stic_url
                   FROM products p
                   WHERE p.eol=0 AND p.manufacturer=? AND p.product_group=?
                     AND p.product_id NOT IN (
                         SELECT DISTINCT product_id FROM stic_prices
                         WHERE date=? AND manufacturer=? AND product_group=?
                     )
                   ORDER BY p.model_no""",
                (manufacturer, product_group, last_scraped, manufacturer, product_group)
            ).fetchall()
        else:
            missing = db.execute(
                """SELECT p.product_id, p.model_no, p.manufacturer, p.stic_url
                   FROM products p
                   WHERE p.eol=0 AND p.product_group=?
                     AND p.product_id NOT IN (
                         SELECT DISTINCT product_id FROM stic_prices
                         WHERE date=? AND product_group=?
                     )
                   ORDER BY p.model_no""",
                (product_group, last_scraped, product_group)
            ).fetchall()

        for row in missing:
            result.append({
                "label":        label,
                "last_scraped": last_scraped,
                "product_id":   row["product_id"],
                "model_no":     row["model_no"],
                "manufacturer": row["manufacturer"],
                "stic_url":     row["stic_url"],
            })
    db.close()
    return jsonify(result)


_missing_scrape_jobs = {}   # label → {"proc": proc, "done": bool}

@app.route("/api/scrape/missing-group", methods=["POST"])
def scrape_missing_group_trigger():
    """Scrape only the missing SKUs for a given group label."""
    import subprocess
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
    ]
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    group = next((g for g in SCRAPE_GROUPS if g[2] == label), None)
    if not group:
        return jsonify({"started": False, "error": f"Unknown group: {label}"})

    manufacturer, product_group, _ = group
    db = get_db()

    # Find last scrape date for this group
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

    # Find missing product IDs
    if manufacturer:
        missing = db.execute(
            """SELECT product_id FROM products
               WHERE eol=0 AND manufacturer=? AND product_group=?
                 AND product_id NOT IN (
                     SELECT DISTINCT product_id FROM stic_prices
                     WHERE date=? AND manufacturer=? AND product_group=?
                 )""",
            (manufacturer, product_group, last_scraped, manufacturer, product_group)
        ).fetchall() if last_scraped else []
    else:
        missing = db.execute(
            """SELECT product_id FROM products
               WHERE eol=0 AND product_group=?
                 AND product_id NOT IN (
                     SELECT DISTINCT product_id FROM stic_prices
                     WHERE date=? AND product_group=?
                 )""",
            (product_group, last_scraped, product_group)
        ).fetchall() if last_scraped else []
    db.close()

    if not missing:
        return jsonify({"started": False, "error": "No missing SKUs found"})

    pid_list = ",".join(str(r["product_id"]) for r in missing)
    try:
        proc = subprocess.Popen(
            ["/usr/bin/python3", "/opt/openclaw/data/stic/stic_scraper.py",
             "--rescrape", pid_list],
            stdout=open("/opt/openclaw/logs/stic.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _missing_scrape_jobs[label] = {"proc": proc, "done": False}
        return jsonify({"started": True, "label": label, "count": len(missing)})
    except Exception as e:
        return jsonify({"started": False, "error": str(e)})


@app.route("/api/scrape/missing-group/status")
def scrape_missing_group_status():
    label = request.args.get("label", "").strip()
    job = _missing_scrape_jobs.get(label)
    if not job:
        return jsonify({"done": True})
    if not job["done"] and job["proc"].poll() is not None:
        job["done"] = True
    return jsonify({"done": job["done"]})


@app.route("/api/scrape/group", methods=["POST"])
def scrape_group_trigger():
    """Launch stic_scraper.py --group <label> as a background subprocess."""
    import subprocess
    import shlex
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"started": False, "error": "No label provided"})

    VALID_LABELS = {
        "Palit GPU", "PowerColor GPU", "MSI GPU", "ASUS GPU", "Gigabyte GPU",
        "MSI Motherboards", "Gigabyte Motherboards", "ASUS Motherboards", "Server / Pro",
        "Probe SKUs",
    }
    if label not in VALID_LABELS:
        return jsonify({"started": False, "error": f"Unknown group: {label}"})

    try:
        cmd = [
            "/usr/bin/python3",
            "/opt/openclaw/data/stic/stic_scraper.py",
            "--group", label,
            "--force",   # manual portal trigger always re-scrapes regardless of prior runs today
        ]
        # Launch detached — portal doesn't wait for it
        proc = subprocess.Popen(
            cmd,
            stdout=open("/opt/openclaw/logs/stic.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _scrape_group_jobs[label] = {"proc": proc, "done": False}
        return jsonify({"started": True, "label": label})
    except Exception as e:
        return jsonify({"started": False, "error": str(e)})


# In-memory registry for group scrape jobs: {label: {"proc": proc, "done": bool}}
_scrape_group_jobs = {}

@app.route("/api/scrape/group/status")
def scrape_group_status():
    """Poll whether a group scrape has finished."""
    label = request.args.get("label", "").strip()
    job = _scrape_group_jobs.get(label)
    if not job:
        return jsonify({"done": True})   # no record → unblock poller
    if not job["done"] and job["proc"].poll() is not None:
        job["done"] = True
    return jsonify({"done": job["done"]})


# In-memory registry for single-SKU rescrape jobs: {product_id: {"pid": proc, "done": bool}}
_rescrape_jobs = {}

@app.route("/api/scrape/sku", methods=["POST"])
def scrape_sku_trigger():
    """Launch stic_scraper.py --rescrape <product_id> as a background subprocess."""
    import subprocess
    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"started": False, "error": "Invalid product_id"})

    try:
        cmd = [
            "/usr/bin/python3",
            "/opt/openclaw/data/stic/stic_scraper.py",
            "--rescrape", str(product_id),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=open("/opt/openclaw/logs/stic.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _rescrape_jobs[product_id] = {"proc": proc, "done": False}
        return jsonify({"started": True, "product_id": product_id})
    except Exception as e:
        return jsonify({"started": False, "error": str(e)})


@app.route("/api/scrape/sku/status")
def scrape_sku_status():
    """Poll whether a single-SKU rescrape has finished."""
    try:
        product_id = int(request.args.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"done": True})  # fail-safe: unblock the poller

    job = _rescrape_jobs.get(product_id)
    if not job:
        return jsonify({"done": True})  # no record → treat as done

    if not job["done"]:
        if job["proc"].poll() is not None:   # process has exited
            job["done"] = True

    return jsonify({"done": job["done"]})


@app.route("/api/import/template/new-skus")
def import_template_new_skus():
    """Return a CSV template for the Add/Update SKUs import tool."""
    from flask import Response
    # UTF-8 BOM so Excel opens it correctly without an import wizard
    csv = (
        "\ufeffProduct,model_no,manufacturer,product_group,description,chipset,ean\r\n"
        "123456,RTX 5090 GAMING OC 32G,GIGABYTE,PROD_VIDEO,GIGABYTE GeForce RTX 5090 Gaming OC 32G,RTX 5090,4719331314224\r\n"
    )
    return Response(
        csv,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=new-skus-template.csv"},
    )


@app.route("/api/import/new-skus/preview", methods=["POST"])
def import_new_skus_preview():
    """Parse uploaded CSV text, validate rows, return preview JSON (no DB writes)."""
    import csv, io
    data = request.get_json(silent=True) or {}
    raw = (data.get("csv") or "").lstrip("\ufeff").strip()   # strip UTF-8 BOM if present
    if not raw:
        return jsonify({"error": "No CSV content received."})

    KNOWN_GROUPS = {"PROD_VIDEO", "PROD_MBRD", "PROD_MBRDS"}
    REQUIRED     = ("product_id", "model_no", "manufacturer", "product_group")

    try:
        reader = csv.DictReader(io.StringIO(raw))
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {e}"})

    # Normalise header names (strip whitespace, lower)
    raw_rows = list(reader)
    if not raw_rows:
        return jsonify({"error": "CSV file appears to be empty (no data rows)."})

    # Check required headers exist (case-insensitive).
    # Accept "product" as an alias for "product_id" to match VIP system exports.
    headers_lower = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    if "product_id" not in headers_lower and "product" in headers_lower:
        headers_lower["product_id"] = headers_lower["product"]
        raw_rows = [{("product_id" if k.strip().lower() == "product" else k): v
                     for k, v in row.items()} for row in raw_rows]

    missing_hdrs  = [f for f in REQUIRED if f not in headers_lower]
    if missing_hdrs:
        return jsonify({"error": f"Missing required columns: {', '.join(missing_hdrs)}. "
                                  "Download the template to see the expected format."})

    # Normalise header key map: column name → canonical field name
    col = {f: headers_lower[f] for f in REQUIRED}
    col_opt = {}
    for f in ("description", "chipset", "ean", "stic_url"):
        if f in headers_lower:
            col_opt[f] = headers_lower[f]

    # Load existing product_ids for new/update classification
    db = get_db()
    existing_ids = {r["product_id"] for r in db.execute("SELECT product_id FROM products").fetchall()}
    db.close()

    valid_rows  = []
    error_rows  = []
    warn_count  = 0

    for i, row in enumerate(raw_rows, start=2):   # row 1 = header, data starts at 2
        # Normalise keys
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}

        # Validate required fields
        err = None
        pid_raw = r.get("product_id", "")
        try:
            pid = int(pid_raw)
        except ValueError:
            err = f"product_id must be an integer (got '{pid_raw}')"

        if not err and not r.get("model_no"):
            err = "model_no is required"
        if not err and not r.get("manufacturer"):
            err = "manufacturer is required"
        if not err and not r.get("product_group"):
            err = "product_group is required"

        if err:
            error_rows.append({
                "product_id": pid_raw, "model_no": r.get("model_no",""),
                "manufacturer": r.get("manufacturer",""), "product_group": r.get("product_group",""),
                "description": r.get("description",""), "chipset": r.get("chipset",""),
                "ean": r.get("ean",""), "_status": "error", "_note": err,
            })
            continue

        # Warn if product_group not recognised (but still accept it)
        note = ""
        status = "update" if pid in existing_ids else "new"
        if r.get("product_group") not in KNOWN_GROUPS:
            note = f"Unrecognised group '{r['product_group']}' (expected PROD_VIDEO, PROD_MBRD, or PROD_MBRDS)"
            status = "warn"
            warn_count += 1

        valid_rows.append({
            "product_id":    pid,
            "model_no":      r.get("model_no", ""),
            "manufacturer":  r.get("manufacturer", ""),
            "product_group": r.get("product_group", ""),
            "description":   r.get("description", "") or None,
            "chipset":       r.get("chipset", "") or None,
            "ean":           r.get("ean", "") or None,
            "_status":       status,
            "_note":         note,
        })

    summary = {
        "total":    len(raw_rows),
        "new":      sum(1 for r in valid_rows if r["_status"] == "new"),
        "update":   sum(1 for r in valid_rows if r["_status"] == "update"),
        "warnings": warn_count,
        "errors":   len(error_rows),
    }
    return jsonify({"valid_rows": valid_rows, "error_rows": error_rows, "summary": summary})


@app.route("/api/import/new-skus/confirm", methods=["POST"])
def import_new_skus_confirm():
    """Write previously-previewed rows to the products table."""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    if not rows:
        return jsonify({"error": "No rows to import."})

    db = get_db()
    existing = {r["product_id"] for r in db.execute("SELECT product_id FROM products").fetchall()}

    added = updated = skipped = 0
    try:
        for r in rows:
            try:
                pid = int(r["product_id"])
            except (ValueError, TypeError):
                skipped += 1
                continue
            if not r.get("model_no") or not r.get("manufacturer") or not r.get("product_group"):
                skipped += 1
                continue
            if pid in existing:
                db.execute(
                    """UPDATE products SET
                         model_no=?, manufacturer=?, product_group=?,
                         description=?, chipset=?, ean=?
                       WHERE product_id=?""",
                    (r["model_no"], r["manufacturer"], r["product_group"],
                     r.get("description"), r.get("chipset"), r.get("ean"), pid)
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO products
                         (product_id, model_no, manufacturer, product_group,
                          description, chipset, ean, eol)
                       VALUES (?,?,?,?,?,?,?,0)""",
                    (pid, r["model_no"], r["manufacturer"], r["product_group"],
                     r.get("description"), r.get("chipset"), r.get("ean"))
                )
                added += 1
        db.commit()
    except Exception as e:
        db.close()
        return jsonify({"error": str(e)})
    db.close()
    return jsonify({"added": added, "updated": updated, "skipped": skipped})


@app.route("/api/export/skus")
def export_skus():
    """Download all active (non-EOL) SKUs as a UTF-8 BOM CSV."""
    import csv, io
    from flask import Response
    rows = qry(
        "SELECT product_id AS Product, model_no, manufacturer, product_group, description, chipset, ean "
        "FROM products WHERE eol=0 ORDER BY product_id"
    )
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM for Excel
    writer = csv.DictWriter(
        buf,
        fieldnames=["Product","model_no","manufacturer","product_group","description","chipset","ean"],
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=openclaw-skus.csv"},
    )


@app.route("/api/import/template/eol-status")
def import_template_eol_status():
    """Return a CSV template for the Update EOL Status import tool."""
    from flask import Response
    csv = (
        "\ufeffProduct,Product_Status\r\n"
        "123456,25\r\n"
        "234567,55\r\n"
        "345678,42\r\n"
    )
    return Response(
        csv,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=eol-status-template.csv"},
    )


@app.route("/api/import/eol-status/preview", methods=["POST"])
def import_eol_status_preview():
    """Parse uploaded CSV, validate, classify each row, return preview JSON (no DB writes)."""
    import csv, io
    data = request.get_json(silent=True) or {}
    raw = (data.get("csv") or "").lstrip("\ufeff").strip()
    if not raw:
        return jsonify({"error": "No CSV content received."})

    try:
        reader = csv.DictReader(io.StringIO(raw))
        raw_rows = list(reader)
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {e}"})

    if not raw_rows:
        return jsonify({"error": "CSV file appears to be empty (no data rows)."})

    # Accept "product" as an alias for "product_id" to match VIP system exports.
    headers_lower = {h.strip().lower() for h in (reader.fieldnames or [])}
    if "product_id" not in headers_lower and "product" in headers_lower:
        headers_lower.add("product_id")
        raw_rows = [{("product_id" if k.strip().lower() == "product" else k): v
                     for k, v in row.items()} for row in raw_rows]

    missing = [f for f in ("product_id", "product_status") if f not in headers_lower]
    if missing:
        return jsonify({"error": f"Missing required columns: {', '.join(missing)}. "
                                  "Expected: product_id (or Product), product_status"})

    # Load current EOL state and model info from DB
    db = get_db()
    products = {
        r["product_id"]: r
        for r in db.execute(
            "SELECT product_id, model_no, eol FROM products"
        ).fetchall()
    }
    db.close()

    # EOL status codes: 0–39 = Active, 40/42 = EOL, 41/43–49 = Active, 50–99 = EOL
    EOL_CODES = set(range(50, 100)) | {40, 42}

    valid_rows = []
    error_rows = []
    set_active = set_eol = no_change = not_found = 0

    for row in raw_rows:
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}

        # Validate product_id
        pid_raw = r.get("product_id", "")
        try:
            pid = int(pid_raw)
        except ValueError:
            error_rows.append({
                "product_id": pid_raw, "product_status": r.get("product_status",""),
                "model_no": "—", "current_eol": None, "new_eol": None,
                "_action": "error", "_note": f"product_id must be an integer (got '{pid_raw}')",
            })
            continue

        # Validate product_status
        status_raw = r.get("product_status", "")
        try:
            status = int(status_raw)
            if not (0 <= status <= 99):
                raise ValueError()
        except ValueError:
            error_rows.append({
                "product_id": pid, "product_status": status_raw,
                "model_no": "—", "current_eol": None, "new_eol": None,
                "_action": "error",
                "_note": f"product_status must be an integer 0–99 (got '{status_raw}')",
            })
            continue

        # Look up in DB
        if pid not in products:
            error_rows.append({
                "product_id": pid, "product_status": status,
                "model_no": "—", "current_eol": None, "new_eol": None,
                "_action": "not_found", "_note": "VIP code not found in products table",
            })
            not_found += 1
            continue

        prod = products[pid]
        current_eol = prod["eol"]
        model_no    = prod["model_no"] or "—"

        # Classify using defined EOL codes
        new_eol = 1 if status in EOL_CODES else 0
        if new_eol == current_eol:
            action = "no_change"
            no_change += 1
        elif new_eol == 1:
            action = "set_eol"
            set_eol += 1
        else:
            action = "set_active"
            set_active += 1

        entry = {
            "product_id":     pid,
            "product_status": status,
            "model_no":       model_no,
            "current_eol":    current_eol,
            "new_eol":        new_eol,
            "_action":        action,
            "_note":          "",
        }
        if action == "no_change":
            error_rows.append(entry)   # shown in table but not written to DB
        else:
            valid_rows.append(entry)

    summary = {
        "total":      len(raw_rows),
        "set_active": set_active,
        "set_eol":    set_eol,
        "no_change":  no_change,
        "not_found":  not_found,
        "errors":     sum(1 for r in error_rows if r["_action"] == "error"),
    }
    return jsonify({"valid_rows": valid_rows, "error_rows": error_rows, "summary": summary})


@app.route("/api/import/eol-status/confirm", methods=["POST"])
def import_eol_status_confirm():
    """Apply EOL flag changes from previously-previewed rows."""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    if not rows:
        return jsonify({"error": "No rows to update."})

    db = get_db()
    set_active = set_eol = skipped = 0
    try:
        for r in rows:
            try:
                pid     = int(r["product_id"])
                new_eol = int(r["new_eol"])   # 0 or 1
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            if new_eol not in (0, 1):
                skipped += 1
                continue
            db.execute("UPDATE products SET eol=? WHERE product_id=?", (new_eol, pid))
            if new_eol == 1: set_eol    += 1
            else:             set_active += 1
        db.commit()
    except Exception as e:
        db.close()
        return jsonify({"error": str(e)})
    db.close()
    return jsonify({"set_active": set_active, "set_eol": set_eol, "skipped": skipped})


@app.route("/api/stic-url/<int:product_id>", methods=["POST"])
def stic_url_set(product_id):
    """Save a manually-supplied STIC product URL to the products table."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or "/Product/" not in url:
        return jsonify({"saved": False, "error": "Invalid URL — must contain /Product/"})
    db = get_db()
    db.execute("UPDATE products SET stic_url=? WHERE product_id=?", (url, product_id))
    changed = db.execute("SELECT changes()").fetchone()[0]
    db.commit()
    db.close()
    return jsonify({"saved": bool(changed), "product_id": product_id, "url": url})


@app.route("/api/notes/<int:product_id>", methods=["POST"])
def notes_set(product_id):
    """Save or clear notes for a product."""
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "").strip() or None   # empty string → NULL
    db = get_db()
    db.execute("UPDATE products SET notes=? WHERE product_id=?", (notes, product_id))
    db.commit()
    db.close()
    return jsonify({"saved": True, "product_id": product_id})


# ── Catalogue API ─────────────────────────────────────────────────────────────

@app.route("/api/catalogue/products")
def catalogue_products():
    """Return all active products for the Catalogue Products view."""
    db = get_db()
    rows = db.execute(
        "SELECT product_id, model_no, manufacturer, product_group, description, "
        "chipset, ean, msrp FROM products WHERE eol=0 ORDER BY product_id"
    ).fetchall()
    db.close()
    return jsonify({"products": [dict(r) for r in rows]})


@app.route("/api/catalogue/retailer-ids")
def catalogue_retailer_ids():
    """Return all retailer IDs joined with product model_no."""
    db = get_db()
    rows = db.execute(
        "SELECT r.product_id, p.model_no, r.amazon_asin, r.currys_sku, r.very_sku, "
        "r.argos_sku, r.ccl_url, r.awdit_url, r.scan_ln, r.scan_url, r.ocuk_code, "
        "r.box_url, r.very_url "
        "FROM retailer_ids r "
        "LEFT JOIN products p ON p.product_id = r.product_id "
        "ORDER BY r.product_id"
    ).fetchall()
    db.close()
    return jsonify({"rows": [dict(r) for r in rows]})


@app.route("/api/catalogue/missing-msrp")
def catalogue_missing_msrp():
    """Return products missing MSRP with optional manufacturer/group filters."""
    mfr = request.args.get("mfr", "").strip()
    grp = request.args.get("grp", "").strip()
    db = get_db()

    # Summary — always unfiltered so the summary table shows everything
    summary_rows = db.execute(
        "SELECT manufacturer, product_group, "
        "COUNT(*) as total, "
        "SUM(CASE WHEN msrp IS NOT NULL AND msrp > 0 THEN 1 ELSE 0 END) as has_msrp, "
        "SUM(CASE WHEN msrp IS NULL OR msrp = 0 THEN 1 ELSE 0 END) as missing "
        "FROM products WHERE eol = 0 "
        "GROUP BY manufacturer, product_group "
        "ORDER BY manufacturer, product_group"
    ).fetchall()

    # Product list — filtered, only missing MSRP
    where = ["eol = 0", "(msrp IS NULL OR msrp = 0)"]
    params = []
    if mfr:
        where.append("manufacturer = ?")
        params.append(mfr)
    if grp:
        where.append("product_group = ?")
        params.append(grp)
    products = db.execute(
        f"SELECT product_id, model_no, manufacturer, product_group, chipset, ean "
        f"FROM products WHERE {' AND '.join(where)} "
        f"ORDER BY manufacturer, product_group, model_no",
        params
    ).fetchall()
    db.close()

    return jsonify({
        "summary":  [dict(r) for r in summary_rows],
        "products": [dict(r) for r in products],
    })


@app.route("/api/catalogue/missing-ean")
def catalogue_missing_ean():
    """Return products missing EAN with optional manufacturer/group filters."""
    mfr = request.args.get("mfr", "").strip()
    grp = request.args.get("grp", "").strip()
    db = get_db()

    summary_rows = db.execute(
        "SELECT manufacturer, product_group, "
        "COUNT(*) as total, "
        "SUM(CASE WHEN ean IS NOT NULL AND ean != '' THEN 1 ELSE 0 END) as has_ean, "
        "SUM(CASE WHEN ean IS NULL OR ean = '' THEN 1 ELSE 0 END) as missing "
        "FROM products WHERE eol = 0 "
        "GROUP BY manufacturer, product_group "
        "ORDER BY manufacturer, product_group"
    ).fetchall()

    where = ["eol = 0", "(ean IS NULL OR ean = '')"]
    params = []
    if mfr:
        where.append("manufacturer = ?")
        params.append(mfr)
    if grp:
        where.append("product_group = ?")
        params.append(grp)
    products = db.execute(
        f"SELECT product_id, model_no, manufacturer, product_group, chipset, msrp "
        f"FROM products WHERE {' AND '.join(where)} "
        f"ORDER BY manufacturer, product_group, model_no",
        params
    ).fetchall()
    db.close()

    return jsonify({
        "summary":  [dict(r) for r in summary_rows],
        "products": [dict(r) for r in products],
    })


@app.route("/api/catalogue/product/<int:product_id>", methods=["GET", "POST"])
def catalogue_product(product_id):
    db = get_db()
    if request.method == "GET":
        row = db.execute(
            "SELECT p.product_id, p.model_no, p.manufacturer, p.product_group, "
            "p.description, p.chipset, p.ean, p.msrp, "
            "r.amazon_asin, r.currys_sku, r.very_sku, r.very_url, r.argos_sku, "
            "r.ccl_url, r.awdit_url, r.scan_ln, r.scan_url, r.ocuk_code, r.box_url "
            "FROM products p "
            "LEFT JOIN retailer_ids r ON r.product_id = p.product_id "
            "WHERE p.product_id = ?", (product_id,)
        ).fetchone()
        db.close()
        if not row:
            return jsonify({"error": "Product not found"}), 404
        return jsonify(dict(row))

    # POST — update product fields and/or retailer IDs
    data = request.get_json(silent=True) or {}

    # ── Update products table ──────────────────────────────────────────────────
    product_fields = ["model_no", "manufacturer", "product_group", "description", "chipset", "ean", "msrp"]
    sets, params = [], []
    for field in product_fields:
        if field in data:
            val = data[field]
            if field == "msrp":
                if val is None or val == "":
                    val = None
                else:
                    try:
                        val = round(float(val), 2)
                        if val < 0:
                            db.close()
                            return jsonify({"error": "MSRP cannot be negative"})
                    except (ValueError, TypeError):
                        db.close()
                        return jsonify({"error": f"Invalid MSRP value: {val}"})
            if field == "product_group" and val not in ("PROD_VIDEO", "PROD_MBRD", "PROD_MBRDS", None, ""):
                db.close()
                return jsonify({"error": f"Invalid product_group: {val}"})
            sets.append(f"{field} = ?")
            params.append(val)

    if sets:
        params.append(product_id)
        db.execute(f"UPDATE products SET {', '.join(sets)} WHERE product_id = ?", params)

    # ── Upsert retailer_ids table ──────────────────────────────────────────────
    retailer_fields = ["amazon_asin", "currys_sku", "very_sku", "very_url", "argos_sku",
                       "ccl_url", "awdit_url", "scan_ln", "scan_url", "ocuk_code", "box_url"]
    ret_data = {f: data[f] for f in retailer_fields if f in data}
    if ret_data:
        # Fetch current row (may not exist yet)
        existing = db.execute(
            "SELECT * FROM retailer_ids WHERE product_id = ?", (product_id,)
        ).fetchone()
        if existing:
            # Merge: only overwrite columns present in the payload
            ret_sets = [f"{f} = ?" for f in ret_data]
            ret_params = list(ret_data.values()) + [product_id]
            db.execute(
                f"UPDATE retailer_ids SET {', '.join(ret_sets)} WHERE product_id = ?",
                ret_params
            )
        else:
            # No row yet — insert with whatever we have
            cols = ["product_id"] + list(ret_data.keys())
            vals = [product_id] + list(ret_data.values())
            placeholders = ", ".join("?" * len(vals))
            db.execute(
                f"INSERT INTO retailer_ids ({', '.join(cols)}) VALUES ({placeholders})",
                vals
            )

    if not sets and not ret_data:
        db.close()
        return jsonify({"error": "No fields to update"})

    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/import/template/retailer-ids-import")
def import_template_retailer_ids():
    headers = "Product,amazon_asin,currys_sku,very_sku,argos_sku,ccl_url,awdit_url,scan_ln,scan_url,ocuk_code,box_url"
    from flask import Response
    return Response(
        headers + "\n",
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=retailer_ids_template.csv"}
    )


@app.route("/api/import/retailer-ids-import/preview", methods=["POST"])
def import_retailer_ids_preview():
    """Preview retailer IDs import: validate CSV and return proposed changes."""
    import csv as _csv, io
    data_in = request.get_json(silent=True) or {}
    raw = (data_in.get("csv") or "").lstrip("﻿").strip()
    if not raw:
        return jsonify({"error": "No CSV content received."})

    try:
        reader   = _csv.DictReader(io.StringIO(raw))
        raw_rows = list(reader)
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {e}"})

    if not raw_rows:
        return jsonify({"error": "CSV file appears to be empty (no data rows)."})

    # Normalise headers — accept "product" as alias for "product_id"
    headers_lower = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    if "product_id" not in headers_lower and "product" in headers_lower:
        headers_lower["product_id"] = headers_lower["product"]
        raw_rows = [{("product_id" if k.strip().lower() == "product" else k): v
                     for k, v in row.items()} for row in raw_rows]

    if "product_id" not in headers_lower:
        return jsonify({"error": "Missing required column: Product"})

    RET_COLS = ["amazon_asin","currys_sku","very_sku","argos_sku","ccl_url","awdit_url",
                "scan_ln","scan_url","ocuk_code","box_url","very_url"]

    db = get_db()
    existing   = {str(r["product_id"]) for r in db.execute("SELECT product_id FROM retailer_ids").fetchall()}
    valid_pids = {str(r["product_id"]) for r in db.execute("SELECT product_id FROM products").fetchall()}
    db.close()

    preview_rows, errors = [], []
    for i, row in enumerate(raw_rows[:5000], 1):
        pid_raw = row.get("product_id") or ""
        pid = str(pid_raw).strip()
        if not pid:
            continue
        if pid not in valid_pids:
            errors.append(f"Row {i}: Product {pid} not found in products table")
            continue
        updates = {}
        for col in RET_COLS:
            for k, v in row.items():
                if k.strip().lower() == col.lower() and str(v).strip():
                    updates[col] = str(v).strip()
                    break
        if updates:
            preview_rows.append({
                "product_id": pid, "updates": updates,
                "action": "update" if pid in existing else "insert"
            })

    return jsonify({
        "valid_rows": preview_rows,
        "summary": {"total": len(preview_rows), "errors": len(errors)},
        "errors": errors[:20],
    })


@app.route("/api/import/retailer-ids-import/confirm", methods=["POST"])
def import_retailer_ids_confirm():
    """Apply confirmed retailer IDs import."""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    if not rows:
        return jsonify({"error": "No rows to import"}), 400

    RET_COLS = ["amazon_asin","currys_sku","very_sku","argos_sku","ccl_url","awdit_url",
                "scan_ln","scan_url","ocuk_code","box_url","very_url"]

    db = get_db()
    updated = 0
    try:
        for row in rows:
            pid     = int(row["product_id"])
            updates = {k: v for k, v in row.get("updates", {}).items() if k in RET_COLS}
            if not updates:
                continue
            db.execute("INSERT OR IGNORE INTO retailer_ids (product_id) VALUES (?)", (pid,))
            set_clause = ", ".join(f"{col}=?" for col in updates)
            vals       = list(updates.values()) + [pid]
            db.execute(f"UPDATE retailer_ids SET {set_clause} WHERE product_id=?", vals)
            updated += 1
        db.commit()
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({"error": str(e)})
    db.close()
    return jsonify({"updated": updated})


@app.route("/api/export/retailer-ids")
def export_retailer_ids():
    """Download all retailer IDs as CSV."""
    from flask import Response
    import io, csv as _csv
    db = get_db()
    rows = db.execute(
        "SELECT r.product_id, p.model_no, r.amazon_asin, r.currys_sku, r.very_sku, "
        "r.argos_sku, r.ccl_url, r.awdit_url, r.scan_ln, r.scan_url, r.ocuk_code, "
        "r.box_url, r.very_url "
        "FROM retailer_ids r LEFT JOIN products p ON p.product_id = r.product_id "
        "ORDER BY r.product_id"
    ).fetchall()
    db.close()
    out = io.StringIO()
    w   = _csv.writer(out)
    w.writerow(["Product","model_no","amazon_asin","currys_sku","very_sku","argos_sku",
                "ccl_url","awdit_url","scan_ln","scan_url","ocuk_code","box_url","very_url"])
    for r in rows:
        w.writerow([r["product_id"], r["model_no"] or "",
                    r["amazon_asin"] or "", r["currys_sku"] or "",
                    r["very_sku"] or "", r["argos_sku"] or "",
                    r["ccl_url"] or "", r["awdit_url"] or "",
                    r["scan_ln"] or "", r["scan_url"] or "",
                    r["ocuk_code"] or "", r["box_url"] or "", r["very_url"] or ""])
    from datetime import datetime as _dt
    fname = f"retailer_ids_{_dt.now().strftime('%Y-%m-%d')}.csv"
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ── MSRP import — shared backend logic ───────────────────────────────────────

def _msrp_preview(tool_id, csv_text):
    """Parse a MSRP import CSV and return preview data.
    tool_id: 'msrp-by-vip' | 'msrp-by-ean' | 'msrp-by-model'
    """
    import csv, io
    try:
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        raw_rows = list(reader)
    except Exception as e:
        return {"error": f"CSV parse error: {e}"}

    if not raw_rows:
        return {"error": "CSV appears to be empty."}

    # Normalise headers
    hmap = {h.strip().lower(): h.strip() for h in (reader.fieldnames or [])}

    # Determine key column for this tool
    if tool_id == "msrp-by-vip":
        key_col_candidates = ["product", "product_id", "vip", "vip code"]
        db_lookup = "product_id"
    elif tool_id == "msrp-by-ean":
        key_col_candidates = ["ean", "barcode", "ean13"]
        db_lookup = "ean"
    else:  # msrp-by-model
        key_col_candidates = ["model", "model_no", "model no", "model number"]
        db_lookup = "model_no"

    key_col = next((hmap[c] for c in key_col_candidates if c in hmap), None)
    msrp_col = next((hmap[c] for c in ["msrp", "rrp", "price", "recommended price"] if c in hmap), None)

    if not key_col:
        expected = key_col_candidates[0].title()
        return {"error": f"Missing key column. Expected: {expected}"}
    if not msrp_col:
        return {"error": "Missing MSRP column. Expected: MSRP"}

    db = get_db()
    # Load all products into a lookup dict
    all_products = db.execute(
        "SELECT product_id, model_no, manufacturer, ean, msrp FROM products"
    ).fetchall()
    db.close()

    lookup = {}
    for p in all_products:
        val = str(p[db_lookup] or "").strip()
        if val:
            lookup[val.lower()] = p

    valid_rows, error_rows = [], []
    total = matched = no_change = not_found = bad_value = 0

    for row in raw_rows[:5000]:
        total += 1
        key_raw      = str(row.get(key_col,  "") or "").strip()
        # Strip Excel float artifact: "4711387932445.00" → "4711387932445"
        if key_raw.endswith('.0') or '.00' in key_raw:
            import re as _re2
            key_raw = _re2.sub(r'\.0+$', '', key_raw)
        msrp_raw_orig = str(row.get(msrp_col, "") or "").strip()
        # Robust price parsing: handles £1,234.56 / 1.234,56 (EU) / 299,99 (EU decimal) / "359.00 GBP"
        import re as _re
        _m = _re.search(r'(\d[\d,.]*\d|\d)', msrp_raw_orig)
        if _m:
            _raw = _m.group(1)
            _has_dot   = '.' in _raw
            _has_comma = ',' in _raw
            if _has_dot and _has_comma:
                # Whichever separator comes last is the decimal
                if _raw.rindex('.') > _raw.rindex(','):
                    msrp_raw = _raw.replace(',', '')          # 1,234.56 → 1234.56
                else:
                    msrp_raw = _raw.replace('.', '').replace(',', '.')  # 1.234,56 → 1234.56
            elif _has_comma and not _has_dot:
                # Comma-only: if exactly 2 digits follow last comma it's a decimal separator
                if _re.search(r',\d{2}$', _raw):
                    msrp_raw = _raw.replace(',', '.')         # 299,99 → 299.99
                else:
                    msrp_raw = _raw.replace(',', '')          # 1,234 → 1234
            else:
                msrp_raw = _raw                               # 299.99 → 299.99
        else:
            msrp_raw = msrp_raw_orig

        if not key_raw:
            continue

        # Parse MSRP value
        try:
            new_msrp = round(float(msrp_raw), 2)
            if new_msrp <= 0:
                raise ValueError("zero/negative")
        except (ValueError, TypeError):
            bad_value += 1
            error_rows.append({
                "key_value": key_raw, "model_no": None, "manufacturer": None,
                "current_msrp": None, "new_msrp": msrp_raw_orig, "_action": "bad_value",
            })
            continue

        # Look up product
        product = lookup.get(key_raw.lower())
        if not product:
            not_found += 1
            error_rows.append({
                "key_value": key_raw, "model_no": None, "manufacturer": None,
                "current_msrp": None, "new_msrp": new_msrp, "_action": "not_found",
            })
            continue

        matched += 1
        cur = product["msrp"]
        if cur is not None and round(float(cur), 2) == new_msrp:
            no_change += 1
            error_rows.append({
                "key_value": key_raw, "model_no": product["model_no"],
                "manufacturer": product["manufacturer"],
                "current_msrp": cur, "new_msrp": new_msrp, "_action": "no_change",
            })
        else:
            valid_rows.append({
                "product_id": product["product_id"],
                "key_value":  key_raw,
                "model_no":   product["model_no"],
                "manufacturer": product["manufacturer"],
                "current_msrp": cur,
                "new_msrp":   new_msrp,
                "_action":    "update",
            })

    # ── Write import debug log ───────────────────────────────────────────────
    try:
        import json as _json
        _log_path = "/opt/openclaw/logs/import.log"
        _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _bad_rows  = [r for r in error_rows if r["_action"] == "bad_value"]
        _miss_rows = [r for r in error_rows if r["_action"] == "not_found"]
        _log_entry = {
            "ts": _ts, "tool": tool_id,
            "columns_in_file": list(hmap.keys()),
            "key_col_matched":  key_col,
            "msrp_col_matched": msrp_col,
            "summary": {
                "total": total, "matched": matched,
                "no_change": no_change, "not_found": not_found, "bad_value": bad_value,
            },
            "bad_value_rows":  [{"key": r["key_value"], "raw_msrp": r["new_msrp"]} for r in _bad_rows],
            "not_found_rows":  [{"key": r["key_value"], "new_msrp": r["new_msrp"]}  for r in _miss_rows[:50]],
        }
        with open(_log_path, "a") as _f:
            _f.write(_json.dumps(_log_entry) + "\n")
    except Exception:
        pass  # never break the import on logging failure
    # ────────────────────────────────────────────────────────────────────────

    return {
        "summary": {
            "total": total, "matched": matched,
            "no_change": no_change, "not_found": not_found, "bad_value": bad_value,
        },
        "valid_rows": valid_rows,
        "error_rows": error_rows,
    }


def _msrp_confirm(rows):
    """Write confirmed MSRP rows to the products table."""
    db = get_db()
    updated = skipped = not_found = 0
    for r in rows:
        pid = r.get("product_id")
        val = r.get("new_msrp")
        if not pid or val is None:
            skipped += 1
            continue
        cur = db.execute("SELECT msrp FROM products WHERE product_id=?", (pid,)).fetchone()
        if cur is None:
            not_found += 1
            continue
        db.execute("UPDATE products SET msrp=? WHERE product_id=?", (round(float(val), 2), pid))
        updated += 1
    db.commit()
    db.close()
    return {"updated": updated, "skipped": skipped, "not_found": not_found}


# ── MSRP by VIP Code ──────────────────────────────────────────────────────────

@app.route("/api/import/template/msrp-by-vip")
def msrp_by_vip_template():
    from flask import Response
    return Response("Product,MSRP\n123456,299.99\n",
                    mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=msrp_by_vip_template.csv"})

@app.route("/api/import/msrp-by-vip/preview", methods=["POST"])
def msrp_by_vip_preview():
    data = request.get_json(silent=True) or {}
    return jsonify(_msrp_preview("msrp-by-vip", data.get("csv", "")))

@app.route("/api/import/msrp-by-vip/confirm", methods=["POST"])
def msrp_by_vip_confirm():
    data = request.get_json(silent=True) or {}
    return jsonify(_msrp_confirm(data.get("rows", [])))


# ── MSRP by EAN ───────────────────────────────────────────────────────────────

@app.route("/api/import/template/msrp-by-ean")
def msrp_by_ean_template():
    from flask import Response
    return Response("EAN,MSRP\n4711377086578,299.99\n",
                    mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=msrp_by_ean_template.csv"})

@app.route("/api/import/msrp-by-ean/preview", methods=["POST"])
def msrp_by_ean_preview():
    data = request.get_json(silent=True) or {}
    return jsonify(_msrp_preview("msrp-by-ean", data.get("csv", "")))

@app.route("/api/import/msrp-by-ean/confirm", methods=["POST"])
def msrp_by_ean_confirm():
    data = request.get_json(silent=True) or {}
    return jsonify(_msrp_confirm(data.get("rows", [])))


# ── MSRP by Model ─────────────────────────────────────────────────────────────

@app.route("/api/import/template/msrp-by-model")
def msrp_by_model_template():
    from flask import Response
    return Response("Model,MSRP\nROG STRIX B650E-E GAMING WIFI,299.99\n",
                    mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=msrp_by_model_template.csv"})

@app.route("/api/import/msrp-by-model/preview", methods=["POST"])
def msrp_by_model_preview():
    data = request.get_json(silent=True) or {}
    return jsonify(_msrp_preview("msrp-by-model", data.get("csv", "")))

@app.route("/api/import/msrp-by-model/confirm", methods=["POST"])
def msrp_by_model_confirm():
    data = request.get_json(silent=True) or {}
    return jsonify(_msrp_confirm(data.get("rows", [])))


# ─────────────────────────────────────────────────────────────────────────────

def _init_watchlist():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        product_id INTEGER PRIMARY KEY,
        added_date TEXT NOT NULL
    )""")
    db.commit()
    db.close()

_init_watchlist()
_init_products()

if __name__ == "__main__":
    import os
    os.environ.setdefault("FLASK_SKIP_DOTENV", "1")
    app.run(host="0.0.0.0", port=8090, debug=False)
