#!/bin/bash
# Nightly sync of OpenClaw source files to GitHub.
# Git root: /opt/openclaw — all source files tracked directly.
# No secrets are committed — .env, .env.secrets, secrets.json are in .gitignore.

REPO="/opt/openclaw"
LOG="$REPO/logs/git-sync.log"
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

echo "[$TIMESTAMP] Starting openclaw git sync..." >> "$LOG"

git -C "$REPO" add -A

if ! git -C "$REPO" diff --cached --quiet; then
    CHANGED=$(git -C "$REPO" diff --cached --name-only | tr '\n' ' ')
    git -C "$REPO" commit -m "Auto-sync: $(date +%Y-%m-%d) — ${CHANGED}"
    if git -C "$REPO" push >> "$LOG" 2>&1; then
        echo "[$TIMESTAMP] Push OK. Files: $CHANGED" >> "$LOG"
    else
        echo "[$TIMESTAMP] Push FAILED." >> "$LOG"
    fi
else
    echo "[$TIMESTAMP] No changes to commit." >> "$LOG"
fi
