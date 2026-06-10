#!/usr/bin/env python3
"""
gmail-archive.py — Weekly archive of labelled emails.
- Age-based labels: archived after N days regardless of read status.
- Read-based labels: archived once read (is:read), stay while unread.
Usage: python3 gmail-archive.py [--days 7] [--dry-run]
"""
import subprocess
import json
import os
import argparse
from datetime import datetime, timezone

# Archived after N days (age-based, regardless of read status)
LABELS_TO_ARCHIVE = {
    "Promos":           "Label_39",
    "Online Orders":    "Label_31",
    "Delivered":        "Label_43",
    "Shipped":          "Label_8108153258188589479",
    "Personal Finance": "Label_3002147784474582387",
    "Utilities":        "Label_38",
}

# Archived only once read — stay in inbox while unread
LABELS_ARCHIVE_WHEN_READ = {
    "Emissions Claim":  "Label_48",
    "Holidays&Travel":  "Label_30",
    "House Projects":   "Label_41",
    "Dentist - NHS":    "Label_42",
    "Doug Kennel":      "Label_34",
    "Health":           "Label_46",
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

def _label_query_name(label_name):
    return label_name.lower().replace(" ", "-")

def _check_response(action, message_id, resp):
    if not isinstance(resp, dict):
        return False
    ok = (resp.get("successfull") or resp.get("successful") or
          resp.get("data", {}).get("success") or
          resp.get("data", {}).get("successfull"))
    if not ok:
        err = resp.get("error") or resp.get("message") or resp.get("data", {}).get("error") or resp
        print(f"  ERROR [{action}] msg={message_id}: {err}")
        return False
    return True

def archive_message(message_id, dry_run=False):
    if dry_run:
        return True
    resp = composio_call("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": [],
        "remove_label_ids": ["INBOX"]
    })
    return _check_response("archive", message_id, resp)

def fetch_all(query):
    page_token = None
    all_messages = []
    while True:
        args = {
            "query": query,
            "max_results": 100,
            "include_spam_trash": False,
            "verbose": False
        }
        if page_token:
            args["page_token"] = page_token
        result = composio_call("GMAIL_FETCH_EMAILS", args)
        messages = result.get("data", {}).get("messages", [])
        all_messages.extend(messages)
        page_token = result.get("data", {}).get("nextPageToken")
        if not messages or not page_token:
            break
    return all_messages

def process_label(label_name, query, dry_run):
    messages = fetch_all(query)
    if not messages:
        print(f"  No emails to archive for {label_name}")
        return 0, 0
    print(f"  Found {len(messages)} emails")
    count, errors = 0, 0
    for msg in messages:
        msg_id = msg.get("messageId") or msg.get("id") or ""
        subject = msg.get("subject") or "(no subject)"
        sender = msg.get("sender") or msg.get("from") or ""
        if not msg_id:
            errors += 1
            continue
        if dry_run:
            print(f"  [DRY RUN] Would archive: {sender[:40]} — {subject[:60]}")
            count += 1
        else:
            if archive_message(msg_id):
                print(f"  Archived: {sender[:40]} — {subject[:60]}")
                count += 1
            else:
                errors += 1
    return count, errors

def main(days=7, dry_run=False):
    total_archived = 0
    total_errors = 0

    print(f"=== Age-based archive (older than {days} days) ===")
    for label_name in LABELS_TO_ARCHIVE:
        print(f"\nProcessing {label_name}...")
        q = f"label:{_label_query_name(label_name)} older_than:{days}d in:inbox"
        count, errors = process_label(label_name, q, dry_run)
        total_archived += count
        total_errors += errors
        print(f"  {label_name}: {count} archived")

    print(f"\n=== Read-based archive (inbox-keep labels, read only) ===")
    for label_name in LABELS_ARCHIVE_WHEN_READ:
        print(f"\nProcessing {label_name}...")
        q = f"label:{_label_query_name(label_name)} is:read in:inbox"
        count, errors = process_label(label_name, q, dry_run)
        total_archived += count
        total_errors += errors
        print(f"  {label_name}: {count} archived")

    print(f"\n--- ARCHIVE SUMMARY ---")
    print(f"Total archived: {total_archived}")
    if dry_run:
        print("(DRY RUN — no changes made)")
    if total_errors:
        print(f"Errors: {total_errors}")
    return total_archived

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(args.days, args.dry_run)
