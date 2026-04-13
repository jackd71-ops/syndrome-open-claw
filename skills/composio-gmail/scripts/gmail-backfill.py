#!/usr/bin/env python3
"""
gmail-backfill.py — One-off backfill to classify and label all emails.
Processes all emails in batches of 50, applying the same rules as gmail-daily.py.
Usage: python3 gmail-backfill.py [--dry-run]
"""
import subprocess
import json
import os
import sys
import argparse
import time

# Import rules and functions from gmail-daily
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

COMPOSIO_API_KEY = None

def get_api_key():
    global COMPOSIO_API_KEY
    if COMPOSIO_API_KEY:
        return COMPOSIO_API_KEY
    COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
    if not COMPOSIO_API_KEY:
        with open("/opt/openclaw/secrets.json") as f:
            COMPOSIO_API_KEY = json.load(f)["COMPOSIO_API_KEY"]
    return COMPOSIO_API_KEY

def composio_call(action, arguments):
    payload = json.dumps({"arguments": arguments, "entity_id": "default"})
    result = subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://backend.composio.dev/api/v3/tools/execute/{action}",
        "-H", f"x-api-key: {get_api_key()}",
        "-H", "Content-Type: application/json",
        "-d", payload
    ], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": result.stdout}

BLOCKED_SUBJECTS = [
    'otp','one-time password','2fa','two-factor','verify your email',
    'confirm your email','password reset','sign-in attempt','new login',
    'security alert','unusual activity'
]
BLOCKED_SENDERS = [
    'security@','verify@','noreply@accounts.','noreply@auth.','no-reply@accounts.',
    'jackd71@gmail.com'
]

LABELS = {
    "Promos":           "Label_39",
    "Online Orders":    "Label_31",
    "Delivered":        "Label_43",
    "Watches":          "Label_44",
    "Shipped":          "Label_8108153258188589479",
    "Personal Finance": "Label_3002147784474582387",
    "Utilities":        "Label_38",
}

RULES = [
    (["delivered","your parcel has been delivered","delivery complete",
      "successfully delivered","out for delivery"],
     [], "Delivered"),
    (["shipped","dispatched","on its way","your order is on the way",
      "tracking number","your tracking"],
     [], "Shipped"),
    (["order confirmed","order received","thank you for your order",
      "order #","your order","purchase confirmation","receipt for",
      "payment confirmation"],
     [], "Online Orders"),
    (["dividend","portfolio","investment","isa","sipp","pension",
      "statement","transaction","mortgage","receipt","invoice",
      "payment unsuccessful","payment failed","billing"],
     ["billing@anthropic.com","receipts@anthropic.com","invoice@anthropic.com",
      "stripe.com","product.contact.hl.co.uk"],
     "Personal Finance"),
    (["bill ready","statement ready","direct debit","payment due",
      "energy","broadband","water","council tax","insurance renewal"],
     [], "Utilities"),
    ([],
     ["sharkninja.com","snapfish.com","secretlab.com","rac.co.uk",
      "hollywoodbowl.co.uk","eufylife.com","virginexperiencedays.com",
      "mcdonalds.com","iglucruise.com","gog.com","doorwaytovalueemail.com",
      "shein.com","karcher","procook.co.uk","qwertee.com","asco-news.co.uk",
      "mail.aliexpress.com","selections.aliexpress.com","msi.marketing",
      "macdonaldhotels.co.uk","marketing@","newsletter@","offers@","hello@",
      "hi@","news@","updates@","info@","no-reply@","noreply@"],
     "Promos"),
    (["sale","offer","deal","discount","% off","save up","introducing",
      "new in","just arrived","flash","limited time","don't miss",
      "newsletter","unsubscribe","weekly","monthly digest","exclusive",
      "last chance","ends tonight","hurry","back in stock"],
     [], "Promos"),
]

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
        if subj_keywords and any(k in subj for k in subj_keywords):
            return label_name
        if sender_keywords and any(k in sndr for k in sender_keywords):
            return label_name
    return None

def add_label_and_archive(message_id, label_id, dry_run):
    if dry_run:
        return
    composio_call("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": [label_id],
        "remove_label_ids": ["INBOX"]
    })

def fetch_batch(page_token=None):
    args = {
        "max_results": 50,
        "include_spam_trash": False,
        "verbose": False,
        "query": "in:all"
    }
    if page_token:
        args["page_token"] = page_token
    return composio_call("GMAIL_FETCH_EMAILS", args)

def main(dry_run=False):
    if dry_run:
        print("DRY RUN — no changes will be made")

    total = 0
    labelled = {}
    filtered = 0
    skipped = 0
    page_token = None
    batch = 1

    while True:
        print(f"\nFetching batch {batch}...")
        result = fetch_batch(page_token)

        messages = result.get("data", {}).get("messages", [])
        next_token = result.get("data", {}).get("nextPageToken")

        if not messages:
            print("No more emails.")
            break

        print(f"  Processing {len(messages)} emails...")

        for msg in messages:
            msg_id = msg.get("messageId", msg.get("id", ""))
            subject = msg.get("subject", "(no subject)")
            sender = msg.get("sender", msg.get("from", ""))

            total += 1

            if is_filtered(subject, sender):
                filtered += 1
                continue

            # Skip if already has a non-inbox label
            existing_labels = msg.get("labelIds", [])
            already_labelled = any(
                lid in existing_labels
                for lid in LABELS.values()
            )
            if already_labelled:
                skipped += 1
                continue

            label_name = classify_email(subject, sender)
            if label_name and label_name in LABELS:
                label_id = LABELS[label_name]
                add_label_and_archive(msg_id, label_id, dry_run)
                labelled[label_name] = labelled.get(label_name, 0) + 1
                print(f"  [{label_name}] {sender[:40]} — {subject[:50]}")
            else:
                skipped += 1

        print(f"  Batch {batch} done. Running total: {total} processed")

        if not next_token:
            print("All batches complete.")
            break

        page_token = next_token
        batch += 1
        time.sleep(2)  # Be gentle with the API

    print("\n=== BACKFILL COMPLETE ===")
    print(f"Total processed: {total}")
    print(f"Filtered (security): {filtered}")
    print(f"Skipped (already labelled or unmatched): {skipped}")
    for label, count in sorted(labelled.items(), key=lambda x: x[1], reverse=True):
        print(f"  {label}: {count}")
    print(f"Total labelled: {sum(labelled.values())}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be labelled without making changes")
    args = parser.parse_args()
    main(args.dry_run)
