# Heartbeat — strict instructions

DO NOT check budgets, balances, provider spending, or API costs during heartbeat. This is handled by the daily cron job at 5am, which only messages if there's an issue.

If nothing urgent: reply HEARTBEAT_OK.
