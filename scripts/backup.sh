#!/bin/bash
# Daily backup of OpenClaw config and data to TrueNAS.
# Each source directory is backed up to its own subdirectory on TrueNAS
# so restore is clean — no flat merging.

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG="/opt/openclaw/logs/backup.log"
SSH_KEY="/home/adminclaude/.ssh/id_ed25519_openclaw"
DEST="truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw"

_rsync() {
    rsync -av -e "ssh -i $SSH_KEY -o BatchMode=yes" "$@" >> "$LOG" 2>&1
}

echo "[$TIMESTAMP] Starting openclaw backup..." >> "$LOG"

# Directories — each backed up to its own named subdirectory
_rsync /opt/openclaw/config/           "$DEST/config/"
_rsync /opt/openclaw/workspace/        "$DEST/workspace/"
_rsync /opt/openclaw/workspace-family/ "$DEST/workspace-family/"
_rsync /opt/openclaw/workspace-sales/  "$DEST/workspace-sales/"
_rsync /opt/openclaw/data/             "$DEST/data/"
_rsync /opt/openclaw/scripts/          "$DEST/scripts/"
_rsync /opt/openclaw/ha-config/        "$DEST/ha-config/"

# Individual files (secrets + config) — root of backup
_rsync \
  /opt/openclaw/secrets.json \
  /opt/openclaw/.env \
  /opt/openclaw/.env.secrets \
  /opt/openclaw/docker-compose.yml \
  /opt/openclaw/Dockerfile \
  "$DEST/"

MAIN_OK=$?

# Backup rclone config (OneDrive OAuth token)
_rsync /home/adminclaude/.config/rclone/ \
  "truenas_admin@192.168.1.158:/mnt/Deep/backups/openclaw/rclone-config/"
RCLONE_OK=$?

if [ $MAIN_OK -eq 0 ] && [ $RCLONE_OK -eq 0 ]; then
    echo "[$TIMESTAMP] Backup complete." >> "$LOG"
else
    echo "[$TIMESTAMP] Backup FAILED (openclaw=$MAIN_OK rclone=$RCLONE_OK)." >> "$LOG"
fi
