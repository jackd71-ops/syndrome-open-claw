#!/bin/bash
# Daily backup of OpenClaw config and data to TrueNAS

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG="/opt/openclaw/logs/backup.log"

echo "[$TIMESTAMP] Starting backup..." >> "$LOG"

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

# Backup rclone config (OneDrive OAuth token) into a subdir of the same backup path
rsync -av \
  -e "ssh -i /home/adminclaude/.ssh/id_ed25519_openclaw -o BatchMode=yes" \
  /home/adminclaude/.config/rclone/ \
  truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw/rclone-config/ \
  >> "$LOG" 2>&1

RCLONE_OK=$?

if [ $MAIN_OK -eq 0 ] && [ $RCLONE_OK -eq 0 ]; then
  echo "[$TIMESTAMP] Backup complete." >> "$LOG"
else
  echo "[$TIMESTAMP] Backup FAILED (main=$MAIN_OK rclone=$RCLONE_OK)." >> "$LOG"
fi
