#!/bin/bash
# Daily backup of OpenClaw config and data to TrueNAS

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG="/opt/openclaw/logs/backup.log"

echo "[$TIMESTAMP] Starting backup..." >> "$LOG"

# OpenClaw (AI assistant, config, workspace)
rsync -av --delete \
  -e "ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw -o BatchMode=yes" \
  /opt/openclaw/config/ \
  /opt/openclaw/workspace/ \
  /opt/openclaw/workspace-family/ \
  /opt/openclaw/workspace-sales/ \
  /opt/openclaw/data/ \
  /opt/openclaw/scripts/ \
  /opt/openclaw/ha-config/ \
  /opt/openclaw/secrets.json \
  /opt/openclaw/docker-compose.yml \
  /opt/openclaw/Dockerfile \
  truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw/ \
  >> "$LOG" 2>&1

MAIN_OK=$?

# STIC scraper + portal (standalone, separate from OpenClaw)
rsync -av --delete \
  -e "ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw -o BatchMode=yes" \
  /opt/stic-scraper/scraper/ \
  /opt/stic-scraper/analytics/ \
  /opt/stic-scraper/data/ \
  /opt/stic-scraper/general/ \
  /opt/stic-scraper/docs/ \
  /opt/stic-scraper/secrets.json \
  truenas_admin@192.168.1.158:/mnt/Deep/backups/stic-scraper/ \
  >> "$LOG" 2>&1

STIC_OK=$?

# Backup rclone config (OneDrive OAuth token)
rsync -av \
  -e "ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw -o BatchMode=yes" \
  /home/adminclaude/.config/rclone/ \
  truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw/rclone-config/ \
  >> "$LOG" 2>&1

RCLONE_OK=$?

if [ $MAIN_OK -eq 0 ] && [ $STIC_OK -eq 0 ] && [ $RCLONE_OK -eq 0 ]; then
  echo "[$TIMESTAMP] Backup complete." >> "$LOG"
else
  echo "[$TIMESTAMP] Backup FAILED (openclaw=$MAIN_OK stic=$STIC_OK rclone=$RCLONE_OK)." >> "$LOG"
fi
