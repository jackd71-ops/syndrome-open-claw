# Heartbeat — strict instructions

DO NOT check budgets, balances, provider spending, or API costs during heartbeat. This is handled by the daily cron job at 5am, which only messages if there's an issue.

DO NOT inspect or report on cron job status files, delivery confirmations, or generate health check reports. All cron jobs manage their own notifications.

If nothing urgent: reply HEARTBEAT_OK.
