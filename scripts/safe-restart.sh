#!/bin/bash
# Safe restart — always backs up Manifest DB first
echo "Backing up Manifest DB before restart..."
bash /opt/openclaw/scripts/manifest-backup-full.sh

echo "Restarting OpenClaw container..."
cd /opt/openclaw && sudo docker compose up -d --force-recreate

echo "Waiting for container to start..."
sleep 15

echo "Patching Manifest synchronize flag..."
for DIST_FILE in $(docker exec openclaw find /home/node/.openclaw/extensions/manifest/dist -name "*.js" 2>/dev/null | xargs grep -l "synchronize:!0\|synchronize: true" 2>/dev/null); do
    docker exec openclaw sed -i 's/synchronize:!0/synchronize:!1/g; s/synchronize: true/synchronize: false/g' "$DIST_FILE"
    echo "Patch applied to: $DIST_FILE"
done

echo "Done. If Manifest DB was wiped, restore with:"
echo "  bash /opt/openclaw/scripts/manifest-restore-full.sh"
