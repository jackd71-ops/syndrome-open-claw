#!/usr/bin/env python3
"""
Promo sender tracker for OpenClaw Gmail archive job.
Tracks senders archived to Promo labels over 3-month periods.
Usage:
  --record --sender "email@example.com" --name "Display Name"
  --report
"""
import json
import sys
import os
import argparse
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

TRACKING_FILE = os.path.expanduser("~/.openclaw/workspace/memory/promo-senders.json")
SECRETS_FILE = "/opt/openclaw/secrets.json"

def load_secrets():
    with open(SECRETS_FILE) as f:
        return json.load(f)

def send_telegram(message):
    import subprocess
    secrets = load_secrets()
    token = secrets["TELEGRAM_TOKEN"]
    chat_id = "1163684840"
    cmd = [
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})
    ]
    subprocess.run(cmd, capture_output=True)

def load_tracking():
    os.makedirs(os.path.dirname(TRACKING_FILE), exist_ok=True)
    if not os.path.exists(TRACKING_FILE):
        return new_period()
    with open(TRACKING_FILE) as f:
        return json.load(f)

def save_tracking(data):
    with open(TRACKING_FILE, "w") as f:
        json.dump(data, f, indent=2)

def new_period():
    today = date.today()
    end = today + relativedelta(months=3)
    return {
        "period_start": today.isoformat(),
        "period_end": end.isoformat(),
        "senders": {}
    }

def check_reset(data):
    if date.today() > date.fromisoformat(data["period_end"]):
        print("3-month period ended — resetting tracker")
        return new_period()
    return data

def record(sender_email, sender_name):
    data = load_tracking()
    data = check_reset(data)
    email = sender_email.lower().strip()
    if email not in data["senders"]:
        data["senders"][email] = {"name": sender_name, "count": 0, "last_seen": None}
    data["senders"][email]["count"] += 1
    data["senders"][email]["last_seen"] = date.today().isoformat()
    if sender_name and sender_name != email:
        data["senders"][email]["name"] = sender_name
    save_tracking(data)
    print(f"Recorded: {email} ({data['senders'][email]['count']} total)")

def report():
    data = load_tracking()
    data = check_reset(data)
    save_tracking(data)

    senders = data["senders"]
    if not senders:
        send_telegram("📧 *Promo Sender Report*\nNo promo senders tracked yet this period.")
        return

    sorted_senders = sorted(senders.items(), key=lambda x: x[1]["count"], reverse=True)[:10]

    period_start = date.fromisoformat(data["period_start"]).strftime("%d %b %Y")
    period_end = date.fromisoformat(data["period_end"]).strftime("%d %b %Y")

    lines = [f"📧 *Top Promo Senders*", f"_{period_start} — {period_end}_", ""]
    for i, (email, info) in enumerate(sorted_senders, 1):
        name = info["name"] if info["name"] != email else email
        lines.append(f"{i}. {name} — {info['count']} emails")

    lines.append("")
    lines.append("_Consider unsubscribing from the top senders._")

    message = "\n".join(lines)
    send_telegram(message)
    print("Report sent to Telegram")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--sender", default="")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    if args.record:
        record(args.sender, args.name)
    elif args.report:
        report()
    else:
        print("Usage: --record --sender email --name name | --report")
