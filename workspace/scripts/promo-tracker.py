#!/usr/bin/env python3
"""
Promo sender tracker for Kevin.
- Records every sender archived to Promos
- Resets every 3 months automatically
- Used by gmail-daily.py to log senders
- Used by weekly archive cron to generate top-10 report
"""

import json, os, re
from datetime import datetime, date, timedelta

TRACKER_PATH = os.path.expanduser(
    "~/.openclaw/workspace/memory/promo-senders.json"
)
PERIOD_DAYS = 91  # ~3 months


def load():
    """Load tracker file, auto-reset if period has expired."""
    today = date.today()
    new_period = False

    if os.path.exists(TRACKER_PATH):
        with open(TRACKER_PATH) as f:
            data = json.load(f)
        period_end = date.fromisoformat(data.get("period_end", "2000-01-01"))
        if today > period_end:
            data = _new_period(today)
            new_period = True
    else:
        data = _new_period(today)
        new_period = True

    return data, new_period


def _new_period(start: date) -> dict:
    end = start + timedelta(days=PERIOD_DAYS)
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "senders": {}
    }


def save(data: dict):
    os.makedirs(os.path.dirname(TRACKER_PATH), exist_ok=True)
    with open(TRACKER_PATH, "w") as f:
        json.dump(data, f, indent=2)


def extract_email(sender: str) -> tuple[str, str]:
    """Extract email address and display name from sender string."""
    match = re.search(r'<([^>]+)>', sender)
    email = match.group(1).lower() if match else sender.lower().strip()
    # Display name — everything before the <
    name_match = re.match(r'^"?([^"<]+)"?\s*<', sender)
    name = name_match.group(1).strip().strip('"') if name_match else email.split('@')[0]
    return email, name


def record_senders(senders: list[str]):
    """Increment count for each sender. Call with list of sender strings."""
    data, new_period = load()
    today = date.today().isoformat()

    for sender_str in senders:
        email, name = extract_email(sender_str)
        if email not in data["senders"]:
            data["senders"][email] = {"name": name, "count": 0, "last_seen": today}
        data["senders"][email]["count"] += 1
        data["senders"][email]["last_seen"] = today
        # Keep display name updated
        data["senders"][email]["name"] = name

    save(data)
    return new_period


def weekly_report() -> str:
    """Generate top-10 promo sender report for Monday briefing."""
    if not os.path.exists(TRACKER_PATH):
        return "📧 *Promo sender tracking* — No data yet (started this week)."

    data, new_period = load()
    if new_period:
        save(data)

    period_start = datetime.fromisoformat(data["period_start"]).strftime("%-d %b %Y")
    period_end   = datetime.fromisoformat(data["period_end"]).strftime("%-d %b %Y")
    senders = data.get("senders", {})

    if not senders:
        return f"📧 *Promo senders ({period_start} — {period_end})* — Nothing tracked yet."

    top10 = sorted(senders.items(), key=lambda x: x[1]["count"], reverse=True)[:10]

    lines = [f"📧 *Top promo senders ({period_start} — {period_end})*", ""]
    for i, (email, info) in enumerate(top10, 1):
        lines.append(f"  {i}. {info['name']} — {info['count']} emails")
    lines.append("")
    lines.append("_Consider unsubscribing from the top senders to save inbox clutter._")

    if new_period:
        lines.append("")
        lines.append("_ℹ️ New tracking period just started — counts reset._")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--report" in sys.argv:
        print(weekly_report())
    elif "--test" in sys.argv:
        # Test with dummy senders
        record_senders([
            '"Argos" <offers@argos.co.uk>',
            'AliExpress <aliexpress@notice.aliexpress.com>',
            '"Argos" <offers@argos.co.uk>',
        ])
        print("Recorded test senders.")
        print(weekly_report())
    else:
        print("Usage: promo-tracker.py --report | --test")
