#!/bin/bash
# Full Manifest DB backup — run before any container restart or plugin update
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB="/opt/openclaw/config/manifest/manifest.db"
BACKUP_DIR="/opt/openclaw/config/manifest/backups"

mkdir -p "$BACKUP_DIR"

# Full DB backup
cp "$DB" "$BACKUP_DIR/manifest-$TIMESTAMP.db"

# SQL dump of all critical tables
sqlite3 "$DB" ".dump user_providers" > "$BACKUP_DIR/providers-$TIMESTAMP.sql"
sqlite3 "$DB" ".dump agents" >> "$BACKUP_DIR/providers-$TIMESTAMP.sql"
sqlite3 "$DB" ".dump agent_api_keys" >> "$BACKUP_DIR/providers-$TIMESTAMP.sql"
sqlite3 "$DB" ".dump tier_assignments" >> "$BACKUP_DIR/providers-$TIMESTAMP.sql"
sqlite3 "$DB" ".dump tenants" >> "$BACKUP_DIR/providers-$TIMESTAMP.sql"

# Keep only last 5 backups
ls -t "$BACKUP_DIR"/*.db 2>/dev/null | tail -n +6 | xargs rm -f
ls -t "$BACKUP_DIR"/*.sql 2>/dev/null | tail -n +6 | xargs rm -f

echo "[$TIMESTAMP] Manifest backup complete: $BACKUP_DIR/manifest-$TIMESTAMP.db"
