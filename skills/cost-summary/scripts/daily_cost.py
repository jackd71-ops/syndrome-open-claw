#!/usr/bin/env python3
"""
LokiVault Daily Cost Summary
-----------------------------
Queries the Manifest SQLite DB for today's and month-to-date token usage
and cost, broken down by model, then sends a Telegram summary.

Also checks if monthly spend has crossed the 90% alert threshold ($26).

Usage:
    daily_cost.py --summary          # send daily summary (11pm cron)
    daily_cost.py --budget-check     # only alert if 90% threshold crossed
    daily_cost.py --print            # print report without sending
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = '/home/node/.openclaw/manifest/manifest.db'
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = '1163684840'
MONTHLY_BUDGET_USD = 30.00
ALERT_THRESHOLD_PCT = 0.90          # alert at 90% = $26
GBP_PER_USD = 0.79                  # approximate; updated manually or via API

# Month 1 budget is $30; subsequent months also $30 per project memory
MONTH_1_BUDGET = 30.00
ONGOING_BUDGET = 30.00

# Model display name mapping (response_model → friendly name)
MODEL_NAMES = {
    'deepseek-chat':        'DeepSeek V3.2',
    'deepseek/deepseek-chat': 'DeepSeek V3.2',
    'deepseek-reasoner':    'DeepSeek R1',
    'deepseek/deepseek-reasoner': 'DeepSeek R1',
    'claude-sonnet-4-6':    'Claude Sonnet',
    'claude-sonnet-4-5':    'Claude Sonnet',
    'anthropic/claude-sonnet-4-6': 'Claude Sonnet',
}

# Model costs (USD per token) — used as fallback if DB cost_usd is zero
MODEL_COSTS = {
    'deepseek-chat':        {'input': 2.8e-7,  'output': 4.2e-7,  'cache_read': 2.8e-8},
    'deepseek/deepseek-chat': {'input': 2.8e-7, 'output': 4.2e-7, 'cache_read': 2.8e-8},
    'deepseek-reasoner':    {'input': 5.5e-7,  'output': 2.19e-6, 'cache_read': 5.5e-8},
    'deepseek/deepseek-reasoner': {'input': 5.5e-7, 'output': 2.19e-6, 'cache_read': 5.5e-8},
    'claude-sonnet-4-6':    {'input': 3.0e-6,  'output': 1.5e-5,  'cache_read': 3.0e-7},
    'anthropic/claude-sonnet-4-6': {'input': 3.0e-6, 'output': 1.5e-5, 'cache_read': 3.0e-7},
}

ALERT_SENT_FLAG = '/home/node/.openclaw/workspace/data/cost-alert-sent.json'


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f'Manifest DB not found at {DB_PATH}')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _today_range() -> tuple[str, str]:
    """Return ISO start/end strings for today in UTC."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


def _month_range() -> tuple[str, str]:
    """Return ISO start/end strings for the current calendar month in UTC."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # End of month: first day of next month minus 1 second
    if now.month == 12:
        end = now.replace(year=now.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    else:
        end = now.replace(month=now.month + 1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    end = end - datetime.timedelta(seconds=1)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


def _calc_cost(row_model: str, input_tok: int, output_tok: int, cache_read: int,
               db_cost: float) -> float:
    """Use DB cost_usd if non-zero, else compute from token prices."""
    if db_cost and db_cost > 0:
        return db_cost
    rates = MODEL_COSTS.get(row_model, {})
    if not rates:
        return 0.0
    return (input_tok * rates.get('input', 0) +
            output_tok * rates.get('output', 0) +
            cache_read * rates.get('cache_read', 0))


def query_usage(start: str, end: str) -> dict:
    """
    Return per-model usage for the given time window.
    Result: {model_key: {tokens, input_tokens, output_tokens, cost_usd}}
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            model,
            SUM(input_tokens)      AS input_tokens,
            SUM(output_tokens)     AS output_tokens,
            SUM(cache_read_tokens) AS cache_read_tokens,
            SUM(cost_usd)          AS cost_usd
        FROM agent_messages
        WHERE timestamp >= ? AND timestamp <= ?
          AND status = 'ok'
        GROUP BY model
    """, (start, end))

    result = {}
    for row in cur.fetchall():
        model = row['model'] or 'unknown'
        inp = row['input_tokens'] or 0
        out = row['output_tokens'] or 0
        cache = row['cache_read_tokens'] or 0
        db_cost = float(row['cost_usd'] or 0)
        cost = _calc_cost(model, inp, out, cache, db_cost)
        result[model] = {
            'input_tokens': inp,
            'output_tokens': out,
            'cache_read_tokens': cache,
            'tokens': inp + out + cache,
            'cost_usd': cost,
        }
    conn.close()
    return result


def group_by_friendly(usage: dict) -> dict:
    """Merge model variants under their friendly display name."""
    out = {}
    for model_key, data in usage.items():
        friendly = MODEL_NAMES.get(model_key, model_key)
        if friendly not in out:
            out[friendly] = {'tokens': 0, 'input_tokens': 0, 'output_tokens': 0,
                             'cache_read_tokens': 0, 'cost_usd': 0.0}
        for k in ('tokens', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cost_usd'):
            out[friendly][k] += data.get(k, 0)
    return out


# ---------------------------------------------------------------------------
# Alert state (avoid duplicate budget alerts per month)
# ---------------------------------------------------------------------------

def _load_alert_state() -> dict:
    if os.path.exists(ALERT_SENT_FLAG):
        with open(ALERT_SENT_FLAG) as f:
            return json.load(f)
    return {}


def _save_alert_state(state: dict) -> None:
    os.makedirs(os.path.dirname(ALERT_SENT_FLAG), exist_ok=True)
    with open(ALERT_SENT_FLAG, 'w') as f:
        json.dump(state, f, indent=2)


def _alert_already_sent(month_key: str) -> bool:
    state = _load_alert_state()
    return state.get(month_key, False)


def _mark_alert_sent(month_key: str) -> None:
    state = _load_alert_state()
    state[month_key] = True
    _save_alert_state(state)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _telegram_send(text: str) -> bool:
    if not BOT_TOKEN:
        print('[cost-summary] TELEGRAM_BOT_TOKEN not set')
        return False
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = json.dumps({
        'chat_id': CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True,
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
            return result.get('ok', False)
    except Exception as e:
        print(f'[cost-summary] Telegram error: {e}')
        return False


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f'{n/1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n/1_000:.1f}k'
    return str(n)


def build_report(send: bool = True) -> str:
    today_start, today_end = _today_range()
    month_start, month_end = _month_range()
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime('%d %b %Y')
    month_key = now.strftime('%Y-%m')

    # Query
    today_raw = query_usage(today_start, today_end)
    month_raw = query_usage(month_start, month_end)

    today_by_model = group_by_friendly(today_raw)
    month_by_model = group_by_friendly(month_raw)

    today_total_usd = sum(v['cost_usd'] for v in today_by_model.values())
    month_total_usd = sum(v['cost_usd'] for v in month_by_model.values())

    today_total_gbp = today_total_usd * GBP_PER_USD
    month_total_gbp = month_total_usd * GBP_PER_USD
    remaining_usd = max(0.0, MONTHLY_BUDGET_USD - month_total_usd)

    # Format per-model lines (ordered)
    MODEL_ORDER = ['DeepSeek V3.2', 'DeepSeek R1', 'Claude Sonnet']
    model_lines = []
    for name in MODEL_ORDER:
        data = today_by_model.get(name, {})
        cost = data.get('cost_usd', 0.0)
        toks = data.get('tokens', 0)
        if cost > 0 or toks > 0:
            model_lines.append(f'{name}: ${cost:.4f} ({_fmt_tokens(toks)} tokens)')
        else:
            model_lines.append(f'{name}: $0.0000 (0 tokens)')

    model_section = '\n'.join(model_lines)

    report = (
        f'📊 *Daily AI Cost Summary*\n'
        f'Date: {date_str}\n\n'
        f'{model_section}\n\n'
        f"Today's total: ${today_total_usd:.4f} (£{today_total_gbp:.2f})\n"
        f'Month to date: ${month_total_usd:.4f} (£{month_total_gbp:.2f}) of ${MONTHLY_BUDGET_USD:.0f} budget\n'
        f'Remaining budget: ${remaining_usd:.2f}'
    )

    print(report)

    if send:
        ok = _telegram_send(report)
        print(f'[cost-summary] Daily summary sent: {ok}')

    # Budget alert check
    alert_threshold = MONTHLY_BUDGET_USD * ALERT_THRESHOLD_PCT
    if month_total_usd >= alert_threshold:
        if not _alert_already_sent(month_key):
            pct = (month_total_usd / MONTHLY_BUDGET_USD) * 100
            alert_msg = (
                f'⚠️ *Budget Alert — {pct:.0f}% used*\n'
                f'Monthly spend has reached ${month_total_usd:.2f} '
                f'(£{month_total_gbp:.2f}) of ${MONTHLY_BUDGET_USD:.0f} budget.\n'
                f'Remaining: ${remaining_usd:.2f}'
            )
            print(f'[cost-summary] BUDGET ALERT: {pct:.0f}% used (${month_total_usd:.2f})')
            if send:
                _telegram_send(alert_msg)
                _mark_alert_sent(month_key)

    return report


def budget_check_only() -> None:
    """Just check if alert threshold crossed — no daily summary."""
    month_start, month_end = _month_range()
    now = datetime.datetime.now(datetime.timezone.utc)
    month_key = now.strftime('%Y-%m')

    month_raw = query_usage(month_start, month_end)
    month_total_usd = sum(v['cost_usd'] for v in month_raw.values())
    month_total_gbp = month_total_usd * GBP_PER_USD
    remaining_usd = max(0.0, MONTHLY_BUDGET_USD - month_total_usd)
    alert_threshold = MONTHLY_BUDGET_USD * ALERT_THRESHOLD_PCT

    print(f'[cost-summary] MTD: ${month_total_usd:.4f} / ${MONTHLY_BUDGET_USD:.0f} '
          f'(threshold: ${alert_threshold:.0f})')

    if month_total_usd >= alert_threshold and not _alert_already_sent(month_key):
        pct = (month_total_usd / MONTHLY_BUDGET_USD) * 100
        alert_msg = (
            f'⚠️ *Budget Alert — {pct:.0f}% used*\n'
            f'Monthly spend has reached ${month_total_usd:.2f} '
            f'(£{month_total_gbp:.2f}) of ${MONTHLY_BUDGET_USD:.0f} budget.\n'
            f'Remaining: ${remaining_usd:.2f}'
        )
        _telegram_send(alert_msg)
        _mark_alert_sent(month_key)
        print('[cost-summary] Budget alert sent.')
    else:
        print('[cost-summary] Under threshold or alert already sent for this month.')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='LokiVault Daily Cost Summary')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--summary', action='store_true',
                       help='Send daily summary via Telegram')
    group.add_argument('--budget-check', action='store_true',
                       help='Only send alert if 90%% threshold exceeded')
    group.add_argument('--print', action='store_true', dest='print_only',
                       help='Print report without sending')
    args = parser.parse_args()

    if args.summary:
        build_report(send=True)
    elif args.budget_check:
        budget_check_only()
    elif args.print_only:
        build_report(send=False)


if __name__ == '__main__':
    main()
