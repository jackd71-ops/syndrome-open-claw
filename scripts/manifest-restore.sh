#!/bin/bash
DB="/opt/openclaw/config/manifest/manifest.db"
BACKUP="/opt/openclaw/scripts/manifest-providers-backup.sql"

echo "Checking current user_providers..."
COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM user_providers;" 2>/dev/null || echo "0")
echo "Current rows: $COUNT"

if [ "$COUNT" -gt "0" ]; then
    echo "user_providers already has data — no restore needed."
    if [ "$1" != "--force" ]; then exit 0; fi
    echo "Force flag set — clearing and restoring..."
    sqlite3 "$DB" "DELETE FROM user_providers;"
fi

echo "Restoring from backup..."
sqlite3 "$DB" < "$BACKUP"

echo "Verifying..."
sqlite3 "$DB" \
  "SELECT provider, auth_type, is_active, connected_at FROM user_providers;"
echo "Done."
