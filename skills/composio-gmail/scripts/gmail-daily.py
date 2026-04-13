#!/usr/bin/env python3
"""
gmail-daily.py — Daily Gmail labelling, archiving, and promo tracking.
Fetches unread emails, applies labels, records promo senders, returns summary.

Usage: python3 gmail-daily.py [--since-hours 12]
"""
import subprocess
import json
import os
import sys
import argparse
from datetime import datetime, timezone

PROMO_TRACKER = os.path.expanduser(
    "~/.openclaw/workspace/skills/promo-tracker/promo-tracker.py")

BLOCKED_SUBJECTS = [
    'otp','one-time password','2fa','two-factor','verify your email',
    'confirm your email','password reset','sign-in attempt','new login',
    'security alert','unusual activity'
]
BLOCKED_SENDERS = ['security@','verify@','noreply@accounts.',
                   'noreply@auth.','no-reply@accounts.','jackd71@gmail.com']

# Label IDs
LABELS = {
    "Promos":           "Label_39",
    "Online Orders":    "Label_31",
    "Delivered":        "Label_43",
    "Watches":          "Label_44",
    "Shipped":          "Label_8108153258188589479",
    "Personal Finance": "Label_3002147784474582387",
    "Utilities":        "Label_38",
}

# Keyword rules — (subject keywords, sender keywords) -> label name
RULES = [
    # Delivered — check before Promos to avoid SHEIN misclassification
    (["delivered","your parcel has been delivered","delivery complete",
      "successfully delivered","out for delivery"],
     [],
     "Delivered"),

    # Shipped
    (["shipped","dispatched","on its way","your order is on the way",
      "tracking number","your tracking"],
     [],
     "Shipped"),

    # Online Orders
    (["order confirmed","order received","thank you for your order",
      "order #","your order","purchase confirmation","receipt for",
      "payment confirmation"],
     [],
     "Online Orders"),

    # Personal Finance
    (["dividend","portfolio","investment","isa","sipp","pension",
      "statement","transaction","mortgage","receipt","invoice",
      "payment unsuccessful","payment failed","billing"],
     ["billing@anthropic.com","receipts@anthropic.com",
      "invoice@anthropic.com","stripe.com","product.contact.hl.co.uk"],
     "Personal Finance"),

    # Utilities
    (["bill ready","statement ready","direct debit","payment due",
      "energy","broadband","water","council tax","insurance renewal"],
     [],
     "Utilities"),

    # Promos — sender domains
    ([],
     ["sharkninja.com","snapfish.com","secretlab.com","rac.co.uk",
      "hollywoodbowl.co.uk","eufylife.com","virginexperiencedays.com",
      "mcdonalds.com","iglucruise.com","gog.com","doorwaytovalueemail.com",
      "shein.com","karcher","procook.co.uk","qwertee.com",
      "asco-news.co.uk","mail.aliexpress.com","selections.aliexpress.com",
      "msi.marketing","macdonaldhotels.co.uk",
      "marketing@","newsletter@","offers@","hello@","hi@",
      "news@","updates@","info@","no-reply@","noreply@"],
     "Promos"),

    # Promos — subject keywords
    (["sale","offer","deal","discount","% off","save up","introducing",
      "new in","just arrived","flash","limited time","don't miss",
      "newsletter","unsubscribe","weekly","monthly digest","exclusive",
      "last chance","ends tonight","hurry","back in stock"],
     [],
     "Promos"),
]

def composio_call(action, arguments):
    api_key = os.environ.get("COMPOSIO_API_KEY", "")
    if not api_key:
        # fallback — read from secrets.json
        with open("/opt/openclaw/secrets.json") as f:
            api_key = json.load(f)["COMPOSIO_API_KEY"]

    payload = json.dumps({"arguments": arguments, "entity_id": "default"})
    result = subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://backend.composio.dev/api/v3/tools/execute/{action}",
        "-H", f"x-api-key: {api_key}",
        "-H", "Content-Type: application/json",
        "-d", payload
    ], capture_output=True, text=True)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": result.stdout}

def is_filtered(subject, sender):
    subj = (subject or "").lower()
    sndr = (sender or "").lower()
    if any(k in subj for k in BLOCKED_SUBJECTS):
        return True
    if any(b in sndr for b in BLOCKED_SENDERS):
        return True
    return False

def classify_email(subject, sender):
    subj = (subject or "").lower()
    sndr = (sender or "").lower()
    for subj_keywords, sender_keywords, label_name in RULES:
        if any(k in subj for k in subj_keywords):
            return label_name
        if sender_keywords and any(k in sndr for k in sender_keywords):
            return label_name
    return None

def record_promo(sender_email, sender_name):
    subprocess.run([
        sys.executable, PROMO_TRACKER,
        "--record",
        "--sender", sender_email,
        "--name", sender_name
    ], capture_output=True)

def add_label(message_id, label_id):
    return composio_call("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": [label_id],
        "remove_label_ids": []
    })

def archive_message(message_id):
    return composio_call("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": [],
        "remove_label_ids": ["INBOX"]
    })

def main(since_hours=12):
    print(f"Fetching unread emails from last {since_hours} hours...")

    result = composio_call("GMAIL_FETCH_EMAILS", {
        "query": f"is:unread newer_than:{since_hours}h",
        "label_ids": ["INBOX"],
        "max_results": 50,
        "include_spam_trash": False,
        "verbose": False
    })

    messages = result.get("data", {}).get("messages", [])
    if not messages:
        print("No new emails.")
        return {"total": 0, "labelled": {}, "filtered": 0, "unlabelled": []}

    print(f"Found {len(messages)} emails")

    summary = {
        "total": len(messages),
        "labelled": {},
        "filtered": 0,
        "unlabelled": []
    }

    for msg in messages:
        msg_id = msg.get("messageId", msg.get("id", ""))
        subject = msg.get("subject", "(no subject)")
        sender = msg.get("sender", msg.get("from", ""))
        sender_name = sender.split("<")[0].strip().strip('"') if "<" in sender else sender
        sender_email = sender.split("<")[-1].strip(">") if "<" in sender else sender

        # Security filter
        if is_filtered(subject, sender):
            summary["filtered"] += 1
            continue

        # Classify
        label_name = classify_email(subject, sender)

        if label_name and label_name in LABELS:
            label_id = LABELS[label_name]
            add_label(msg_id, label_id)

            # Archive promos and orders (remove from inbox)
            if label_name in ("Promos", "Delivered", "Shipped", "Online Orders",
                              "Utilities", "Personal Finance"):
                archive_message(msg_id)

            # Track promo senders
            if label_name == "Promos":
                record_promo(sender_email, sender_name)

            summary["labelled"][label_name] = summary["labelled"].get(label_name, 0) + 1
            print(f"  [{label_name}] {sender_name} — {subject[:60]}")
        else:
            summary["unlabelled"].append({
                "from": sender_name,
                "subject": subject[:80]
            })
            print(f"  [inbox] {sender_name} — {subject[:60]}")

    # Print summary for agent to forward to Telegram
    print("\n--- SUMMARY ---")
    print(f"Total processed: {summary['total']}")
    print(f"Filtered (security): {summary['filtered']}")
    for label, count in summary["labelled"].items():
        print(f"  {label}: {count} labelled/archived")

    if summary["unlabelled"]:
        print(f"\n{len(summary['unlabelled'])} emails need attention:")
        for e in summary["unlabelled"]:
            print(f"  • {e['from']} — {e['subject']}")
    else:
        print("Nothing needs your attention.")

    return summary

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-hours", type=int, default=12)
    args = parser.parse_args()
    main(args.since_hours)
