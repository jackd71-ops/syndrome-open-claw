---
name: cost-summary
description: Daily AI cost reporting and budget monitoring. Query the Manifest DB for today's and month-to-date LLM spend broken down by model, send a Telegram summary at 11pm UK time, and alert immediately if monthly spend exceeds 90% of the £30 budget. Use when asked about costs, usage, or budget status.
metadata:
  author: openclaw
  version: "1.0"
  clawdbot:
    emoji: 📊
    requires:
      bins: []
      env:
        - TELEGRAM_BOT_TOKEN
---

# Cost Summary Skill

Queries the Manifest SQLite database at `/home/node/.openclaw/manifest/manifest.db` for LLM usage data and generates cost reports.

## Scheduled runs
- **11:00 PM UK daily** — full daily summary sent to Telegram
- **Hourly budget check** — silent unless 90% threshold ($26) is crossed

## Manual run (on demand)

```bash
/opt/python/bin/python3 ~/.openclaw/workspace/skills/cost-summary/scripts/daily_cost.py --print
```

## Send summary now

```bash
/opt/python/bin/python3 ~/.openclaw/workspace/skills/cost-summary/scripts/daily_cost.py --summary
```

## Budget check only

```bash
/opt/python/bin/python3 ~/.openclaw/workspace/skills/cost-summary/scripts/daily_cost.py --budget-check
```

## Message format

```
📊 Daily AI Cost Summary
Date: 08 Apr 2026

DeepSeek V3.2: $0.0012 (4.3k tokens)
DeepSeek R1: $0.0000 (0 tokens)
Claude Sonnet: $0.0034 (228 tokens)

Today's total: $0.0046 (£0.00)
Month to date: $0.0046 (£0.00) of $30 budget
Remaining budget: $29.99
```

## Budget alert threshold
Alert fires (once per month) when MTD spend reaches $26 (90% of $30 budget).
Alert state stored in `data/cost-alert-sent.json` — delete to re-arm.

## Data source
Table: `agent_messages` in Manifest DB — columns used: `model`, `input_tokens`,
`output_tokens`, `cache_read_tokens`, `cost_usd`, `timestamp`, `status`.
Costs are taken from `cost_usd` if non-zero, else computed from known token prices.
