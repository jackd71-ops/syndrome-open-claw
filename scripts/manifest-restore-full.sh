#!/bin/bash
# Restore Manifest DB from most recent backup
BACKUP_DIR="/opt/openclaw/config/manifest/backups"
LATEST=$(ls -t "$BACKUP_DIR"/*.db 2>/dev/null | head -1)

if [ -z "$LATEST" ]; then
    echo "No backup found in $BACKUP_DIR"
    exit 1
fi

echo "Restoring from: $LATEST"
cp "$LATEST" /opt/openclaw/config/manifest/manifest.db
echo "Restored. Restart OpenClaw to apply:"
echo "  docker restart openclaw"
