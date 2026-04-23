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
    r = qry_one(f"SELECT MAX(date) AS d FROM {table}")
    return r["d"] if r else None

def prev_date(table, current):
    r = qry_one(
        f"SELECT MAX(date) AS d FROM {table} WHERE date < ?", (current,)
    )
    return r["d"] if r else None

# ── Chipset extraction ─────────────────────────────────────────────────────────

_CHIPSET_RE = re.compile(
    r'\b(Z[0-9]{3}[A-Z]?|B[0-9]{3}[A-Z]?|H[0-9]{3}[A-Z]?|X[0-9]{3}[A-Z]?|'
    r'A[0-9]{3}[A-Z]?|W[0-9]{3}[A-Z]?|TRX[0-9]+|WRX[0-9]+)\b'
)

def extract_chipset(model_no):
    m = _CHIPSET_RE.search(model_no.upper())
    return m.group(1) if m else "Other"

# ── HTML template ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw Sales Portal</title>
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

  /* Badges */
  .badge { display: inline-block; padding: 2px 6px; border-radius: 2px; font-size: 11px;
           font-weight: 600; }
  .badge-red { background: #FDE7E9; color: #A4262C; }
  .badge-green { background: #DFF6DD; color: #107C10; }
  .badge-orange { background: #FFF4CE; color: #8A4B00; }
  .badge-blue { background: #DEECF9; color: #0078D4; }

  /* Charts */
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .chart-box { background: #fff; border: 1px solid #EDEBE9; border-radius: 2px; padding: 16px; }
  .chart-box h4 { font-size: 12px; font-weight: 600; color: #605E5C; margin-bottom: 10px; }
  .chart-box canvas { max-height: 200px; }

  /* Spinner */
  .spinner { text-align: center; padding: 40px; color: #A19F9D; font-size: 13px; }

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
  <span class="tab-bar-title">OpenClaw</span>
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
  </div>

  <div class="main" id="main-stic">
    <!-- Overview -->
    <div class="content-section active" id="stic-overview">
      <div id="stic-kpi" class="kpi-row"><div class="spinner">Loading…</div></div>
      <div class="section-title">Chipset Daily Overview</div>
      <div class="tbl-wrap" id="stic-chipset-tbl"><div class="spinner">Loading…</div></div>
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

<script>
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
      <div class="kpi-card"><div class="label">Last Scraped</div><div class="value" style="font-size:16px">${data.latest_date}</div><div class="sub">${data.dates_tracked} dates tracked</div></div>
    `;
  });

  fetch('/api/stic/chipset-overview').then(r=>r.json()).then(rows => {
    if (!rows.length) { document.getElementById('stic-chipset-tbl').innerHTML = '<p style="color:#A19F9D;padding:20px">No data</p>'; return; }
    const cols = ['Chipset','VIP SKUs','Channel Floor £','VIP Lowest £','VIP vs Floor','Channel Stock'];
    let html = '<table><thead><tr>' + cols.map(c=>`<th>${c}</th>`).join('') + '</tr></thead><tbody>';
    rows.forEach(r => {
      const diff = (r.vip_price && r.floor_price) ? ((r.vip_price - r.floor_price) / r.floor_price * 100).toFixed(1) : null;
      const diffBadge = diff === null ? '' : diff > 5 ? `<span class="badge badge-red">+${diff}%</span>` : diff > 0 ? `<span class="badge badge-orange">+${diff}%</span>` : `<span class="badge badge-green">${diff}%</span>`;
      html += `<tr>
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
    let html = '<div class="section-title">Results (' + rows.length + ')</div><div class="tbl-wrap"><table><thead><tr><th>Product ID</th><th>Model</th><th>Manufacturer</th><th>Channel Stock</th><th>Lowest Price</th></tr></thead><tbody>';
    rows.forEach(r => {
      html += `<tr class="clickable" onclick="loadSticSku(${r.product_id},'search')">
        <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
        <td>${(r.total_stock||0).toLocaleString()}</td>
        <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
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

function renderSticSku(data) {
  const el = document.getElementById('stic-sku-content');
  const { info, snapshot, price_history, stock_history, cheapest_history } = data;

  let html = `<h3 style="margin-bottom:8px">${info.manufacturer} ${info.model_no}</h3>
    <p style="color:#605E5C;margin-bottom:16px">Product ID: ${info.product_id} | Group: ${info.product_group||'—'}</p>`;

  // Snapshot table
  html += '<div class="section-title">Current Snapshot</div><div class="tbl-wrap"><table><thead><tr><th>Distributor</th><th>Price</th><th>Stock</th></tr></thead><tbody>';
  snapshot.forEach(r => {
    html += `<tr><td>${r.distributor}</td><td>${r.price ? '£'+r.price.toFixed(2) : '<span class="badge badge-orange">No price</span>'}</td><td>${r.qty !== null ? r.qty : '—'}</td></tr>`;
  });
  html += '</tbody></table></div>';

  // Cheapest history table
  html += '<div class="section-title">Cheapest Price History</div><div class="tbl-wrap"><table><thead><tr><th>Date</th><th>Distributor</th><th>Price</th></tr></thead><tbody>';
  cheapest_history.forEach(r => {
    html += `<tr><td>${r.date}</td><td>${r.distributor}</td><td>${r.price ? '£'+r.price.toFixed(2) : '—'}</td></tr>`;
  });
  html += '</tbody></table></div>';

  el.innerHTML = html;

  // Charts
  const dists = [...new Set(price_history.map(r => r.distributor))];
  const dates  = [...new Set(price_history.map(r => r.date))].sort();
  const palette = ['#0078D4','#E88C1A','#8A8886','#FFB900','#107C10','#D13438'];

  const priceDs = dists.map((d, i) => ({
    label: d,
    data: dates.map(dt => { const row = price_history.find(r => r.distributor===d && r.date===dt); return row?.price ?? null; }),
    borderColor: palette[i % palette.length], backgroundColor: 'transparent',
    tension: 0.2, spanGaps: true, pointRadius: 2,
  }));

  const stockDs = dists.map((d, i) => ({
    label: d,
    data: dates.map(dt => { const row = stock_history.find(r => r.distributor===d && r.date===dt); return row?.qty ?? 0; }),
    backgroundColor: palette[i % palette.length],
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

  const opts = (type, datasets, stacked) => ({
    type, data: { labels: dates, datasets },
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
  no_channel_stock: 'No Channel Stock 5+ Days',
  back_in_stock: 'Back In Stock (zero yesterday, stock today)',
  single_distributor: 'Single Distributor Remaining',
  new_stock_arrival: 'New Stock Arrival (5+ days absent, now stocked)',
  vip_out_on_price: 'VIP Out on Price (has stock, not cheapest)',
  vip_static: 'VIP Static — Market Moving',
  vip_exclusive: 'VIP Exclusive (only stocked distributor)',
  vip_price_gap: 'VIP Price Gap (£ above cheapest)',
  never_stocked: 'No Channel Stock Ever (potential exclusives)',
  price_dropping: 'Price Dropping vs Yesterday',
  price_rising: 'Price Rising (all distributors up 7 days)',
  daily_changes: 'All Changes Since Yesterday',
};

function renderReport(name, rows) {
  const title = REPORT_TITLES[name] || name;
  if (!rows.length) {
    document.getElementById('stic-report-content').innerHTML =
      `<div class="section-title">${title}</div><p style="color:#A19F9D;padding:20px">No items match this report.</p>`;
    return;
  }

  let cols, rowFn;

  if (name === 'vip_price_gap') {
    cols = ['Product ID','Model','Manufacturer','VIP £','Floor £','Gap £'];
    rowFn = r => `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
      <td>${r.vip_price ? '£'+r.vip_price.toFixed(2) : '—'}</td>
      <td>${r.floor_price ? '£'+r.floor_price.toFixed(2) : '—'}</td>
      <td><span class="badge badge-red">+£${r.gap.toFixed(2)}</span></td>
    </tr>`;
  } else if (name === 'daily_changes') {
    cols = ['Product ID','Model','Distributor','Yesterday £','Today £','Change','Stock'];
    rowFn = r => {
      const diff = r.price_today !== null && r.price_yesterday !== null ? r.price_today - r.price_yesterday : null;
      const badge = diff === null ? '' : diff > 0 ? `<span class="badge badge-red">+£${diff.toFixed(2)}</span>` : `<span class="badge badge-green">£${diff.toFixed(2)}</span>`;
      return `<tr><td>${r.product_id}</td><td>${r.model_no}</td><td>${r.distributor}</td>
        <td>${r.price_yesterday ? '£'+r.price_yesterday.toFixed(2) : '—'}</td>
        <td>${r.price_today ? '£'+r.price_today.toFixed(2) : '—'}</td>
        <td>${badge}</td><td>${r.qty_today ?? '—'}</td></tr>`;
    };
  } else if (name === 'price_dropping' || name === 'price_rising') {
    cols = ['Product ID','Model','Manufacturer','Yesterday £','Today £','Change'];
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
    cols = ['Product ID','Model','Manufacturer','Channel Stock','Lowest Price'];
    rowFn = r => `<tr class="clickable" onclick="loadSticSku(${r.product_id},'report')">
      <td>${r.product_id}</td><td>${r.model_no}</td><td>${r.manufacturer}</td>
      <td>${(r.total_stock||0).toLocaleString()}</td>
      <td>${r.min_price ? '£'+r.min_price.toFixed(2) : '—'}</td>
    </tr>`;
  }

  let html = `<div class="section-title">${title} <span style="font-size:12px;font-weight:400;color:#605E5C">(${rows.length} items)</span></div>
    <div class="tbl-wrap"><table><thead><tr>${cols.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>`;
  rows.forEach(r => { html += rowFn(r); });
  html += '</tbody></table></div>';
  document.getElementById('stic-report-content').innerHTML = html;
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
      <div class="kpi-card"><div class="label">Last Scraped</div><div class="value" style="font-size:16px">${data.latest_date}</div></div>
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
    let html = '<div class="section-title">Results (' + rows.length + ')</div><div class="tbl-wrap"><table><thead><tr><th>Product ID</th><th>Model</th><th>Manufacturer</th><th>Lowest Price</th><th>Below MSRP</th></tr></thead><tbody>';
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
    <p style="color:#605E5C;margin-bottom:16px">Product ID: ${info.product_id} | MSRP: ${info.msrp ? '£'+info.msrp.toFixed(2) : '—'}</p>`;

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
    data: { labels: dates, datasets },
    options: { responsive:true, maintainAspectRatio:true,
               plugins:{legend:{labels:{font:{size:10}}}},
               scales:{x:{ticks:{font:{size:10}}}, y:{ticks:{font:{size:10}}}} }
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
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

    rows = qry(
        """SELECT product_id, model_no, distributor, price, qty
           FROM stic_prices WHERE date=?""", (latest,)
    )

    chipset_data = {}
    for r in rows:
        cs = extract_chipset(r["model_no"])
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
        """SELECT DISTINCT s.product_id, s.model_no, s.manufacturer,
               SUM(s.qty) AS total_stock,
               MIN(CASE WHEN s.price > 0 THEN s.price END) AS min_price
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
        "SELECT product_id, model_no, manufacturer, product_group FROM stic_prices WHERE product_id=? LIMIT 1",
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

    STOCK_SUM = "SUM(COALESCE(qty,0))"
    PRICE_MIN = "MIN(CASE WHEN price>0 THEN price END)"

    if name == "no_channel_stock":
        cutoff_date = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 5)"
        )["d"]
        rows = qry(
            f"""SELECT product_id, model_no, manufacturer,
                   {PRICE_MIN} AS min_price, {STOCK_SUM} AS total_stock
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
                   SUM(t.qty) AS total_stock, MIN(CASE WHEN t.price>0 THEN t.price END) AS min_price
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
                   {PRICE_MIN} AS min_price, {STOCK_SUM} AS total_stock
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
                   SUM(t.qty) AS total_stock, MIN(CASE WHEN t.price>0 THEN t.price END) AS min_price
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
            f"""SELECT v.product_id, v.model_no, v.manufacturer,
                   v.price AS vip_price, v.qty AS vip_stock,
                   f.min_price AS floor_price
                FROM stic_prices v
                JOIN (SELECT product_id, {PRICE_MIN} AS min_price FROM stic_prices WHERE date=? GROUP BY product_id) f
                  ON f.product_id = v.product_id
                WHERE v.date=? AND v.distributor='VIP'
                  AND v.qty > 0 AND v.price IS NOT NULL
                  AND v.price > f.min_price
                ORDER BY v.qty DESC LIMIT 200""",
            (latest, latest)
        )

    elif name == "vip_static":
        cutoff = qry_one(
            "SELECT MIN(date) AS d FROM (SELECT DISTINCT date FROM stic_prices ORDER BY date DESC LIMIT 7)"
        )["d"]
        rows = qry(
            f"""SELECT v.product_id, v.model_no, v.manufacturer,
                   v.qty AS vip_stock, v.price AS vip_price,
                   mkt.total_stock AS total_stock, 0 AS min_price
                FROM stic_prices v
                JOIN (SELECT product_id, {STOCK_SUM} AS total_stock FROM stic_prices WHERE date=? GROUP BY product_id) mkt
                  ON mkt.product_id = v.product_id
                WHERE v.date=? AND v.distributor='VIP' AND v.qty > 0
                  AND v.product_id IN (
                    SELECT product_id FROM stic_prices WHERE distributor='VIP' AND date >= ?
                    GROUP BY product_id
                    HAVING MAX(COALESCE(qty,0)) = MIN(COALESCE(qty,0)) AND COUNT(DISTINCT date) >= 5
                  )
                ORDER BY v.qty DESC LIMIT 100""",
            (latest, latest, cutoff)
        )

    elif name == "vip_exclusive":
        rows = qry(
            f"""SELECT product_id, model_no, manufacturer,
                   {PRICE_MIN} AS min_price,
                   SUM(CASE WHEN distributor='VIP' THEN COALESCE(qty,0) ELSE 0 END) AS total_stock
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
                   v.price AS vip_price, f.min_price AS floor_price,
                   (v.price - f.min_price) AS gap
                FROM stic_prices v
                JOIN (SELECT product_id, {PRICE_MIN} AS min_price FROM stic_prices WHERE date=? GROUP BY product_id) f
                  ON f.product_id = v.product_id
                WHERE v.date=? AND v.distributor='VIP' AND v.qty>0
                  AND v.price IS NOT NULL AND f.min_price IS NOT NULL
                  AND v.price > f.min_price
                ORDER BY gap DESC LIMIT 100""",
            (latest, latest)
        )

    elif name == "never_stocked":
        rows = qry(
            f"""SELECT DISTINCT product_id, model_no, manufacturer,
                   0 AS total_stock, NULL AS min_price
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


if __name__ == "__main__":
    import os
    os.environ.setdefault("FLASK_SKIP_DOTENV", "1")
    app.run(host="0.0.0.0", port=8090, debug=False)
