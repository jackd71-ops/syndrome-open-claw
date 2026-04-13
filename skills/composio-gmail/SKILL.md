---
name: composio-gmail
description: Gmail access via Composio managed OAuth. Read, search, send, and manage emails for jackd71@gmail.com. Use when the user asks to check, read, send, or manage email. IMPORTANT — always use Claude Sonnet model for email tasks (privacy policy).
metadata:
  author: lokivault-local
  version: "4.0"
  clawdbot:
    emoji: 📧
    requires:
      env:
        - COMPOSIO_API_KEY
---

# Composio Gmail

Access Gmail for jackd71@gmail.com via Composio's managed OAuth. Composio handles token refresh and OAuth securely.

**Model policy:** Email tasks use DeepSeek Chat by default. Only escalate to Sonnet for complex drafting or sending via Manifest routing.

## Pre-filter Policy
Apply this filter before showing emails to Darren. Filter on INTENT, not sender format.

### Block — Security & Authentication only
Block emails where the PRIMARY PURPOSE is security/authentication:
- Subject contains ANY of: `otp`, `one-time password`, `2fa`, `two-factor`, `verify your email`, `confirm your email`, `password reset`, `sign-in attempt`, `new login`, `security alert`, `unusual activity`
- Sender is specifically: `security@`, `verify@`, `noreply@accounts.`, `noreply@auth.`

Show blocked items as: `[FILTERED — security/auth email not processed by LokiVault policy]`

### Allow — Everything else including
- Order confirmations (`order confirmed`, `order received`, `thank you for your order`)
- Shipping & delivery notifications (`shipped`, `dispatched`, `out for delivery`, `delivered`, `tracking`)
- General `noreply@` retail/service senders — these are fine to show
- Newsletters, marketing, promotions
- Account notifications that are NOT security-related
- Receipts, invoices, statements

## Authentication

All Composio API calls require:
```
x-api-key: $COMPOSIO_API_KEY
Content-Type: application/json
```

**Base URL:** `https://backend.composio.dev/api/v3`

**Entity ID:** `default` (maps to the authorised Gmail account)

**Auth Config ID:** `ac_Foq5RfbQ7hud` (Composio-managed OAuth, created 2026-04-08)

## Check OAuth Connection Status

```bash
curl -s \
  "https://backend.composio.dev/api/v3/connected_accounts?user_ids=default" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  | python3 -m json.tool
```

## Initiate Gmail OAuth (get authUrl for user)

Only needed if no active Gmail connection exists:

```bash
curl -s -X POST \
  "https://backend.composio.dev/api/v3/connected_accounts" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"auth_config": {"id": "ac_Foq5RfbQ7hud"}, "connection": {"user_id": "default"}}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('OAuth URL:', d.get('redirect_url')); print('Connection ID:', d.get('id'))"
```

Share the OAuth URL with the user and ask them to visit it to authorise Gmail access.

## Fetch Emails (with pre-filter applied)

**Always apply the pre-filter before presenting results to the user.**

```bash
curl -s -X POST \
  "https://backend.composio.dev/api/v3/tools/execute/GMAIL_FETCH_EMAILS" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"max_results": 10, "label_ids": ["INBOX"], "include_spam_trash": false}, "entity_id": "default"}' \
  | python3 -c "
import sys, json

BLOCKED_SUBJECTS = ['otp','one-time password','2fa','two-factor','verify your email','confirm your email','password reset','sign-in attempt','new login','security alert','unusual activity']
BLOCKED_SENDERS = ['security@','verify@','noreply@accounts.','noreply@auth.']

d = json.load(sys.stdin)
emails = d.get('data', {}).get('messages', [])
for i, m in enumerate(emails, 1):
    subj = m.get('subject', '').lower()
    sender = m.get('sender', m.get('from', '')).lower()
    if any(k in subj for k in BLOCKED_SUBJECTS) or any(b in sender for b in BLOCKED_SENDERS):
        print(f'{i}. [FILTERED — security/auth email not processed by LokiVault policy]')
    else:
        print(f'{i}. From: {m.get(\"sender\", m.get(\"from\", \"\"))}')
        print(f'   Subject: {m.get(\"subject\", \"(no subject)\")}')
        print(f'   Date: {m.get(\"date\", \"\")}')
        print()
"
```

## Search Emails

```bash
curl -s -X POST \
  "https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEARCH_EMAILS" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"query": "is:unread newer_than:7d", "max_results": 20}, "entity_id": "default"}' \
  | python3 -m json.tool
```

## Send Email

```bash
curl -s -X POST \
  "https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEND_EMAIL" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"recipient_email": "recipient@example.com", "subject": "Subject here", "body": "Email body here"}, "entity_id": "default"}' \
  | python3 -m json.tool
```

## Common Gmail Action Names

| Action | Description | Key arguments |
|---|---|---|
| `GMAIL_FETCH_EMAILS` | Fetch recent emails from inbox | `max_results`, `label_ids` |
| `GMAIL_SEARCH_EMAILS` | Search with Gmail query syntax | `query`, `max_results` |
| `GMAIL_GET_THREAD` | Get a full email thread | `thread_id` |
| `GMAIL_SEND_EMAIL` | Send a new email | `recipient_email`, `subject`, `body` |
| `GMAIL_REPLY_TO_THREAD` | Reply to an existing thread | `thread_id`, `body` |
| `GMAIL_CREATE_DRAFT` | Create a draft (don't send) | `recipient_email`, `subject`, `body` |
| `GMAIL_LIST_LABELS` | List all labels (get IDs for custom labels) | *(none required)* |
| `GMAIL_CREATE_LABEL` | Create a new label | `label_name` |
| `GMAIL_ADD_LABEL_TO_EMAIL` | Add and/or remove labels on a message | `message_id`, `add_label_ids`, `remove_label_ids` |
| `GMAIL_DELETE_MESSAGE` | Delete a message (requires confirmation) | `message_id` |

## Add / Remove Labels on a Message

```bash
curl -s -X POST \
  "https://backend.composio.dev/api/v3/tools/execute/GMAIL_ADD_LABEL_TO_EMAIL" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"message_id": "MESSAGE_ID", "add_label_ids": ["LABEL_ID"], "remove_label_ids": []}, "entity_id": "default"}' \
  | python3 -m json.tool
```

- Use `GMAIL_LIST_LABELS` first to get IDs for custom labels
- System label IDs: `INBOX`, `UNREAD`, `STARRED`, `IMPORTANT`, `TRASH`, `SPAM`
- Both `add_label_ids` and `remove_label_ids` can be used in the same call

All actions follow the same pattern:
```bash
curl -s -X POST \
  "https://backend.composio.dev/api/v3/tools/execute/{ACTION_NAME}" \
  -H "x-api-key: $COMPOSIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"arguments": { ... }, "entity_id": "default"}'
```

## Notes

- Always confirm before sending email or deleting messages.
- Respect the pre-filter policy above — no exceptions.
- `COMPOSIO_API_KEY` is available as a container env var — never read secrets.json.
- Composio API errors 401/403 mean the key is invalid or the Gmail connection hasn't been authorised yet.
- To check connection status: `GET /api/v3/connected_accounts?user_ids=default` — look for `toolkit.slug == "gmail"` and `status == "ACTIVE"`.
