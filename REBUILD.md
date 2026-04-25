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
- `data/analytics/prices.db` — all STIC + retailer price/stock history
- `secrets.json` — Telegram token, STIC login credentials
- `config/` — OpenClaw platform config, manifest DB, credentials
- `workspace/` — agent memory and session notes
- `data/general/*.xlsx` — STIC and Retailer templates

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

```bash
cat > /etc/systemd/system/openclaw-portal.service << 'EOF'
[Unit]
Description=OpenClaw Sales Portal
After=network.target

[Service]
User=adminclaude
WorkingDirectory=/opt/openclaw/data/analytics
ExecStart=/usr/bin/python3 /opt/openclaw/data/analytics/portal.py
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

---

## 10. Cron Jobs

Run as `adminclaude`:

```bash
crontab -e
```

Add:

```
0 3 * * * /opt/openclaw/scripts/backup.sh
# STIC scraper - morning full run all groups (fires 8:25am, adds 0-10min random delay Mon-Fri)
25 8 * * 1-5 TZ=Europe/London python3 /opt/openclaw/data/stic/stic_scraper.py --runall >> /opt/openclaw/logs/stic_cron.log 2>&1
# STIC scraper - afternoon GPU-only run (fires 1:55pm, adds 0-10min random delay Mon-Fri)
55 13 * * 1-5 TZ=Europe/London python3 /opt/openclaw/data/stic/stic_scraper.py --gpus >> /opt/openclaw/logs/stic_cron.log 2>&1
# Travel watchlist check - twice daily
0 8,18 * * * /usr/bin/python3 /opt/openclaw/data/travel/check_watchlist.py >> /opt/openclaw/logs/travel.log 2>&1
# Retailer tracker - batch 1 (7:00pm UK daily)
0 19 * * * TZ=Europe/London /usr/bin/python3 /opt/openclaw/data/stic/retailer_scraper.py --batch 1 >> /opt/openclaw/logs/retailer_cron.log 2>&1
# Retailer tracker - batch 2 (8:30pm UK daily)
30 20 * * * TZ=Europe/London /usr/bin/python3 /opt/openclaw/data/stic/retailer_scraper.py --batch 2 >> /opt/openclaw/logs/retailer_cron.log 2>&1
# Retailer tracker - batch 3 (10:00pm UK daily + Telegram notification)
0 22 * * * TZ=Europe/London /usr/bin/python3 /opt/openclaw/data/stic/retailer_scraper.py --batch 3 >> /opt/openclaw/logs/retailer_cron.log 2>&1
# Nightly git sync of source files to GitHub (2am)
0 2 * * * /opt/openclaw/scripts/git-sync.sh >> /opt/openclaw/logs/git-sync.log 2>&1
# STIC template sync: DISABLED - products table now managed via portal Import/Export tools
# 0 0 * * * TZ=Europe/London python3 /opt/openclaw/scripts/sync_template.py >> /opt/openclaw/logs/sync_template.log 2>&1
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

- **prices.db WAL mode** — after restore, run `sqlite3 /opt/openclaw/data/analytics/prices.db "PRAGMA wal_checkpoint(FULL);"` to ensure DB is clean
- **Portal port** — served on `:8090`, local network only
- **OpenClaw container ports** — 18789 (main), 3000 (internal), 2099 (manifest public)
- **TrueNAS backup path** — `/mnt/Deep/backups/openclaw/`
- **GitHub repo** — `jackd71-ops/syndrome-open-claw`
- Update this document whenever cron jobs, services, or packages change

---

## Products Table

The `products` table in `prices.db` is the operational SKU catalogue — the scraper and portal read from it directly. It is seeded and kept in sync by `scripts/sync_template.py` (nightly, midnight UK). On a fresh rebuild, seed it manually before running the scraper:

```bash
python3 /opt/openclaw/scripts/sync_template.py
```

Schema:

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

EOL flag is managed via the portal Import/Export → Update EOL Status tool or the ⛔ EOL view. The nightly sync flushes EOL changes back to the OneDrive Excel.

---

## Portal Import / Export

The portal (`/api/import/…`, `/api/export/…`) provides CSV-based tools for managing the products table without touching Excel directly. Current tools:

| Tool | Endpoint | Description |
|---|---|---|
| Add / Update SKUs | `POST /api/import/new-skus/preview` + `/confirm` | Upsert products; does not touch EOL |
| Update EOL Status | `POST /api/import/eol-status/preview` + `/confirm` | Bulk-set EOL flag from product status data |
| Export Active SKUs | `GET /api/export/skus` | CSV download of all non-EOL products |
| Template download | `GET /api/import/template/<tool-id>` | Pre-formatted CSV template with correct headers |

CSV column named `Product` is the VIP product code (maps to `product_id` in DB). Both tools also accept `product_id` as the column name.

---

## Scraper Groups

STIC scraper runs as 9 manufacturer/product-group segments instead of batches. Each group sends its own Telegram on completion.

| Label | Manufacturer | Group |
|---|---|---|
| Palit GPU | PALIT | PROD_VIDEO |
| PowerColor GPU | POWERCOLOR | PROD_VIDEO |
| MSI GPU | MSI | PROD_VIDEO |
| ASUS GPU | ASUS | PROD_VIDEO |
| Gigabyte GPU | GIGABYTE | PROD_VIDEO |
| MSI Motherboards | MSI | PROD_MBRD |
| Gigabyte Motherboards | GIGABYTE | PROD_MBRD |
| ASUS Motherboards | ASUS | PROD_MBRD |
| Server / Pro | (all) | PROD_MBRDS |

**CLI:**
```bash
python3 stic_scraper.py --runall          # all groups, random 0–10 min start delay
python3 stic_scraper.py --gpus            # GPU groups only
python3 stic_scraper.py --group "ASUS GPU"  # single named group, no delay
python3 stic_scraper.py --rescrape 123456,234567  # re-scrape specific VIP codes
```

Groups can also be triggered from the portal: STIC → Scraper → Refresh SKUs.
