# Server Rebuild Guide

Complete rebuild procedure after total server loss.
Assumes a fresh Ubuntu install on replacement hardware.

**Recovery sources:**
- **GitHub** (`syndrome-open-claw`) — all source code, scripts, config structure
- **TrueNAS** (`192.168.1.158:/mnt/Deep/backups/openclaw/`) — live data, secrets, platform config

---

## 1. OS Prerequisites

```bash
# Confirm Ubuntu 25.10 (or latest available)
lsb_release -a

# Create adminclaude user if not present
useradd -m -s /bin/bash adminclaude
usermod -aG sudo,docker adminclaude
```

---

## 2. Install System Dependencies

```bash
apt-get update
apt-get install -y \
    docker.io docker-compose \
    python3 python3-pip \
    rclone \
    xvfb \
    git \
    curl \
    openssh-client

# Python version should be 3.13.x
python3 --version
```

---

## 3. Install Python Packages

```bash
pip3 install \
    flask \
    playwright==1.58.0 \
    patchright==1.58.2 \
    playwright-stealth \
    openpyxl \
    requests \
    camoufox

# Install browser binaries
python3 -m playwright install chromium
python3 -m patchright install chromium
python3 -m camoufox fetch
```

---

## 4. Create Directory Structure

```bash
mkdir -p /opt/openclaw/{data/analytics,data/stic,data/general,data/travel,data/finance,data/recipes}
mkdir -p /opt/openclaw/{config,workspace,workspace-family,workspace-sales}
mkdir -p /opt/openclaw/{scripts,logs,docs,ha-config,skills}
chown -R adminclaude:adminclaude /opt/openclaw
```

---

## 5. Clone GitHub Repo

```bash
# Clone into docs/ — contains source code, scripts, agent config
cd /opt/openclaw
git clone https://github.com/jackd71-ops/syndrome-open-claw.git docs
# Note: you will need to add the GitHub PAT to the remote URL for push access:
# git -C /opt/openclaw/docs remote set-url origin https://<user>:<PAT>@github.com/jackd71-ops/syndrome-open-claw.git
```

---

## 6. Restore from TrueNAS

SSH into TrueNAS and rsync back, or run from the new server:

```bash
# Set up SSH key first (see step 7), then:
rsync -av \
    -e "ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw" \
    truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw/ \
    /opt/openclaw/restore-tmp/

# Then copy into place:
cp -r /opt/openclaw/restore-tmp/config/        /opt/openclaw/config/
cp -r /opt/openclaw/restore-tmp/workspace/     /opt/openclaw/workspace/
cp -r /opt/openclaw/restore-tmp/workspace-family/ /opt/openclaw/workspace-family/
cp -r /opt/openclaw/restore-tmp/workspace-sales/  /opt/openclaw/workspace-sales/
cp -r /opt/openclaw/restore-tmp/data/          /opt/openclaw/data/
cp -r /opt/openclaw/restore-tmp/scripts/       /opt/openclaw/scripts/
cp -r /opt/openclaw/restore-tmp/ha-config/     /opt/openclaw/ha-config/
cp    /opt/openclaw/restore-tmp/secrets.json   /opt/openclaw/secrets.json
cp    /opt/openclaw/restore-tmp/docker-compose.yml /opt/openclaw/docker-compose.yml
cp    /opt/openclaw/restore-tmp/Dockerfile     /opt/openclaw/Dockerfile

chmod +x /opt/openclaw/scripts/*.sh
chown -R adminclaude:adminclaude /opt/openclaw
```

**Key files restored from TrueNAS:**
- `data/analytics/prices.db` — all STIC + retailer price/stock history (includes products, retailer_ids, retailer_prices)
- `secrets.json` — Telegram token, STIC login credentials
- `config/` — OpenClaw platform config, manifest DB, credentials
- `workspace/` — agent memory and session notes
- `data/general/Retailer_Template.xlsx` — legacy retailer template (used by seed script on first rebuild if prices.db is empty)

> If `prices.db` is restored from backup it will already contain `retailer_ids` and `products.msrp`. Only run `seed_retailer_db.py` if the DB is new/empty and you need to seed from Excel.

---

## 7. SSH Key for TrueNAS Backup

The key is backed up inside TrueNAS itself. For initial restore you may need to
temporarily use password auth, or copy the key from another machine that has it.

```bash
# Place the backed-up key (restored from TrueNAS or another device):
cp id_ed25519_openclaw     /home/adminclaude/.ssh/id_ed25519_openclaw
cp id_ed25519_openclaw.pub /home/adminclaude/.ssh/id_ed25519_openclaw.pub
chmod 600 /home/adminclaude/.ssh/id_ed25519_openclaw
chmod 644 /home/adminclaude/.ssh/id_ed25519_openclaw.pub
chown adminclaude:adminclaude /home/adminclaude/.ssh/id_ed25519_openclaw*
```

---

## 8. Rebuild Docker Container

```bash
cd /opt/openclaw
docker build -t openclaw-custom:latest .
docker compose up -d
# Verify container is running:
docker ps | grep openclaw
```

---

## 9. Portal Systemd Service

**Important:** The sales portal (`portal.py`) runs as a **host systemd service**, NOT inside the OpenClaw Docker container. The Docker container runs the Kevin AI agent only. They are completely separate processes.

```bash
cat > /etc/systemd/system/openclaw-portal.service << 'EOF'
[Unit]
Description=OpenClaw Sales Portal
After=network.target

[Service]
User=adminclaude
WorkingDirectory=/opt/stic-scraper/analytics
ExecStart=/usr/bin/python3 /opt/stic-scraper/analytics/portal.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable openclaw-portal
systemctl start openclaw-portal

# Verify:
systemctl status openclaw-portal
curl -s http://localhost:8090/api/stic/kpi | head -c 100
```

### Service Architecture — Restart Procedures

| Component | What it is | How to restart |
|---|---|---|
| `openclaw` Docker container | Kevin AI agent (Node.js) | `bash /opt/openclaw/scripts/safe-restart.sh` |
| `openclaw-portal` systemd service | Sales portal Flask app (Python) | `sudo systemctl restart openclaw-portal` |
| Scraper processes | Cron-scheduled Python scripts | Re-run the cron command manually, or wait for next schedule |

**Never use `safe-restart.sh` to restart the portal** — it only affects the Docker container and will not pick up portal.py changes. Always use `systemctl restart openclaw-portal` after editing `portal.py`.

---

## 10. Cron Jobs

Run as `adminclaude`:

```bash
crontab -e
```

Add:

```
# ── OpenClaw / Backup ─────────────────────────────────────────────────────────
0 3 * * *   /opt/openclaw/scripts/backup.sh
0 2 * * *   /opt/openclaw/scripts/git-sync.sh >> /opt/openclaw/logs/git-sync.log 2>&1
0 8,18 * * * /usr/bin/python3 /opt/openclaw/data/travel/check_watchlist.py >> /opt/openclaw/logs/travel.log 2>&1

# ── STIC scraper ──────────────────────────────────────────────────────────────
# Morning: full run all 13 groups (Mon-Fri 06:30 UK)
30 6 * * 1-5 TZ=Europe/London python3 /opt/stic-scraper/scraper/stic_scraper.py --runall >> /opt/stic-scraper/logs/stic_cron.log 2>&1
# Afternoon: 6 volatile groups only, force=True — price move check (Mon-Fri 12:30 UK)
30 12 * * 1-5 TZ=Europe/London python3 /opt/stic-scraper/scraper/stic_scraper.py --pm >> /opt/stic-scraper/logs/stic_cron.log 2>&1

# ── Amazon scraper ────────────────────────────────────────────────────────────
0 8  * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/amazon_scraper.py --slot am >> /opt/stic-scraper/logs/amazon_cron.log 2>&1
0 13 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/amazon_scraper.py --slot pm >> /opt/stic-scraper/logs/amazon_cron.log 2>&1

# ── Retailer scraper — URL discovery (nightly 01:00, fast after first run) ───
0 1 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --discover >> /opt/stic-scraper/logs/retailer_discovery.log 2>&1

# ── Retailer scraper — 8 parallel sessions, one per retailer (09:30 daily) ───
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer Currys      >> /opt/stic-scraper/logs/retailer_currys.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer Scan        >> /opt/stic-scraper/logs/retailer_scan.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer Overclockers >> /opt/stic-scraper/logs/retailer_ocuk.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer Box         >> /opt/stic-scraper/logs/retailer_box.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer "CCL Online" >> /opt/stic-scraper/logs/retailer_ccl.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer AWD-IT      >> /opt/stic-scraper/logs/retailer_awdit.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer Very        >> /opt/stic-scraper/logs/retailer_very.log 2>&1
30 9 * * * TZ=Europe/London /usr/bin/python3 /opt/stic-scraper/scraper/retailer_scraper.py --retailer Argos       >> /opt/stic-scraper/logs/retailer_argos.log 2>&1
```

---

## 11. rclone / OneDrive

rclone OAuth token is backed up to TrueNAS at `/mnt/Deep/backups/openclaw/rclone-config/`.

```bash
# Restore from TrueNAS backup:
mkdir -p /home/adminclaude/.config/rclone
rsync -av \
  -e "ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw" \
  truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw/rclone-config/ \
  /home/adminclaude/.config/rclone/
chown -R adminclaude:adminclaude /home/adminclaude/.config/rclone

# Verify it works:
rclone lsd onedrive:
```

If the token has expired (unlikely but possible), re-authenticate:

```bash
rclone config reconnect onedrive:
# drive_id: 47252F09A1E9A101 (personal drive)
```

---

## 12. Verify Everything

```bash
# Portal responding
curl -s http://localhost:8090/api/stic/kpi | python3 -m json.tool

# Docker container healthy
docker ps --format "{{.Names}}: {{.Status}}"

# Scraper dry-run (no write)
python3 /opt/openclaw/data/stic/stic_scraper.py --help

# Backup reachable
ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw truenas_admin@192.168.1.158 "ls /mnt/Deep/backups/openclaw/"
```

---

## File Permissions Reference

```bash
chmod 644 /opt/openclaw/docker-compose.yml /opt/openclaw/Dockerfile
chmod +x   /opt/openclaw/scripts/*.sh
chmod 600  /opt/openclaw/secrets.json
chown -R adminclaude:adminclaude /opt/openclaw
```

---

## Notes

- **prices.db WAL mode** — after restore, run `sqlite3 /opt/stic-scraper/data/prices.db "PRAGMA wal_checkpoint(FULL);"` to ensure DB is clean
- **Portal port** — `portal.py` serves on `:8090`, local network only. This is a **host systemd service** (`openclaw-portal`), not inside Docker. Restart with `sudo systemctl restart openclaw-portal`, not `safe-restart.sh`.
- **OpenClaw Docker container** — Kevin AI agent only. Ports: 18789 (main), 3000 (internal), 2099 (manifest public). Restart with `bash /opt/openclaw/scripts/safe-restart.sh`.
- **Scraper files location** — `/opt/stic-scraper/scraper/` (Python scripts), `/opt/stic-scraper/analytics/portal.py` (portal), `/opt/stic-scraper/logs/` (all log files), `/opt/stic-scraper/data/` (DB + progress files)
- **TrueNAS backup path** — `/mnt/Deep/backups/openclaw/`
- **GitHub repo** — `jackd71-ops/syndrome-open-claw`
- Update this document whenever cron jobs, services, packages, or architecture changes

---

## DB Schema

All operational data lives in `prices.db`. Key tables:

### `products` — SKU catalogue (shared by STIC and Retailer scrapers)

| Column | Type | Notes |
|---|---|---|
| product_id | INTEGER PK | VIP 6-digit code (displayed as "Product" in portal/CSV) |
| model_no | TEXT | |
| manufacturer | TEXT | |
| product_group | TEXT | PROD_VIDEO, PROD_MBRD, PROD_MBRDS |
| description | TEXT | |
| chipset | TEXT | |
| ean | TEXT | |
| eol | INTEGER | 0 = active, 1 = EOL |
| stic_url | TEXT | Cached STIC product detail URL (auto-populated by scraper) |
| msrp | REAL | Recommended retail price |

### `retailer_not_stocked` — explicit per-retailer not-stocked flags

| Column | Type | Notes |
|---|---|---|
| product_id | INTEGER | FK → products |
| retailer | TEXT | e.g. "Amazon", "Currys" |
| source | TEXT | "import", "edit", "audit" |
| set_date | TEXT | ISO date when flag was set |

PK is `(product_id, retailer)`. Auto-cleared when a code/URL is added for that retailer.

### `housekeeping_log` — monthly task completion tracking

| Column | Type | Notes |
|---|---|---|
| task_id | TEXT PK | Static task identifier (e.g. "new-skus") |
| last_done | TEXT | ISO date of last completion |

### `retailer_ids` — retailer-specific IDs per product

| Column | Type | Notes |
|---|---|---|
| product_id | INTEGER PK | FK → products |
| amazon_asin | TEXT | |
| currys_sku | TEXT | |
| very_sku | TEXT | |
| very_url | TEXT | Direct product URL (auto-discovered) |
| argos_sku | TEXT | Direct product ID (e.g. "WL 3063295"); scraped via patchright + Xvfb |
| ccl_url | TEXT | Direct product URL (auto-discovered) |
| awdit_url | TEXT | Direct product URL (auto-discovered) |
| scan_ln | TEXT | LN code for Google discovery |
| scan_url | TEXT | Direct product URL (auto-discovered) |
| ocuk_code | TEXT | OCUK product code for Overclockers |
| box_url | TEXT | Direct product URL (auto-discovered) |

**On a fresh rebuild**, seed `retailer_ids` and `products.msrp` from the Excel backup:

```bash
python3 /opt/openclaw/scripts/seed_retailer_db.py
```

This script reads `Retailer_Template.xlsx` from `/opt/openclaw/data/general/` — restore this file from TrueNAS backup first. The seed script is safe to re-run (uses INSERT OR REPLACE).

EOL flag is managed via the portal Catalogue → Update EOL Status tool or ⛔ View EOL SKUs.

---

## Portal — Catalogue Tab

The portal has three tabs: **STIC** (distributor data), **Retailer** (market prices), and **Catalogue** (product management).

The Catalogue tab provides:

**Products section:**
| Action | Description |
|---|---|
| View / Search SKUs | Searchable table of all active products — click any row to edit inline |
| Add / Update SKUs | CSV import to upsert products (EOL not touched) |
| Update EOL Status | CSV import to bulk-set EOL flags |
| View EOL SKUs | List of EOL products with restore button |
| Export Active SKUs | CSV download of all non-EOL products |

**Retailers section:**
| Action | Description |
|---|---|
| View Retailer IDs | Searchable table of all retailer IDs/URLs |
| Import Retailer IDs | CSV import to add/update ASINs, SKUs, URLs |
| Export Retailer IDs | CSV download including 9 `ns_*` not-stocked boolean columns |
| Missing URLs Report | Filter by retailer/mfr/group; SKU count; export to Excel; shows not-stocked flags |
| Not Stocked Flags | Audit page — view all `retailer_not_stocked` entries; unset individually or bulk |
| Missing URLs Import | Catalogue → Missing URLs; import the exported Excel back to update codes/URLs/not-stocked |

**Not Stocked flags (`retailer_not_stocked` table):**
- Explicit per-retailer flag: `(product_id, retailer, source, set_date)`
- Set via: import, inline checkbox in product edit modal, or audit page
- Auto-cleared when a code/URL is set for that retailer (code always wins)
- Flag means "we know this retailer doesn't stock it" — suppresses from missing reports

**MSRP section:**
| Action | Description |
|---|---|
| Import by VIP Code | CSV: `Product,MSRP` — matches on VIP 6-digit code |
| Import by EAN | CSV: `EAN,MSRP` — matches on EAN (strips Excel `.00` float suffix automatically) |
| Import by Model | CSV: `Model,MSRP` — matches on model_no |
| Missing MSRP Report | Summary by manufacturer/group + filterable product list; click row to add MSRP inline |
| Missing EAN Report | Summary by manufacturer/group + filterable product list; click row to add EAN inline |

**Scraper section:**
| Action | Description |
|---|---|
| Manual Triggers | STIC scraper group triggers + Retailer scraper single-SKU and mfr/group triggers |

Manual Triggers page has three panels:
1. **Live banner** — shows if a scraper is currently running with progress bar
2. **STIC Scraper** — table of all 13 groups with last-scraped date and Run button
3. **Retailer Scraper** — Single SKU search (autocomplete → select → retailer filter → Run); Product Group panel (manufacturer + group dropdowns + retailer filter → Run)

**Admin section:**
| Action | Description |
|---|---|
| Monthly Housekeeping | 10 recurring tasks with colour-coded age badges (green <35d, amber <70d, red 70d+); mark-done stores date in `housekeeping_log` DB table |

**Inline product editing:** Clicking any row in View/Search SKUs or either missing report opens an edit modal. All fields are editable: Model, Manufacturer, Product Group, Description, Chipset, EAN, MSRP. Each of the 9 retailers has a "Not stocked" checkbox inline. After saving, the search filter re-runs and the row disappears if it no longer matches.

**MSRP import notes:**
- Parser handles `£299.99`, `299.99 GBP`, European decimal `299,99`, European thousands `1.234,56`
- EANs exported as floats from Excel (`4711387932445.00`) are stripped automatically
- Broken Excel cells (`#REF!`, blank) are flagged as bad value — fix in the source spreadsheet
- Every preview is logged to `/opt/openclaw/logs/import.log` (JSON lines) for diagnostics

## Portal — STIC Tab

**Chipset Daily Overview drill-down:**
Clicking a chipset row now shows a three-layer panel:
1. **Channel Stock Holding — Historical**: stacked bar chart (canvas, no external libs) showing daily total stock per distributor across all available dates. Distributor colours match all other portal charts (VIP=blue, M2M=amber, TD Synnex=red, Target=grey, Westcoast=green, Ingram=teal). Hover tooltip shows date + per-distributor breakdown.
2. **SKU table**: per-product stock, VIP stock, floor price, VIP price.

**Scraper section:**
The STIC tab has its own Scraper Health view (🩺 Scraper Health) mirrored from the Retailer tab. Both instances auto-refresh every 60 seconds while the section is active; the interval self-clears on navigation away.

---

## Scraper Groups

STIC scraper runs as 13 manufacturer/product-group segments (PowerColor removed — VIP-distributed only, not in channel). Each group sends its own Telegram on completion.

| Label | Manufacturer | Group | Morning | Afternoon (PM) |
|---|---|---|---|---|
| Palit GPU | PALIT | PROD_VIDEO | ✓ | ✓ |
| MSI GPU | MSI | PROD_VIDEO | ✓ | ✓ |
| ASUS GPU | ASUS | PROD_VIDEO | ✓ | ✓ |
| Gigabyte GPU | GIGABYTE | PROD_VIDEO | ✓ | ✓ |
| MSI Motherboards | MSI | PROD_MBRD | ✓ | — |
| Gigabyte Motherboards | GIGABYTE | PROD_MBRD | ✓ | — |
| ASUS Motherboards | ASUS | PROD_MBRD | ✓ | — |
| Server / Pro | (all) | PROD_MBRDS | ✓ | — |
| AMD Retail CPU | AMD Retail | PROD_CPU | ✓ | ✓ |
| AMD MPK CPU | AMD MPK | PROD_CPU | ✓ | ✓ |
| Intel CPU | Intel | PROD_CPU | ✓ | — |
| Intel OEM CPU | Intel OEM | PROD_CPU | ✓ | — |
| Probe SKUs | (all) | PROBE | ✓ | — |

**Afternoon (PM) session** (`--pm` flag): re-scrapes the 6 most price-volatile groups with `force=True` to capture intraday price moves. Does **not** use the progress file — always overwrites morning rows.

**CLI:**
```bash
python3 stic_scraper.py --runall              # all 13 groups, random 0–10 min start delay
python3 stic_scraper.py --pm                  # afternoon: 6 volatile groups only, force=True
python3 stic_scraper.py --gpus                # GPU groups only (PROD_VIDEO)
python3 stic_scraper.py --cpus-amd            # AMD CPU groups only
python3 stic_scraper.py --cpus-intel          # Intel CPU groups only
python3 stic_scraper.py --group "ASUS GPU"    # single named group, no delay
python3 stic_scraper.py --rescrape 123456,234567  # re-scrape specific VIP codes
```

Groups can also be triggered from the portal: Catalogue → Scraper → Manual Triggers.

---

## Retailer Scraper

Reads products from `products` table and retailer IDs from `retailer_ids` table (no Excel dependency).

**Architecture — 8 parallel sessions:**
All 8 retailers run simultaneously at 09:30, one session per retailer, each scraping all ~587 active products for their retailer. Each session has its own log file and progress file so crashes are independent. Peak RAM usage ~9.2 GB with browser restart every 100 products.

**CLI:**
```bash
python3 retailer_scraper.py --retailer Currys           # single retailer, all products
python3 retailer_scraper.py --retailer "CCL Online"     # (quote names with spaces)
python3 retailer_scraper.py --discover                  # URL discovery only (nightly 01:00)
python3 retailer_scraper.py --test                      # first 20 products, no retailer filter
python3 retailer_scraper.py --product 126285            # single product_id, all retailers
python3 retailer_scraper.py --product 126285 --retailer Amazon  # single product, one retailer
python3 retailer_scraper.py --mfr ASUS --group PROD_VIDEO       # all ASUS GPUs, all retailers
python3 retailer_scraper.py --mfr ASUS --group PROD_VIDEO --retailer Scan  # filtered
```

Manual triggers for `--product` and `--mfr`/`--group` are also available in the portal: Catalogue → Scraper → Manual Triggers → Retailer Scraper section.

**Discovery** runs nightly at 01:00 as a standalone job — finds product URLs for retailers that use URL-based scraping (AWD-IT, Scan, Box, CCL, Very). After the initial full discovery pass, only new products without URLs are searched so subsequent runs complete in minutes.

**Log files** (one per retailer session):
```
/opt/stic-scraper/logs/retailer_currys.log
/opt/stic-scraper/logs/retailer_scan.log
/opt/stic-scraper/logs/retailer_ocuk.log
/opt/stic-scraper/logs/retailer_box.log
/opt/stic-scraper/logs/retailer_ccl.log
/opt/stic-scraper/logs/retailer_awdit.log
/opt/stic-scraper/logs/retailer_very.log
/opt/stic-scraper/logs/retailer_argos.log
/opt/stic-scraper/logs/retailer_discovery.log
```

**Progress files** (allow crash recovery per-retailer):
```
/opt/stic-scraper/data/retailer_progress_{DD-MM-YYYY}_{retailer}.json
```

**Bot detection bypass — headed mode:**
All scrapers run patchright (patched Chromium) in **headed mode** (`headless=False`) via Xvfb virtual display. This bypasses Cloudflare and Akamai Bot Manager on a residential IP. Key requirements:
- `xvfb` must be installed (`apt-get install xvfb`)
- `patchright` installed and browser binary fetched (`python3 -m patchright install chromium`)

**Standalone scrapers** (called as subprocesses via `xvfb-run --auto-servernum`):

| File | Retailer | Notes |
|---|---|---|
| `argos_scrape.py` | Argos | Takes `"WL XXXXXXX"` SKU; navigates product page directly; JSON-LD price extraction |
| `very_scrape.py` | Very | Takes full `.prd` URL; searches by numeric product ID; reads price from search results (avoids Akamai-blocked product page) |
| `ocuk_scrape.py` | Overclockers | Takes OCUK product code |

**Very URL discovery:**
Very SKUs in `retailer_ids.very_sku` are WGDSH-style codes. Discovery uses Very's own search box + network response interception to capture the redirect URL (`.prd` format), stored in `retailer_ids.very_url`.

**STIC daily overview — chipset source of truth:**
The chipset overview reads `COALESCE(products.chipset, stic_prices.chipset)` — edits to chipset in the Catalogue tab are reflected immediately in the overview without waiting for a rescrape. If you bulk-rename chipsets in the `products` table, run:
```sql
UPDATE stic_prices SET chipset = (
    SELECT p.chipset FROM products p WHERE p.product_id = stic_prices.product_id
) WHERE product_id IN (SELECT product_id FROM products WHERE chipset IS NOT NULL AND chipset != '');
```
to backfill historic `stic_prices` rows.
