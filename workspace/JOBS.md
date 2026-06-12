# OpenClaw Cron Jobs — Standards & Patterns

## Success Definition

**A job is only considered successful when Darren receives the intended Telegram message.**

OpenClaw's internal `lastRunStatus: "ok"` only means the agent turn completed without crashing — it does not confirm the task worked. The job monitor uses status files written by scripts to confirm real delivery.

---

## How Job Success Tracking Works

Each job's Python script writes a status file **only after the final Telegram send is confirmed** (HTTP 200 + `ok: true` from the Telegram API):

```
/home/node/.openclaw/workspace/data/job-status/<job-id>.json
```

On the host this maps to:
```
/opt/openclaw/workspace/data/job-status/<job-id>.json
```

The job monitor (`/opt/openclaw/scripts/job-monitor.py`) runs at 00:05 daily and checks each status file. If it's missing or stale (not written during yesterday's window), the job is flagged as failed in the morning Telegram report.

---

## Creating a New Job

### 1. Write the script

Every script that sends a Telegram message must:

1. **Check the API response** — use `urllib.request`, not `requests`. Confirm `ok: true`.
2. **Exit non-zero on failure** — if Telegram fails, `sys.exit(1)`.
3. **Write the status file** after confirmed send — use this function:

```python
import json, os
from datetime import datetime, timezone

JOB_ID = "your-job-uuid-here"  # must match jobs.json

def _write_job_status() -> None:
    path = f"/home/node/.openclaw/workspace/data/job-status/{JOB_ID}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "status": "ok",
            "job": "Your Job Name",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, f)
```

Call `_write_job_status()` only after you've confirmed Telegram returned `ok: true`.

For jobs where success = script ran (no Telegram send required, e.g. a data sync), write the status file at the end of `main()` after all work completes.

### 2. Load the Telegram token

Always load from `secrets.json`, not from env vars (container env does not have it set):

```python
def _load_telegram_token() -> str:
    for path in [
        os.path.expanduser("~/.openclaw/secrets.json"),
        "/opt/openclaw/secrets.json",
    ]:
        try:
            with open(path) as f:
                return json.load(f).get("TELEGRAM_TOKEN", "")
        except Exception:
            pass
    return ""
```

### 3. Send Telegram via urllib (not requests)

`requests` is not installed in the container Python env. Use `urllib.request`:

```python
import urllib.request

def _send_telegram(text: str) -> bool:
    token = _load_telegram_token()
    if not token:
        return False
    payload = json.dumps({
        "chat_id": "1163684840",
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
            return result.get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)
        return False
```

### 4. Write the job prompt

The prompt should:
- Run the script directly (not ask the agent to also send a Telegram message)
- Say "the script sends Telegram directly and writes a job status file on confirmed delivery"
- Say "if it exits non-zero, report the error — do not attempt to send anything yourself"

Example:
```
Run this command:
/opt/python/bin/python3 ~/.openclaw/workspace/skills/my-skill/my-script.py

The script performs X, sends the result to Telegram directly, and writes a
job status file on confirmed delivery. If it exits non-zero, report the error —
do not attempt to send anything yourself.
```

### 5. Register the job in jobs.json

Use the OpenClaw portal or edit `/opt/openclaw/config/cron/jobs.json` directly.

Required fields for all cron jobs:
```json
{
  "sessionTarget": "isolated",
  "agentId": "cron",
  "model": "manifest/auto"
}
```

The `id` (UUID) must match the `JOB_ID` constant in your script.

---

## Current Jobs

| Job | ID | Schedule | Success = |
|---|---|---|---|
| Daily Cost Summary | `cf60889b` | 05:00 daily | Telegram report sent |
| Budget Alert Check | `58570823` | Hourly | Script ran cleanly |
| Daily weather | `134bc9e9` | 06:00 daily | Telegram forecast sent |
| Gmail weekly archive | `c247ae9d` | 09:00 Mon | Telegram archive summary sent |
| Gmail morning briefing | `ac7f2a51` | 06:00 daily | Telegram briefing sent |
| Gmail evening briefing | `705de85a` | 19:00 daily | Telegram briefing sent |
| Travel watchlist | `f80d5beb` | 08:00 + 18:00 | Script ran cleanly |

---

## Job Monitor

**Script:** `/opt/openclaw/scripts/job-monitor.py`  
**Crontab:** `5 0 * * * /usr/bin/python3 /opt/openclaw/scripts/job-monitor.py >> /opt/openclaw/logs/job-monitor.log 2>&1`

**Logic:**
- `consecutiveErrors > 0` in jobs.json → always flag (hard crash/timeout)
- Frequent jobs (≤60 min): status file must have been written within the last 2 hours
- Daily/weekly jobs: status file `completed_at` must fall within yesterday's London-time window
- Missing status file = failure (delivery never confirmed)

**Status files location (host):** `/opt/openclaw/workspace/data/job-status/`
