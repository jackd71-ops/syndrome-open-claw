#!/bin/bash
# Nightly sync of production source files to GitHub
# Copies live .py files and scripts into the docs repo and pushes if anything changed.
# No secrets are committed — all credentials live in secrets.json (not tracked).

DOCS="/opt/openclaw/docs"
LIVE_ANALYTICS="/opt/openclaw/data/analytics"
LIVE_STIC="/opt/openclaw/data/stic"
LIVE_SCRIPTS="/opt/openclaw/scripts"
LOG="/opt/openclaw/logs/git-sync.log"
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

echo "[$TIMESTAMP] Starting git sync..." >> "$LOG"

# ── Copy live source files into the docs repo ─────────────────────────────────
cp "$LIVE_ANALYTICS/portal.py"        "$DOCS/data/analytics/portal.py"
cp "$LIVE_STIC/stic_scraper.py"       "$DOCS/data/stic/stic_scraper.py"
cp "$LIVE_STIC/retailer_scraper.py"   "$DOCS/data/stic/retailer_scraper.py"
cp "$LIVE_STIC/scan_scrape.py"        "$DOCS/data/stic/scan_scrape.py"
cp "$LIVE_STIC/very_scrape.py"        "$DOCS/data/stic/very_scrape.py"
cp "$LIVE_STIC/box_scrape.py"         "$DOCS/data/stic/box_scrape.py"
cp "$LIVE_STIC/ocuk_scrape.py"        "$DOCS/data/stic/ocuk_scrape.py"
cp "$LIVE_SCRIPTS/backup.sh"          "$DOCS/scripts/backup.sh"
cp "$LIVE_SCRIPTS/git-sync.sh"        "$DOCS/scripts/git-sync.sh"
cp "$LIVE_SCRIPTS/safe-restart.sh"    "$DOCS/scripts/safe-restart.sh"

# ── Check if anything actually changed ───────────────────────────────────────
cd "$DOCS" || { echo "[$TIMESTAMP] ERROR: could not cd to $DOCS" >> "$LOG"; exit 1; }

if git diff --quiet && git diff --cached --quiet; then
    echo "[$TIMESTAMP] No changes — nothing to commit." >> "$LOG"
    exit 0
fi

# ── Commit and push ───────────────────────────────────────────────────────────
git add data/analytics/portal.py \
        data/stic/stic_scraper.py \
        data/stic/retailer_scraper.py \
        data/stic/scan_scrape.py \
        data/stic/very_scrape.py \
        data/stic/box_scrape.py \
        data/stic/ocuk_scrape.py \
        scripts/backup.sh \
        scripts/git-sync.sh \
        scripts/safe-restart.sh

CHANGED=$(git diff --cached --name-only | tr '\n' ' ')
git commit -m "Auto-sync: $(date +%Y-%m-%d) — ${CHANGED}"

if git push >> "$LOG" 2>&1; then
    echo "[$TIMESTAMP] Push OK. Files: $CHANGED" >> "$LOG"
else
    echo "[$TIMESTAMP] Push FAILED." >> "$LOG"
    exit 1
fi
