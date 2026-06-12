#!/usr/bin/env python3
"""
OpenClaw job monitor — runs at 00:05 daily via cubi crontab.
Checks which jobs were scheduled to run yesterday and whether they delivered.
Success is confirmed by a status file written by the script after Telegram send.
Sends a single Telegram summary of any failures or missed runs.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from croniter import croniter

JOBS_PATH = "/opt/openclaw/config/cron/jobs.json"
SECRETS_PATH = "/opt/openclaw/secrets.json"
STATUS_DIR = "/opt/openclaw/workspace/data/job-status"
TELEGRAM_CHAT_ID = "1163684840"

# Jobs running every 60 mins or less — checked by consecutiveErrors only
FREQUENT_JOB_THRESHOLD = 60


def get_telegram_token():
    try:
        return json.load(open(SECRETS_PATH))["TELEGRAM_TOKEN"]
    except Exception as e:
        print(f"Failed to read Telegram token: {e}", file=sys.stderr)
        sys.exit(1)


def send_telegram(token, message):
    import urllib.request
    body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def cron_interval_minutes(expr):
    """Return the minimum interval in minutes between fires for a cron expression."""
    try:
        tz = ZoneInfo("Europe/London")
        base = datetime(2026, 1, 1, 0, 0, tzinfo=tz)
        c = croniter(expr, base)
        first = c.get_next(datetime)
        second = c.get_next(datetime)
        return int((second - first).total_seconds() / 60)
    except Exception:
        return 1440  # assume daily if we can't parse


def was_scheduled_yesterday(expr, tz_name):
    """Return True if the cron expression fired at least once yesterday."""
    try:
        tz = ZoneInfo(tz_name or "Europe/London")
        yesterday_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        yesterday_end = yesterday_start + timedelta(days=1)
        c = croniter(expr, yesterday_start - timedelta(seconds=1))
        next_fire = c.get_next(datetime)
        return next_fire < yesterday_end
    except Exception:
        return False


def check_status_file(job_id, yesterday_start, yesterday_end):
    """
    Check the job's status file for confirmed delivery within yesterday's window.
    Returns (ok: bool, reason: str)
    """
    path = os.path.join(STATUS_DIR, f"{job_id}.json")
    if not os.path.exists(path):
        return False, "No delivery confirmation (status file missing — script never wrote success)"

    try:
        with open(path) as f:
            data = json.load(f)
        completed_at = datetime.fromisoformat(data["completed_at"])
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=ZoneInfo("UTC"))
        tz = ZoneInfo("Europe/London")
        completed_at_local = completed_at.astimezone(tz)
        if yesterday_start <= completed_at_local < yesterday_end:
            return True, ""
        else:
            return False, f"Last confirmed delivery: {completed_at_local.strftime('%a %d %b %H:%M')} — not yesterday"
    except Exception as e:
        return False, f"Status file unreadable: {e}"


def check_jobs():
    try:
        data = json.load(open(JOBS_PATH))
    except json.JSONDecodeError as e:
        return None, f"Could not read jobs.json: {e}"
    except FileNotFoundError:
        return None, "jobs.json not found"

    tz = ZoneInfo("Europe/London")
    now = datetime.now(tz)
    yesterday_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    yesterday_end = yesterday_start + timedelta(days=1)

    issues = []

    for job in data.get("jobs", []):
        if not job.get("enabled", True):
            continue

        name = job["name"]
        job_id = job["id"]
        expr = job["schedule"]["expr"]
        tz_name = job["schedule"].get("tz", "Europe/London")
        state = job.get("state", {})
        consecutive_errors = state.get("consecutiveErrors", 0)
        last_error = state.get("lastError", "")

        interval_mins = cron_interval_minutes(expr)
        is_frequent = interval_mins <= FREQUENT_JOB_THRESHOLD

        # Always flag active consecutive errors regardless of schedule
        if consecutive_errors > 0:
            issues.append(
                f"⚠️ <b>{name}</b>\n"
                f"   {consecutive_errors} consecutive error(s)\n"
                f"   {last_error[:120] if last_error else 'unknown error'}"
            )
            continue

        if is_frequent:
            # Hourly/sub-hourly: check status file was written in last 2 hours
            path = os.path.join(STATUS_DIR, f"{job_id}.json")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data_s = json.load(f)
                    completed_at = datetime.fromisoformat(data_s["completed_at"])
                    if completed_at.tzinfo is None:
                        completed_at = completed_at.replace(tzinfo=ZoneInfo("UTC"))
                    age_hours = (datetime.now(ZoneInfo("UTC")) - completed_at).total_seconds() / 3600
                    if age_hours > 2:
                        issues.append(
                            f"⚠️ <b>{name}</b>\n"
                            f"   Last confirmed run: {completed_at.astimezone(tz).strftime('%a %d %b %H:%M')} "
                            f"({age_hours:.0f}h ago)"
                        )
                except Exception as e:
                    issues.append(f"⚠️ <b>{name}</b>\n   Status file unreadable: {e}")
            # No status file for frequent jobs is not flagged here — consecutiveErrors covers hard failures
            continue

        # Daily/weekly: check if scheduled yesterday, then verify confirmed delivery
        if not was_scheduled_yesterday(expr, tz_name):
            continue

        ok, reason = check_status_file(job_id, yesterday_start, yesterday_end)
        if not ok:
            issues.append(f"❌ <b>{name}</b>\n   {reason}")

    return issues, None


def main():
    token = get_telegram_token()
    issues, read_error = check_jobs()

    if read_error:
        send_telegram(token, f"⚠️ <b>OpenClaw Job Monitor</b>\n\nFailed to read job state:\n{read_error}")
        sys.exit(1)

    tz = ZoneInfo("Europe/London")
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%A %d %b %Y")

    if not issues:
        send_telegram(token, f"✅ <b>OpenClaw Jobs — {yesterday}</b>\n\nAll jobs confirmed delivered.")
    else:
        body = "\n\n".join(issues)
        send_telegram(token, f"🔴 <b>OpenClaw Jobs — {yesterday}</b>\n\n{body}")


if __name__ == "__main__":
    main()
