#!/usr/bin/env python3
"""
OpenClaw Gmail Pre-Filter
--------------------------
Applies the OpenClaw email security policy to a list of email objects from
Composio's GMAIL_FETCH_EMAILS / GMAIL_SEARCH_EMAILS response before they are
returned to the LLM.

Blocked emails have their subject, body, and snippet replaced with a filtered
placeholder. The From/Date headers are preserved so the user can see which
sender was blocked.

Usage:
    python3 pre_filter.py --stdin < emails.json
    python3 pre_filter.py emails.json

Input JSON: a list of email dicts, each containing at minimum:
    { "subject": "...", "from": "...", "snippet": "...", "body": "..." }

Output JSON: the same list, with blocked emails redacted.
"""

import json
import re
import sys
import argparse

# ---------------------------------------------------------------------------
# Blocked subject keywords (case-insensitive, word-boundary matched where possible)
# ---------------------------------------------------------------------------
BLOCKED_SUBJECT_PATTERNS = [
    re.compile(r'\botp\b', re.I),
    re.compile(r'\bone[\s\-]?time\s+(?:code|password|passcode|pin)\b', re.I),
    re.compile(r'\bverif(?:y|ication)\s*code\b', re.I),
    re.compile(r'\bverif(?:y|ication)\b', re.I),
    re.compile(r'\bpassword\s*reset\b', re.I),
    re.compile(r'\breset\s*(?:your\s+)?password\b', re.I),
    re.compile(r'\b2[\s\-]?fa\b', re.I),
    re.compile(r'\btwo[\s\-]?factor\b', re.I),
    re.compile(r'\bsecurity\s+(?:code|alert|notice|notification|warning)\b', re.I),
    re.compile(r'\bconfirm(?:ation)?\s+(?:your|this|email|account|code)\b', re.I),
    re.compile(r'\bconfirm\s+(?:your|this|email|account)\b', re.I),
    re.compile(r'\bsign[\s\-]?in\s+attempt\b', re.I),
    re.compile(r'\bunusual\s+(?:sign[\s\-]?in|activity|login)\b', re.I),
    re.compile(r'\btransaction\s+alert\b', re.I),
    re.compile(r'\bfraud\s+alert\b', re.I),
    re.compile(r'\bbank\s+alert\b', re.I),
    re.compile(r'\byour\s+(?:bank|account)\s+(?:has|statement)\b', re.I),
    re.compile(r'\baccount\s+(?:locked|suspended|compromised|breached)\b', re.I),
]

# ---------------------------------------------------------------------------
# Blocked subject standalone words (match these anywhere in the subject)
# ---------------------------------------------------------------------------
BLOCKED_SUBJECT_WORDS = [
    re.compile(r'\bsecurity\b', re.I),
]

# ---------------------------------------------------------------------------
# Blocked sender patterns (matches local-part of From address)
# ---------------------------------------------------------------------------
BLOCKED_SENDER_PATTERNS = [
    re.compile(r'no[\.\-]?reply@', re.I),
    re.compile(r'noreply@', re.I),
    re.compile(r'security@', re.I),
    re.compile(r'verify@', re.I),
    re.compile(r'verification@', re.I),
    re.compile(r'alerts@', re.I),
    re.compile(r'alert@', re.I),
    re.compile(r'otp@', re.I),
    re.compile(r'auth(?:entication)?@', re.I),
    re.compile(r'password@', re.I),
    re.compile(r'reset@', re.I),
]

FILTER_NOTICE = "[FILTERED — security-sensitive email blocked by OpenClaw policy. Content not processed.]"


def _check_blocked(email: dict) -> str | None:
    """Return a block reason string if the email should be blocked, else None."""
    subject = email.get('subject', '') or ''
    from_addr = email.get('from', '') or email.get('from_email', '') or ''

    # Check subject against specific patterns
    for pat in BLOCKED_SUBJECT_PATTERNS:
        if pat.search(subject):
            return f'subject matches pattern: {pat.pattern}'

    # Check subject for standalone blocked words (lower threshold)
    for pat in BLOCKED_SUBJECT_WORDS:
        if pat.search(subject):
            return f'subject contains blocked keyword: {pat.pattern}'

    # Check sender
    for pat in BLOCKED_SENDER_PATTERNS:
        if pat.search(from_addr):
            return f'sender matches blocked pattern: {pat.pattern}'

    return None


def apply_prefilter(emails: list) -> list:
    result = []
    blocked_count = 0

    for email in emails:
        reason = _check_blocked(email)
        if reason:
            blocked_count += 1
            # Preserve non-sensitive metadata, redact content
            redacted = {
                k: v for k, v in email.items()
                if k in ('id', 'threadId', 'date', 'from', 'from_email', 'from_name', 'label_ids', 'labels')
            }
            redacted['subject'] = FILTER_NOTICE
            redacted['snippet'] = FILTER_NOTICE
            redacted['body'] = FILTER_NOTICE
            redacted['body_html'] = FILTER_NOTICE
            redacted['_filtered'] = True
            redacted['_filter_reason'] = reason
            result.append(redacted)
        else:
            result.append(email)

    if blocked_count:
        print(f'[pre_filter] {blocked_count}/{len(emails)} email(s) blocked by security policy.', file=sys.stderr)

    return result


def main():
    parser = argparse.ArgumentParser(description='OpenClaw email pre-filter')
    parser.add_argument('file', nargs='?', help='JSON file to filter (default: stdin)')
    parser.add_argument('--stdin', action='store_true', help='Read from stdin')
    args = parser.parse_args()

    if args.stdin or not args.file:
        raw = sys.stdin.read()
    else:
        with open(args.file) as f:
            raw = f.read()

    try:
        emails = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'pre_filter: invalid JSON input: {e}', file=sys.stderr)
        sys.exit(1)

    if isinstance(emails, dict):
        # Might be a single email object
        emails = [emails]

    filtered = apply_prefilter(emails)
    print(json.dumps(filtered, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
