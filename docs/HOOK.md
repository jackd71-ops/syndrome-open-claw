---
name: gmail-prefilter
description: "Intercept incoming Gmail push-notification messages and redact security-sensitive content before it reaches the LLM. Blocks OTPs, verification codes, password resets, bank alerts, and emails from security/noreply senders."
metadata:
  openclaw:
    emoji: "🔒"
    events:
      - "message:preprocessed"
    requires:
      bins: []
---

# Gmail Pre-Filter Hook

Intercepts every `message:preprocessed` event that originates from the Gmail push-notification channel (`channelId === "gmail"`) and replaces the body of any security-sensitive email with a blocked notice — so that content never reaches the LLM.

## What it blocks

**Subject keywords (case-insensitive):**
- OTP, one-time code/password/passcode/pin
- verification code, verify, verification
- password reset, reset password
- 2FA, two-factor
- security (as standalone word)
- confirm, confirmation
- sign-in attempt, unusual sign-in/activity
- transaction alert, fraud alert, bank alert
- account locked/suspended/compromised

**Sender patterns:**
- no-reply@\*, noreply@\*, security@\*, verify@\*, alerts@\*, alert@\*, otp@\*, auth@\*, password@\*, reset@\*

## What it does

When a blocked email is detected, `context.bodyForAgent` is replaced with:

```
[EMAIL BLOCKED BY OpenClaw SECURITY POLICY]
Sender: <from>
Reason: <why it was blocked>
The content of this email has been filtered and will not be processed.
```

This ensures the LLM never sees the email content. The filter logs to stderr for audit purposes.
