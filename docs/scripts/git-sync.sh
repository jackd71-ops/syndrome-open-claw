#!/bin/bash
# Nightly sync of OpenClaw source files to GitHub.
# No secrets are committed — all credentials live in secrets.json (not tracked).

DOCS="/opt/openclaw/docs"
LIVE_SCRIPTS="/opt/openclaw/scripts"
LOG="/opt/openclaw/logs/git-sync.log"
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

echo "[$TIMESTAMP] Starting openclaw git sync..." >> "$LOG"

# ── Copy live source files into the OpenClaw docs repo ───────────────────────
cp "$LIVE_SCRIPTS/backup.sh"       "$DOCS/scripts/backup.sh"
cp "$LIVE_SCRIPTS/git-sync.sh"     "$DOCS/scripts/git-sync.sh"
cp "$LIVE_SCRIPTS/safe-restart.sh" "$DOCS/scripts/safe-restart.sh"

# ── Commit and push ───────────────────────────────────────────────────────────
cd "$DOCS" || { echo "[$TIMESTAMP] ERROR: could not cd to $DOCS" >> "$LOG"; exit 1; }

git add scripts/backup.sh \
        scripts/git-sync.sh \
        scripts/safe-restart.sh \
        REBUILD.md

if ! git diff --cached --quiet; then
    CHANGED=$(git diff --cached --name-only | tr '\n' ' ')
    git commit -m "Auto-sync: $(date +%Y-%m-%d) — ${CHANGED}"
    if git push >> "$LOG" 2>&1; then
        echo "[$TIMESTAMP] Push OK. Files: $CHANGED" >> "$LOG"
    else
        echo "[$TIMESTAMP] Push FAILED." >> "$LOG"
    fi
else
    echo "[$TIMESTAMP] No changes to commit." >> "$LOG"
fi
