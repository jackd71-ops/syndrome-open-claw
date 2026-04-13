#!/usr/bin/env python3
"""
gmail-archive.py — Weekly archive of already-labelled emails older than 7 days.
Removes INBOX label from emails that have been labelled by gmail-daily.py.
Usage: python3 gmail-archive.py [--days 7] [--dry-run]
"""
import subprocess
import json
import os
import argparse
from datetime import datetime, timezone

LABELS_TO_ARCHIVE = {
    "Promos":           "Label_39",
    "Online Orders":    "Label_31",
    "Delivered":        "Label_43",
    "Watches":          "Label_44",
    "Shipped":          "Label_8108153258188589479",
    "Personal Finance": "Label_3002147784474582387",
    "Utilities":        "Label_38",
}

def composio_call(action, arguments):
    api_key = os.environ.get("COMPOSIO_API_KEY", "")
    if not api_key:
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

def archive_message(message_id, dry_run=False):
    if dry_run:
        return {"dry_run": True}
    return composio_call("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": [],
        "remove_label_ids": ["INBOX"]
    })

def main(days=7, dry_run=False):
    total_archived = 0
    errors = 0

    for label_name, label_id in LABELS_TO_ARCHIVE.items():
        print(f"Processing {label_name}...")

        result = composio_call("GMAIL_FETCH_EMAILS", {
            "query": f"label:{label_id} older_than:{days}d in:inbox",
            "max_results": 100,
            "include_spam_trash": False,
            "verbose": False
        })

        messages = result.get("data", {}).get("messages", [])
        if not messages:
            print(f"  No emails to archive for {label_name}")
            continue

        print(f"  Found {len(messages)} emails to archive")
        label_count = 0

        for msg in messages:
            msg_id = msg.get("messageId", msg.get("id", ""))
            subject = msg.get("subject", "(no subject)")
            sender = msg.get("sender", msg.get("from", ""))

            if not msg_id:
                errors += 1
                continue

            resp = archive_message(msg_id, dry_run=dry_run)

            if dry_run:
                print(f"  [DRY RUN] Would archive: {sender} — {subject[:60]}")
                label_count += 1
            elif resp.get("error"):
                print(f"  ERROR archiving {msg_id}: {resp['error']}")
                errors += 1
            else:
                print(f"  Archived: {sender} — {subject[:60]}")
                label_count += 1

        total_archived += label_count
        print(f"  {label_name}: {label_count} archived")

    print(f"\n--- ARCHIVE SUMMARY ---")
    print(f"Total archived: {total_archived}")
    if dry_run:
        print("(DRY RUN — no changes made)")
    if errors:
        print(f"Errors: {errors}")
    return total_archived

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(args.days, args.dry_run)
