---
name: truenas-mgmt
description: Manage TrueNAS at 192.168.1.158 via SSH and REST API. Use when checking pool health, disk status, snapshots, dataset usage, or verifying backup success. Connects via SSH key; read-only by default unless user explicitly requests changes.
metadata: {"clawdbot":{"emoji":"💾","requires":{"bins":["ssh","curl"]}}}
---

# TrueNAS Management

Access and monitor the Jackson household NAS (TrueNAS SCALE at 192.168.1.158).

## Connection Details

- **Host:** 192.168.1.158
- **User:** truenas_admin
- **SSH Key:** ~/.openclaw/ssh/id_ed25519_openclaw
- **API Base:** https://192.168.1.158/api/v2.0

## Safety Rules

- **READ-ONLY by default** — never delete, overwrite, or modify data without explicit user instruction
- Always confirm before any write/destructive operation
- Treat backup data as sacred — never touch it without confirmation

## SSH Access

```bash
ssh -i ~/.openclaw/ssh/id_ed25519_openclaw -o StrictHostKeyChecking=no truenas_admin@192.168.1.158
```

## Common Operations

### Pool and Disk Health
```bash
# Check pool status
ssh -i ~/.openclaw/ssh/id_ed25519_openclaw truenas_admin@192.168.1.158 "zpool status"

# Check disk usage
ssh -i ~/.openclaw/ssh/id_ed25519_openclaw truenas_admin@192.168.1.158 "zfs list -t all"
```

### Backup Verification
```bash
# List recent backups
ssh -i ~/.openclaw/ssh/id_ed25519_openclaw truenas_admin@192.168.1.158 "ls -lht /mnt/Deep/backups/openclaw/ | head -10"

# Check last backup size
ssh -i ~/.openclaw/ssh/id_ed25519_openclaw truenas_admin@192.168.1.158 "du -sh /mnt/Deep/backups/openclaw/"
```

### Snapshots
```bash
# List snapshots
ssh -i ~/.openclaw/ssh/id_ed25519_openclaw truenas_admin@192.168.1.158 "zfs list -t snapshot | grep Deep"
```

### REST API (read-only queries)
```bash
TRUENAS_KEY="${TRUENAS_KEY:-$(cat /home/node/.openclaw/secrets.json | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"TRUENAS_KEY\"])')}"
curl -sk -H "Authorization: Bearer $TRUENAS_KEY" https://192.168.1.158/api/v2.0/pool | python3 -m json.tool
```

## Key Paths on TrueNAS
- OpenClaw backups: `/mnt/Deep/backups/openclaw/`
- SSH key on host: `~/.openclaw/ssh/id_ed25519_openclaw`

## Status Checks

Ask for:
- Pool health (ONLINE / DEGRADED / FAULTED)
- Last successful backup (check rsync log on TrueNAS)
- Available space on Deep pool
- Any SMART errors or scrub warnings

## Notes
- TrueNAS API key is in secrets.json as `TRUENAS_KEY` (currently PLACEHOLDER — add real key to enable API access)
- SSH access works without the API key
- TrueNAS is at https (self-signed cert) — use `-k` with curl to skip cert verification
