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
    'security alert','unusual activity','verification code',
    'is your verification code','email verification'
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
    "Emissions Claim":  "Label_48",
    "Holidays&Travel":  "Label_30",
    "House Projects":   "Label_41",
    "Dentist - NHS":    "Label_42",
    "Doug Kennel":      "Label_34",
    "Health":           "Label_46",
}

INBOX_KEEP_LABELS = {
    "Emissions Claim", "Holidays&Travel", "House Projects",
    "Dentist - NHS", "Doug Kennel", "Health",
}

RULES = [
    # Delivered — check before Promos/Shipped
    (["delivered","your parcel has been delivered","delivery complete",
      "successfully delivered","out for delivery"],
     ["inpost.co.uk"],
     "Delivered"),

    # Shipped
    (["shipped","dispatched","on its way","your order is on the way",
      "tracking number","your tracking","return qr code",
      "heading back"],
     [],
     "Shipped"),

    # Emissions Claim — label only, stays in inbox
    ([],
     ["bondturner.com"],
     "Emissions Claim"),

    # Holidays & Travel — label only, stays in inbox
    (["travel itinerary","booking ref","booking confirmation","booking reference",
      "your holiday","holiday change","pre-flight","departure welcome",
      "your flight","your trip"],
     ["tui.co.uk","emails.tui.co.uk","ryanairemail.com","vipattractions.com",
      "cathaypacific.com","bahia-principe","booking.com","lastminute.com",
      "enterprise.com","coachhirecomparison"],
     "Holidays&Travel"),

    # House Projects — label only, stays in inbox
    (["snag visit","snag appointment","plot 47","plot 047","new tilia",
      "your new home","completion"],
     ["tiliahomes.co.uk","theflooringcentrenw.co.uk","acorn-gardening.co.uk",
      "blinds-2go.co.uk","scs.co.uk","donotreply@scs.co.uk"],
     "House Projects"),

    # Dentist / NHS — label only, stays in inbox
    (["dental appointment","dentist"],
     ["patientbridge.gosensei"],
     "Dentist - NHS"),

    # Dog Kennel — label only, stays in inbox
    (["bancroft","boarding kennels","kennels"],
     ["kennelbooker","bancroftkennels.com"],
     "Doug Kennel"),

    # Health — label only, stays in inbox
    (["weight loss","mounjaro","consultation has been approved",
      "your ashcroft","ashcroft pharmacy order"],
     ["ashcroftpharmacy.co.uk"],
     "Health"),

    # Online Orders
    (["order confirmed","order received","thank you for your order",
      "order #","your order","your recent order","ordered:",
      "purchase confirmation","receipt for","payment confirmation",
      "your refund","return drop-off","booking confirmation",
      "booking reference","rma"],
     ["auto-confirm@amazon","marketplace-messages@amazon","return@amazon",
      "mozillion.com","returns@scan","autoreturns@scan","website@scan.co.uk"],
     "Online Orders"),

    # Personal Finance
    (["dividend","portfolio","investment","isa","sipp","pension",
      "statement","transaction","mortgage","receipt","invoice",
      "payment unsuccessful","payment failed","billing",
      "credit score","your allowances","tax year","secure message"],
     ["billing@anthropic.com","receipts@anthropic.com",
      "invoice@anthropic.com","stripe.com","product.contact.hl.co.uk",
      "lloydsbank.co.uk","starlingbank.com","hargreaveslansdown",
      "vantage.h-l.co.uk","aegon.co.uk","uk.affirm.com","affirm.com",
      "notify.experian.co.uk","experian.co.uk"],
     "Personal Finance"),

    # Utilities
    (["bill is ready","bill ready","statement is ready","statement ready",
      "direct debit","payment due","energy","broadband","water",
      "council tax","insurance renewal","food waste"],
     ["plus.net","plusnet","octopus.energy","south.ribble"],
     "Utilities"),

    # Promos — sender domains
    ([],
     ["sharkninja.com","snapfish.com","secretlab.com","rac.co.uk",
      "hollywoodbowl.co.uk","eufylife.com","virginexperiencedays.com",
      "mcdonalds.com","iglucruise.com","gog.com","doorwaytovalueemail.com",
      "shein.com","karcher","procook.co.uk","qwertee.com",
      "asco-news.co.uk","mail.aliexpress.com","selections.aliexpress.com",
      "msi.marketing","macdonaldhotels.co.uk",
      "zooplus.co.uk","thevillagebutchers.co.uk","skyscanner",
      "mails.dewalt.eu","emails.holidayextras","luxuryescapes.com",
      "e.next.co.uk","diddlysquatfarmshop.com","rspca.org.uk",
      "emails.pocruises.com","update.cineworld.com","thermomix",
      "email.ancestry.co.uk","ancestry.co.uk","newsletter.tp-link.com",
      "eg.hotels.com","hotels.com","airbnb.com",
      "email.moonpig.com","community.denbypottery.com",
      "openrouter.ai","nl.smartfreestuff.co.uk",
      "email.moneysavingexpert.com","comms.ashcroftpharmacy.co.uk",
      "plantsnap.com","dotdigital-email.com","vinted.co.uk",
      "booksy.com","quora.com",
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

def _check_response(action, message_id, resp):
    if not isinstance(resp, dict):
        print(f"  ERROR [{action}] msg={message_id}: unexpected response: {resp}", file=sys.stderr)
        return False
    ok = (resp.get("successfull") or resp.get("successful") or
          resp.get("data", {}).get("success") or
          resp.get("data", {}).get("successfull"))
    if not ok:
        err = resp.get("error") or resp.get("message") or resp.get("data", {}).get("error") or resp
        print(f"  ERROR [{action}] msg={message_id}: {err}", file=sys.stderr)
        return False
    return True

def add_label(message_id, label_id, archive, dry_run):
    if dry_run:
        return True
    resp = composio_call("GMAIL_ADD_LABEL_TO_EMAIL", {
        "message_id": message_id,
        "add_label_ids": [label_id],
        "remove_label_ids": ["INBOX"] if archive else []
    })
    return _check_response("add_label", message_id, resp)

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

UNMATCHED_LOG = "/home/node/.openclaw/workspace/skills/composio-gmail/scripts/backfill_unmatched.txt"

def main(dry_run=False):
    if dry_run:
        print("DRY RUN — no changes will be made")

    total = 0
    labelled = {}
    label_failed = 0
    filtered = 0
    already_labelled = 0
    unmatched = []
    page_token = None
    batch = 1

    while True:
        print(f"\nFetching batch {batch}...")
        result = fetch_batch(page_token)

        messages = result.get("data", {}).get("messages", [])
        next_token = result.get("data", {}).get("nextPageToken")

        if not messages:
            if result.get("error") or not result.get("data"):
                print(f"WARNING: fetch returned unexpected structure: {result}", file=sys.stderr)
            print("No more emails.")
            break

        print(f"  Processing {len(messages)} emails...")

        for msg in messages:
            msg_id = msg.get("messageId", msg.get("id", ""))
            subject = msg.get("subject") or "(no subject)"
            sender = msg.get("sender") or msg.get("from") or ""

            total += 1

            if is_filtered(subject, sender):
                filtered += 1
                continue

            existing = msg.get("labelIds", [])
            if any(lid in existing for lid in LABELS.values()):
                already_labelled += 1
                continue

            label_name = classify_email(subject, sender)
            if label_name and label_name in LABELS:
                label_id = LABELS[label_name]
                archive = label_name not in INBOX_KEEP_LABELS
                ok = add_label(msg_id, label_id, archive, dry_run)
                if ok:
                    labelled[label_name] = labelled.get(label_name, 0) + 1
                    print(f"  [{label_name}] {sender[:40]} — {subject[:50]}")
                else:
                    label_failed += 1
            else:
                unmatched.append(f"{sender[:50]} | {subject[:80]}")

        print(f"  Batch {batch} done. Running total: {total} processed")

        if not next_token:
            print("All batches complete.")
            break

        page_token = next_token
        batch += 1
        time.sleep(2)

    # Write unmatched to file for review
    if unmatched:
        with open(UNMATCHED_LOG, "w") as f:
            f.write(f"Unmatched emails ({len(unmatched)}) — review to add new rules\n")
            f.write("=" * 80 + "\n")
            for line in unmatched:
                f.write(line + "\n")
        print(f"\nUnmatched emails written to: {UNMATCHED_LOG}")

    print("\n=== BACKFILL COMPLETE ===")
    print(f"Total processed:            {total}")
    print(f"Already labelled (skipped): {already_labelled}")
    print(f"Filtered (security):        {filtered}")
    print(f"Label API failures:         {label_failed}")
    print(f"Unmatched (no rule):        {len(unmatched)}")
    for label, count in sorted(labelled.items(), key=lambda x: x[1], reverse=True):
        print(f"  {label}: {count}")
    print(f"Total newly labelled:       {sum(labelled.values())}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be labelled without making changes")
    args = parser.parse_args()
    main(args.dry_run)
