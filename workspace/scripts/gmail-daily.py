#!/usr/bin/env python3
"""
Gmail daily processor for Kevin.
- Fetches inbox emails (in batches to avoid Cloudflare limits)
- Applies labels based on sender/subject rules
- Archives labeled emails
- Tracks promo senders for weekly report
- Returns a structured summary for the daily briefing
"""

import subprocess, json, os, sys, re
from datetime import datetime, timezone

API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
ENTITY_ID = "default"
BASE_URL = "https://backend.composio.dev/api/v3/tools/execute"

# ── Label IDs ─────────────────────────────────────────────────────────────────
LABEL_PROMOS          = "Label_39"
LABEL_ONLINE_ORDERS   = "Label_31"
LABEL_SHIPPED         = "Label_8108153258188589479"
LABEL_DELIVERED       = "Label_43"
LABEL_INTERESTS_WATCH = "Label_44"
LABEL_PERSONAL_FIN    = "Label_3002147784474582387"
LABEL_BANKING         = "Label_8139765542677525540"
LABEL_FINANCE_ADVICE  = "Label_47"
LABEL_UTILITIES       = "Label_38"
LABEL_HOLIDAYS        = "Label_30"
LABEL_HEALTH          = "Label_46"

# ── Security filter (hard block — never process these) ───────────────────────
BLOCKED_SUBJECTS = [
    "otp", "one-time password", "2fa", "two-factor",
    "verify your email", "confirm your email", "password reset",
    "sign-in attempt", "new login", "security alert", "unusual activity"
]
BLOCKED_SENDERS = [
    "security@", "verify@", "noreply@accounts.", "noreply@auth.",
    "no-reply@accounts."
]

# ── Routing rules (checked in order, first match wins) ───────────────────────
RULES = [
    # Amazon — order state detection via subject
    (["shipment-tracking@amazon"],    ["delivered"],                  LABEL_DELIVERED,       "delivered",      True),
    (["shipment-tracking@amazon"],    ["out for delivery"],            LABEL_SHIPPED,         "delivery_today", True),
    (["shipment-tracking@amazon"],    ["dispatched", "on its way"],   LABEL_SHIPPED,         "shipped",        True),
    (["order-update@amazon"],         ["delivered"],                  LABEL_DELIVERED,       "delivered",      True),
    (["order-update@amazon"],         ["out for delivery"],            LABEL_SHIPPED,         "delivery_today", True),
    (["order-update@amazon"],         ["dispatched", "shipped"],      LABEL_SHIPPED,         "shipped",        True),
    (["order-update@amazon",
      "marketplace-messages@amazon",
      "auto-confirm@amazon",
      "return@amazon"],               [],                             LABEL_ONLINE_ORDERS,   "ordered",        True),

    # SHEIN
    (["sheinnotice", "shein@"],       ["delivered"],                  LABEL_DELIVERED,       "delivered",      True),
    (["sheinnotice", "shein@"],       ["shipped", "dispatched"],      LABEL_SHIPPED,         "shipped",        True),
    (["sheinnotice", "shein@"],       [],                             LABEL_ONLINE_ORDERS,   "ordered",        True),

    # AliExpress
    (["aliexpress@notice"],           ["delivered"],                  LABEL_DELIVERED,       "delivered",      True),
    (["aliexpress@notice"],           ["shipped"],                    LABEL_SHIPPED,         "shipped",        True),
    (["aliexpress@notice"],           [],                             LABEL_ONLINE_ORDERS,   "ordered",        True),

    # Ashcroft Pharmacy — treat like Amazon
    (["ashcroft"],                    ["delivered"],                  LABEL_DELIVERED,       "delivered",      True),
    (["ashcroft"],                    ["out for delivery"],            LABEL_SHIPPED,         "delivery_today", True),
    (["ashcroft"],                    ["dispatched", "shipped",
                                       "on its way"],                 LABEL_SHIPPED,         "shipped",        True),
    (["ashcroft"],                    [],                             LABEL_HEALTH,           "health",         True),

    # Watches
    (["chrono24"],                    [],                             LABEL_INTERESTS_WATCH, "watches",        True),

    # Banking
    (["starling", "email.starling"],  [],                             LABEL_BANKING,         "finance",        True),

    # Personal Finance
    (["experian"],                    [],                             LABEL_PERSONAL_FIN,    "finance",        True),
    (["hargreaveslansdown",
      "hl.co.uk",
      "email@vantage.h-l.c"],         [],                             LABEL_PERSONAL_FIN,    "finance",        True),

    # Finance Advice
    (["msemoneytips", "lloydsbank"],  [],                             LABEL_FINANCE_ADVICE,  "finance",        True),

    # Holidays & Travel
    (["holidayextras", "holiday-extras", "skyscanner", "booking.com",
      "airbnb", "easyjet", "ryanair", "travelodge", "e.premierinn",
      "premierinn.com", "hotels.com", "pocruises", "virgin experience",
      "groupon", "hollywoodbowl", "riu class"],
                                      [],                             LABEL_HOLIDAYS,        "travel",         True),

    # Utilities
    (["unitedutilities", "united-utilities",
      "no-reply@unitedutilities"],    [],                             LABEL_UTILITIES,       "utilities",      True),

    # Promos (catch-all — add new senders here)
    (["qwertee", "moonpig", "dewalt@mails", "radisson", "netdata",
      "macdonaldhotels", "gog.com", "snapfish", "nextdoor", "secretlab",
      "vax.co.uk", "cworks.co.uk", "kaercher", "msi.marketing", "eset.com",
      "affinity", "cinemasociety", "youmaynothavehear", "aliexpress@",
      "jdwetherspoon", "facebookmail", "goodreads", "news.eufylife", "eufy",
      "quora", "doorwaytovalue", "hawkstone", "procook", "sharkninja",
      "bostonseeds", "claude.com", "hello.mcdonalds", "no-reply@hello.mc",
      "mcdonald", "thermomix", "rac.co.uk", "thevillagebutchers",
      "petdrugsonline", "grouponmail", "samsung.com", "nationalnumbers",
      "paypal", "anthropic", "no-reply@email.claude", "pocruises",
      "hollywoodbowl", "riu", "virgin experience", "hotels.com"],
     [],                                                              LABEL_PROMOS,          "promos",         True),
]


def composio(action, args):
    cmd = [
        "curl", "-s", "-X", "POST",
        f"{BASE_URL}/{action}",
        "-H", f"x-api-key: {API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"arguments": args, "entity_id": ENTITY_ID})
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)


def is_blocked(sender, subject):
    s = sender.lower()
    subj = subject.lower()
    return any(b in s for b in BLOCKED_SENDERS) or any(b in subj for b in BLOCKED_SUBJECTS)


def match_rule(sender, subject):
    s = sender.lower()
    subj = subject.lower()
    for sender_kws, subj_kws, label_id, category, archive in RULES:
        if not any(kw in s for kw in sender_kws):
            continue
        if subj_kws and not any(kw in subj for kw in subj_kws):
            continue
        return label_id, category, archive
    return None, None, False


def apply_label(msg_id, add_labels, remove_labels=None):
    return composio("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": msg_id,
        "add_label_ids": add_labels,
        "remove_label_ids": remove_labels or []
    })


def record_promo_senders(senders):
    """Write promo senders to tracker file."""
    try:
        tracker_path = os.path.expanduser(
            "~/.openclaw/workspace/memory/promo-senders.json"
        )
        from datetime import date, timedelta

        today = date.today()

        # Load or init
        if os.path.exists(tracker_path):
            with open(tracker_path) as f:
                data = json.load(f)
            period_end = date.fromisoformat(data.get("period_end", "2000-01-01"))
            if today > period_end:
                # New period
                data = {
                    "period_start": today.isoformat(),
                    "period_end": (today + timedelta(days=91)).isoformat(),
                    "senders": {}
                }
        else:
            os.makedirs(os.path.dirname(tracker_path), exist_ok=True)
            data = {
                "period_start": today.isoformat(),
                "period_end": (today + timedelta(days=91)).isoformat(),
                "senders": {}
            }

        # Increment counts
        for sender_str in senders:
            match = re.search(r'<([^>]+)>', sender_str)
            email = match.group(1).lower() if match else sender_str.lower().strip()
            name_match = re.match(r'^"?([^"<]+)"?\s*<', sender_str)
            name = name_match.group(1).strip().strip('"') if name_match else email.split('@')[0]

            if email not in data["senders"]:
                data["senders"][email] = {"name": name, "count": 0, "last_seen": today.isoformat()}
            data["senders"][email]["count"] += 1
            data["senders"][email]["last_seen"] = today.isoformat()
            data["senders"][email]["name"] = name

        with open(tracker_path, "w") as f:
            json.dump(data, f, indent=2)

    except Exception as e:
        pass  # Don't fail the main job if tracking breaks


def main():
    summary = {
        "delivery_today": [],
        "shipped":        [],
        "delivered":      [],
        "ordered":        [],
        "watches":        [],
        "finance":        [],
        "utilities":      [],
        "travel":         [],
        "health":         [],
        "promos":         [],
        "filtered":       [],
        "unlabeled":      [],
        "errors":         []
    }

    seen_ids = set()
    promo_senders = []

    for _ in range(4):  # up to 4 batches of 15 = 60 emails max
        resp = composio("GMAIL_FETCH_EMAILS", {
            "max_results": 15,
            "label_ids": ["INBOX"],
            "include_spam_trash": False
        })
        messages = resp.get("data", {}).get("messages", [])
        new_msgs = [m for m in messages if m.get("messageId", "") not in seen_ids]
        if not new_msgs:
            break

        for m in new_msgs:
            msg_id  = m.get("messageId", "")
            sender  = m.get("sender", "")
            subject = m.get("subject", "")
            seen_ids.add(msg_id)

            if is_blocked(sender, subject):
                summary["filtered"].append(subject[:50])
                continue

            label_id, category, archive = match_rule(sender, subject)

            if label_id:
                remove = ["INBOX"] if archive else []
                r = apply_label(msg_id, [label_id], remove)
                if r.get("successful"):
                    entry = {"subject": subject, "sender": sender}
                    summary.get(category, summary["unlabeled"]).append(entry)
                    # Track promo senders
                    if label_id == LABEL_PROMOS:
                        promo_senders.append(sender)
                else:
                    summary["errors"].append(f"{subject[:40]}: {r.get('error', '?')}")
            else:
                summary["unlabeled"].append({"subject": subject, "sender": sender})

    # Record promo senders for weekly report
    if promo_senders:
        record_promo_senders(promo_senders)

    return summary


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
