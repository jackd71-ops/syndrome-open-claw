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
  .kpi-row { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .kpi-card { background: #fff; border: 1px solid #EDEBE9; border-radius: 2px;
               padding: 14px 18px; flex: 1; min-width: 140px; }
  .kpi-card .label { font-size: 11px; color: #A19F9D; text-transform: uppercase;
                     letter-spacing: .4px; margin-bottom: 6px; }
  .kpi-card .value { font-size: 24px; font-weight: 600; color: #323130; }
  .kpi-card .sub { font-size: 11px; color: #605E5C; margin-top: 4px; }

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
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        EOL Products <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadEOLProducts(this)">⛔ View EOL SKUs</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Scraper <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadScrapeGroups(this)">⟳ Refresh SKUs</button>
      </div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-section-header" onclick="toggleSection(this)">
        Import / Export <span class="arrow">▾</span>
      </div>
      <div class="sidebar-items">
        <button class="sidebar-btn" onclick="loadImportExport(this)">📥 Import / Export</button>
      </div>
    </div>
  </div>

  <div class="main" id="main-stic">
    <!-- Overview -->
    <div class="content-section active" id="stic-overview">
      <div id="stic-kpi" class="kpi-row"><div class="spinner">Loading…</div></div>
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
    <!-- EOL Products -->
    <div class="content-section" id="stic-eol">
      <div id="stic-eol-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Scrape Groups -->
    <div class="content-section" id="stic-scrape">
      <div id="stic-scrape-content"><div class="spinner">Loading…</div></div>
    </div>
    <!-- Import / Export -->
    <div class="content-section" id="stic-import-export">
      <div id="stic-import-export-content"></div>
    </div>
  </div>
</div>

<!-- Retailer layout -->
<div class="layout" id="layout-retailer" style="display:none">
  <div class="main" style="padding:20px">
    <div id="retailer-kpi" class="kpi-row"><div class="spinner">Loading…</div></div>
    <div class="search-bar">
      <input id="ret-search-input" type="text" placeholder="Search by model number or description…" onkeydown="if(event.key==='Enter')doRetSearch()">
      <button onclick="doRetSearch()">Search</button>
    </div>
    <div id="ret-search-results"></div>
    <div class="content-section" id="ret-sku">
      <button class="back-btn" onclick="document.getElementById('ret-sku').classList.remove('active')">← Back</button>
      <div id="ret-sku-content"><div class="spinner">Loading…</div></div>
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

<script>
// ── Date formatting ───────────────────────────────────────────────────────────
function fmtDate(d) {
  if (!d || d === '—') return d;
  // "2026-04-22" → "22/04/26"
  const parts = d.split('-');
  if (parts.length !== 3) return d;
  return `${parts[2]}/${parts[1]}/${parts[0].slice(2)}`;
}

// ── Tab switching ─────────────────────────────────────────────────────────────
let currentTab = 'stic';
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.getElementById('layout-stic').style.display = (tab === 'stic') ? 'flex' : 'none';
  document.getElementById('layout-retailer').style.display = (tab === 'retailer') ? 'flex' : 'none';
  if (tab === 'retailer' && !retailerKpiLoaded) loadRetailerKpi();
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
  fetch('/api/stic/kpi').then(r=>r.json()).then(data => {
    const el = document.getElementById('stic-kpi');
    el.innerHTML = `
      <div class="kpi-card"><div class="label">SKUs Tracked</div><div class="value">${data.total_skus}</div><div class="sub">products</div></div>
      <div class="kpi-card"><div class="label">Channel Stock Today</div><div class="value">${data.channel_stock_today.toLocaleString()}</div><div class="sub">units across all distributors</div></div>
      <div class="kpi-card"><div class="label">SKUs In Stock</div><div class="value">${data.skus_in_stock}</div><div class="sub">of ${data.total_skus} tracked</div></div>
      <div class="kpi-card"><div class="label">SKUs No Stock</div><div class="value">${data.skus_no_stock}</div><div class="sub">zero channel inventory</div></div>
      <div class="kpi-card"><div class="label">Last Scraped</div><div class="value" style="font-size:16px">${fmtDate(data.latest_date)}</div><div class="sub">${data.dates_tracked} dates tracked</div></div>
    `;
  });

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
    const eolSection = document.getElementById('stic-eol');
    if (eolSection && eolSection.classList.contains('active')) loadEOLProducts();
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
  const input = document.getElementById('stic-url-input-' + pid);
  const url = input ? input.value.trim() : '';
  if (!url) { alert('Please paste a STIC URL first'); return; }
  fetch('/api/stic-url/' + pid, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  }).then(r => r.json()).then(data => {
    if (data.saved) {
      alert('URL saved — scraper will use it as sanity check from next run');
      loadSticSku(pid, currentSection);  // refresh the product page
    } else {
      alert('Save failed');
    }
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
    document.getElementById('stic-chipset-tbl').innerHTML = html;
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
      document.getElementById('stic-chipset-drill-tbl').innerHTML = html;
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
    document.getElementById('stic-search-results').innerHTML = html;
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
    </span>
  </div>`;

  // Snapshot table
  html += '<div class="section-title">Current Snapshot</div><div class="tbl-wrap"><table><thead><tr><th>Distributor</th><th>Price</th><th>Stock</th></tr></thead><tbody>';
  snapshot.forEach(r => {
    html += `<tr><td>${r.distributor}</td><td>${r.price ? '£'+r.price.toFixed(2) : '<span class="badge badge-orange">No price</span>'}</td><td>${r.qty !== null ? r.qty : '—'}</td></tr>`;
  });
  html += '</tbody></table></div>';

  // Cheapest history table
  html += '<div class="section-title">Cheapest Price History</div><div class="tbl-wrap"><table><thead><tr><th>Date</th><th>Distributor</th><th>Price</th></tr></thead><tbody>';
  cheapest_history.forEach(r => {
    html += `<tr><td>${fmtDate(r.date)}</td><td>${r.distributor}</td><td>${r.price ? '£'+r.price.toFixed(2) : '—'}</td></tr>`;
  });
  html += '</tbody></table></div>';

  el.innerHTML = html;

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

  const chartHtml = `<div class="chart-grid">
    <div class="chart-box"><h4>Price per Distributor</h4><canvas id="chart-price"></canvas></div>
    <div class="chart-box"><h4>Cheapest Price Trend</h4><canvas id="chart-cheapest"></canvas></div>
    <div class="chart-box"><h4>Stock per Distributor</h4><canvas id="chart-stock"></canvas></div>
  </div>`;
  el.innerHTML += chartHtml;

  const fmtDates = dates.map(fmtDate);
  const opts = (type, datasets, stacked) => ({
    type, data: { labels: fmtDates, datasets },
    options: { responsive:true, maintainAspectRatio:true, plugins:{legend:{labels:{font:{size:10}}}},
                scales: { x:{ticks:{font:{size:10}}}, y:{stacked: stacked||false, ticks:{font:{size:10}}} } }
  });

  new Chart(document.getElementById('chart-price'), opts('line', priceDs));
  new Chart(document.getElementById('chart-cheapest'), opts('line', cheapestDs));
  new Chart(document.getElementById('chart-stock'), opts('bar', stockDs, true));
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
  document.getElementById('stic-report-content').innerHTML = html;

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

    el.innerHTML = html;
    _refreshAllEolBtns();
  });
}

function loadEOLProducts(btn) {
  if (btn) {
    document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  }
  document.querySelectorAll('#main-stic .content-section').forEach(s=>s.classList.remove('active'));
  document.getElementById('stic-eol').classList.add('active');
  const el = document.getElementById('stic-eol-content');
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
    if (data.started) {
      btn.textContent = '⏳ Running…';
    } else {
      btn.disabled = false;
      btn.textContent = '▶ Run';
      _scrapeGroupsRunning[label] = false;
      alert('Failed to start: ' + (data.error || 'unknown error'));
    }
  }).catch(() => {
    btn.disabled = false;
    btn.textContent = '▶ Run';
    _scrapeGroupsRunning[label] = false;
    alert('Network error — could not start scrape.');
  });
}

// ── Import / Export ───────────────────────────────────────────────────────────

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
];

let _ieCurrentTool = null;    // tool id currently open
let _iePreviewRows  = [];     // validated rows from last preview, ready to confirm

function loadImportExport(btn) {
  if (btn) {
    document.querySelectorAll('#sidebar-stic .sidebar-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  document.querySelectorAll('#main-stic .content-section').forEach(s => s.classList.remove('active'));
  document.getElementById('stic-import-export').classList.add('active');
  _ieCurrentTool = null;
  _renderIeToolCards();
}

function _renderIeToolCards() {
  const el = document.getElementById('stic-import-export-content');
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
  const el = document.getElementById('stic-import-export-content');
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
      <input type="file" id="ie-file-${toolId}" accept=".csv,text/csv"
             onchange="_ieFileSelected(this,'${toolId}')">
      <div class="dz-icon">📂</div>
      <div class="dz-text">Click to browse or drag &amp; drop a CSV file here</div>
      <div class="dz-hint">CSV format · UTF-8 or UTF-8 BOM · Max 5 000 rows</div>
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
  const reader = new FileReader();
  reader.onload = e => _ieUploadForPreview(toolId, e.target.result, file.name);
  reader.onerror = () => { dz.querySelector('.dz-text').textContent = 'Error reading file.'; };
  reader.readAsText(file);   // UTF-8; server strips BOM
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
    } else {
      resultHtml += `
        <span style="color:#107C10;font-weight:600;font-size:14px">✓ Import complete</span>
        <span style="color:#107C10">Added: <strong>${data.added}</strong></span>
        <span style="color:#0078D4">Updated: <strong>${data.updated}</strong></span>
        ${data.skipped ? `<span style="color:#A19F9D">Skipped: <strong>${data.skipped}</strong></span>` : ''}`;
    }
    resultHtml += `</div>
      <p style="color:#605E5C;font-size:12px;padding:0 0 12px">Changes are live immediately. The nightly sync will push them to OneDrive on the next run.</p>
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
  if (toolId === 'export-skus') {
    window.location.href = '/api/export/skus';
  }
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
  document.getElementById('ret-search-results').innerHTML = '<div class="spinner">Searching…</div>';
  document.getElementById('ret-sku').classList.remove('active');
  fetch('/api/retailer/search?q=' + encodeURIComponent(q)).then(r=>r.json()).then(rows => {
    if (!rows.length) { document.getElementById('ret-search-results').innerHTML = '<p style="color:#A19F9D;padding:20px">No results</p>'; return; }
    let html = '<div class="section-title">Results (' + rows.length + ')</div><div class="tbl-wrap"><table><thead><tr><th>Product</th><th>Model</th><th>Manufacturer</th><th>Lowest Price</th><th>Below MSRP</th></tr></thead><tbody>';
    rows.forEach(r => {
      html += `<tr class="clickable" onclick="loadRetSku(${r.product_id})">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
        <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
        <td>${r.below_msrp_count > 0 ? '<span class="badge badge-red">Yes ('+r.below_msrp_count+')</span>' : '—'}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    document.getElementById('ret-search-results').innerHTML = html;
  });
}

// ── Retailer SKU drill-down ───────────────────────────────────────────────────
function loadRetSku(productId) {
  document.getElementById('ret-sku').classList.add('active');
  document.getElementById('ret-sku-content').innerHTML = '<div class="spinner">Loading…</div>';
  document.getElementById('ret-search-results').style.display = 'none';
  document.getElementById('ret-sku').querySelector('.back-btn').onclick = () => {
    document.getElementById('ret-sku').classList.remove('active');
    document.getElementById('ret-search-results').style.display = '';
  };
  fetch('/api/retailer/sku/' + productId).then(r=>r.json()).then(data => {
    renderRetSku(data);
  });
}

function renderRetSku(data) {
  const el = document.getElementById('ret-sku-content');
  const { info, snapshot, price_history } = data;

  let html = `<h3 style="margin-bottom:8px">${info.manufacturer} ${info.model_no}</h3>
    <p style="color:#605E5C;margin-bottom:16px">Product: ${info.product_id} | MSRP: ${info.msrp ? '£'+info.msrp.toFixed(2) : '—'}</p>`;

  html += '<div class="section-title">Current Snapshot</div><div class="tbl-wrap"><table><thead><tr><th>Retailer</th><th>Price</th><th>vs MSRP</th></tr></thead><tbody>';
  snapshot.forEach(r => {
    const belowBadge = r.below_msrp === 1 ? '<span class="badge badge-red">Below MSRP</span>' : (r.price ? '<span class="badge badge-green">Above MSRP</span>' : '');
    html += `<tr><td>${r.retailer}</td><td>${r.price ? '£'+r.price.toFixed(2) : '<span style="color:#A19F9D">No data</span>'}</td><td>${belowBadge}</td></tr>`;
  });
  html += '</tbody></table></div>';

  el.innerHTML = html;

  // Price history chart
  const retailers = [...new Set(price_history.map(r => r.retailer))];
  const dates = [...new Set(price_history.map(r => r.date))].sort();
  const palette = ['#0078D4','#E88C1A','#8A8886','#FFB900','#107C10','#D13438','#00B7C3','#8764B8','#69797E'];

  const datasets = retailers.map((ret, i) => ({
    label: ret,
    data: dates.map(dt => { const row = price_history.find(r => r.retailer===ret && r.date===dt); return row?.price ?? null; }),
    borderColor: palette[i % palette.length], backgroundColor: 'transparent',
    tension: 0.2, spanGaps: true, pointRadius: 2,
  }));

  el.innerHTML += `<div class="chart-box" style="margin-bottom:20px"><h4>Price Trend by Retailer</h4><canvas id="ret-chart-price" style="max-height:220px"></canvas></div>`;
  new Chart(document.getElementById('ret-chart-price'), {
    type: 'line',
    data: { labels: dates.map(fmtDate), datasets },
    options: { responsive:true, maintainAspectRatio:true,
               plugins:{legend:{labels:{font:{size:10}}}},
               scales:{x:{ticks:{font:{size:10}}}, y:{ticks:{font:{size:10}}}} }
  });
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
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify([])

    group = request.args.get("group", "mbrd")
    group_filter = {
        "mbrd":   "product_group = 'PROD_MBRD'",
        "server": "product_group = 'PROD_MBRDS'",
        "gpu":    "product_group = 'PROD_VIDEO'",
    }.get(group, "product_group = 'PROD_MBRD'")

    rows = qry(
        f"""SELECT product_id, model_no, chipset, distributor, price, qty
           FROM stic_prices WHERE date=? AND {group_filter}""", (latest,)
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
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify([])

    group   = request.args.get("group", "mbrd")
    chipset = request.args.get("chipset", "").strip()
    if not chipset:
        return jsonify([])

    group_filter = {
        "mbrd":   "product_group = 'PROD_MBRD'",
        "server": "product_group = 'PROD_MBRDS'",
        "gpu":    "product_group = 'PROD_VIDEO'",
    }.get(group, "product_group = 'PROD_MBRD'")

    rows = qry(
        f"""SELECT product_id, model_no, manufacturer, chipset, distributor, price, qty
           FROM stic_prices WHERE date=? AND {group_filter}""", (latest,)
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
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify([])

    like = f"%{q}%"
    rows = qry(
        """SELECT s.product_id, s.model_no, s.manufacturer,
               SUM(s.qty) AS total_stock,
               MAX(CASE WHEN s.distributor='VIP' THEN COALESCE(s.qty,0) END) AS vip_stock,
               MIN(CASE WHEN s.price > 0 THEN s.price END) AS min_price,
               MAX(CASE WHEN s.distributor='VIP' THEN s.price END) AS vip_price
           FROM stic_prices s
           WHERE s.date=?
             AND (CAST(s.product_id AS TEXT) LIKE ? OR s.model_no LIKE ? OR s.manufacturer LIKE ?)
           GROUP BY s.product_id, s.model_no, s.manufacturer
           ORDER BY s.model_no
           LIMIT 100""",
        (latest, like, like, like)
    )
    return jsonify(rows)


@app.route("/api/stic/sku/<int:product_id>")
def stic_sku(product_id):
    latest = latest_date("stic_prices")
    if not latest:
        return jsonify({})

    info = qry_one(
        """SELECT s.product_id, s.model_no, s.manufacturer, s.product_group,
               p.stic_url, p.ean, p.description
           FROM stic_prices s
           LEFT JOIN products p ON p.product_id = s.product_id
           WHERE s.product_id=? LIMIT 1""",
        (product_id,)
    ) or {}

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
           FROM stic_prices WHERE product_id=? AND price IS NOT NULL
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

        result.append({
            "label":        label,
            "manufacturer": manufacturer,
            "product_group": product_group,
            "sku_count":    sku_count,
            "last_scraped": last_scraped,
        })
    db.close()
    return jsonify(result)


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
    }
    if label not in VALID_LABELS:
        return jsonify({"started": False, "error": f"Unknown group: {label}"})

    try:
        cmd = [
            "/usr/bin/python3",
            "/opt/openclaw/data/stic/stic_scraper.py",
            "--group", label,
        ]
        # Launch detached — portal doesn't wait for it
        subprocess.Popen(
            cmd,
            stdout=open("/opt/openclaw/logs/stic.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return jsonify({"started": True, "label": label})
    except Exception as e:
        return jsonify({"started": False, "error": str(e)})


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
