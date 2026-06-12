#!/usr/bin/env python3
"""
OpenClaw Daily Cost Summary
-----------------------------
Queries the Manifest SQLite DB for yesterday's token usage and cost,
tracks running balances for Anthropic (calculated) and DeepSeek (API),
and alerts when either drops below $2.

Usage:
    daily_cost.py --summary          # send daily summary + update balances (05:00 cron)
    daily_cost.py --budget-check     # only alert if balance < $2 (hourly cron)
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
DB_PATH = os.environ.get('MANIFEST_DB_PATH', '/opt/openclaw/config/manifest/manifest.db')
BALANCE_STATE_PATH = os.environ.get(
    'BALANCE_STATE_PATH',
    '/home/node/.openclaw/workspace/data/balances.json',
)
ALERT_SENT_FLAG = os.environ.get(
    'ALERT_SENT_FLAG',
    '/home/node/.openclaw/workspace/data/cost-alert-sent.json',
)
LOW_BALANCE_THRESHOLD = 2.00
GBP_PER_USD = 0.79
JOB_ID_SUMMARY = 'cf60889b-2312-470f-9c8e-bd63b69e9792'
JOB_ID_BUDGET_CHECK = '58570823-38b6-4238-a98b-e3648a2d7ca2'

# Seed: known Anthropic starting balance as of 2026-06-12
ANTHROPIC_SEED_USD = 6.87
ANTHROPIC_SEED_DATE = '2026-06-12'


def _write_job_status(job_id: str, job_name: str) -> None:
    path = f'/home/node/.openclaw/workspace/data/job-status/{job_id}.json'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump({
            'status': 'ok',
            'job': job_name,
            'completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, f)


def _load_secrets() -> dict:
    for path in [
        os.path.expanduser('~/.openclaw/secrets.json'),
        '/opt/openclaw/secrets.json',
    ]:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_token() -> str:
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if token:
        return token
    secrets = _load_secrets()
    return secrets.get('TELEGRAM_TOKEN', '')


BOT_TOKEN = _load_token()
CHAT_ID = '1163684840'

MODEL_NAMES = {
    'deepseek-chat':                    'DeepSeek V3.2',
    'deepseek/deepseek-chat':           'DeepSeek V3.2',
    'deepseek-reasoner':                'DeepSeek R1',
    'deepseek/deepseek-reasoner':       'DeepSeek R1',
    'claude-sonnet-4-6':                'Claude Sonnet',
    'claude-sonnet-4-5':                'Claude Sonnet',
    'anthropic/claude-sonnet-4-6':      'Claude Sonnet',
}

MODEL_COSTS = {
    'deepseek-chat':                    {'input': 2.8e-7,  'output': 4.2e-7,  'cache_read': 2.8e-8},
    'deepseek/deepseek-chat':           {'input': 2.8e-7,  'output': 4.2e-7,  'cache_read': 2.8e-8},
    'deepseek-reasoner':                {'input': 5.5e-7,  'output': 2.19e-6, 'cache_read': 5.5e-8},
    'deepseek/deepseek-reasoner':       {'input': 5.5e-7,  'output': 2.19e-6, 'cache_read': 5.5e-8},
    'claude-sonnet-4-6':                {'input': 3.0e-6,  'output': 1.5e-5,  'cache_read': 3.0e-7},
    'anthropic/claude-sonnet-4-6':      {'input': 3.0e-6,  'output': 1.5e-5,  'cache_read': 3.0e-7},
}

MODEL_ORDER = ['DeepSeek V3.2', 'DeepSeek R1', 'Claude Sonnet']


# ---------------------------------------------------------------------------
# Balance state
# ---------------------------------------------------------------------------

def _load_balances() -> dict:
    if os.path.exists(BALANCE_STATE_PATH):
        try:
            with open(BALANCE_STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'anthropic': {
            'balance_usd': ANTHROPIC_SEED_USD,
            'last_deducted_date': ANTHROPIC_SEED_DATE,
        },
        'deepseek': {
            'balance_usd': None,
            'last_synced': None,
        },
    }


def _save_balances(state: dict) -> None:
    os.makedirs(os.path.dirname(BALANCE_STATE_PATH), exist_ok=True)
    with open(BALANCE_STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# DeepSeek balance API
# ---------------------------------------------------------------------------

def _fetch_deepseek_balance() -> float | None:
    secrets = _load_secrets()
    key = secrets.get('DEEPSEEK_KEY', '')
    if not key:
        print('[cost-summary] DEEPSEEK_KEY not found in secrets')
        return None
    try:
        req = urllib.request.Request(
            'https://api.deepseek.com/user/balance',
            headers={'Accept': 'application/json', 'Authorization': f'Bearer {key}'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        for info in data.get('balance_infos', []):
            if info.get('currency') == 'USD':
                return float(info.get('topped_up_balance', 0))
    except Exception as e:
        print(f'[cost-summary] DeepSeek balance fetch error: {e}')
    return None


# ---------------------------------------------------------------------------
# Manifest DB helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f'Manifest DB not found at {DB_PATH}')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _yesterday_range() -> tuple[str, str]:
    now = datetime.datetime.now(datetime.timezone.utc)
    yesterday = now - datetime.timedelta(days=1)
    start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


def _month_range() -> tuple[str, str]:
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end_base = now.replace(year=now.year + 1, month=1, day=1,
                               hour=0, minute=0, second=0, microsecond=0)
    else:
        end_base = now.replace(month=now.month + 1, day=1,
                               hour=0, minute=0, second=0, microsecond=0)
    end = end_base - datetime.timedelta(seconds=1)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')


def _calc_cost(row_model: str, input_tok: int, output_tok: int, cache_read: int,
               db_cost: float) -> float:
    if db_cost and db_cost > 0:
        return db_cost
    rates = MODEL_COSTS.get(row_model, {})
    return (input_tok * rates.get('input', 0) +
            output_tok * rates.get('output', 0) +
            cache_read * rates.get('cache_read', 0))


def query_usage(start: str, end: str) -> dict:
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
    out = {}
    for model_key, data in usage.items():
        friendly = MODEL_NAMES.get(model_key, model_key)
        if friendly not in out:
            out[friendly] = {'tokens': 0, 'input_tokens': 0, 'output_tokens': 0,
                             'cache_read_tokens': 0, 'cost_usd': 0.0}
        for k in ('tokens', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cost_usd'):
            out[friendly][k] += data.get(k, 0)
    return out


def _is_anthropic_model(model_key: str) -> bool:
    return 'claude' in model_key.lower()


def _anthropic_spend(usage_raw: dict) -> float:
    """Sum cost_usd for all Anthropic (Claude) models in a raw usage dict."""
    return sum(v['cost_usd'] for k, v in usage_raw.items() if _is_anthropic_model(k))


# ---------------------------------------------------------------------------
# Alert state (avoid duplicate low-balance alerts)
# ---------------------------------------------------------------------------

def _load_alert_state() -> dict:
    if os.path.exists(ALERT_SENT_FLAG):
        try:
            with open(ALERT_SENT_FLAG) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_alert_state(state: dict) -> None:
    os.makedirs(os.path.dirname(ALERT_SENT_FLAG), exist_ok=True)
    with open(ALERT_SENT_FLAG, 'w') as f:
        json.dump(state, f, indent=2)


def _alert_already_sent(key: str) -> bool:
    return _load_alert_state().get(key, False)


def _mark_alert_sent(key: str) -> None:
    state = _load_alert_state()
    state[key] = True
    _save_alert_state(state)


def _clear_alert(key: str) -> None:
    """Clear an alert flag once balance recovers above threshold."""
    state = _load_alert_state()
    if key in state:
        del state[key]
        _save_alert_state(state)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _telegram_send(text: str) -> bool:
    if not BOT_TOKEN:
        print('[cost-summary] TELEGRAM_TOKEN not set')
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
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
            return result.get('ok', False)
    except Exception as e:
        print(f'[cost-summary] Telegram error: {e}')
        return False


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f'{n/1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n/1_000:.1f}k'
    return str(n)


def _balance_bar(balance: float, capacity: float = 10.0) -> str:
    """Simple text indicator: ██░░░░░░░░ style."""
    pct = min(1.0, max(0.0, balance / capacity))
    filled = round(pct * 10)
    return '█' * filled + '░' * (10 - filled)


# ---------------------------------------------------------------------------
# Balance update (run once per day in --summary)
# ---------------------------------------------------------------------------

def update_balances(yesterday_usage_raw: dict) -> dict:
    """
    Deduct yesterday's Anthropic spend from running balance,
    sync DeepSeek balance from API.
    Returns updated balance state.
    """
    state = _load_balances()
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    yesterday_str = (now_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    # Anthropic: deduct yesterday's spend if not already done
    anthropic_info = state.setdefault('anthropic', {
        'balance_usd': ANTHROPIC_SEED_USD,
        'last_deducted_date': ANTHROPIC_SEED_DATE,
    })
    if anthropic_info.get('last_deducted_date') != yesterday_str:
        spend = _anthropic_spend(yesterday_usage_raw)
        anthropic_info['balance_usd'] = max(0.0, float(anthropic_info.get('balance_usd', 0)) - spend)
        anthropic_info['last_deducted_date'] = yesterday_str
        print(f'[cost-summary] Anthropic balance: deducted ${spend:.4f}, now ${anthropic_info["balance_usd"]:.4f}')
    else:
        print(f'[cost-summary] Anthropic balance already updated for {yesterday_str}')

    # DeepSeek: fetch real balance from API
    deepseek_info = state.setdefault('deepseek', {'balance_usd': None, 'last_synced': None})
    ds_balance = _fetch_deepseek_balance()
    if ds_balance is not None:
        deepseek_info['balance_usd'] = ds_balance
        deepseek_info['last_synced'] = now_date.strftime('%Y-%m-%d')
        print(f'[cost-summary] DeepSeek balance synced: ${ds_balance:.4f}')
    else:
        print('[cost-summary] DeepSeek balance sync failed — keeping last known value')

    _save_balances(state)
    return state


# ---------------------------------------------------------------------------
# Low balance alerts
# ---------------------------------------------------------------------------

def check_low_balance_alerts(balances: dict, send: bool = True) -> list[str]:
    """Check both balances and fire alerts if below $2. Returns list of alert messages sent."""
    alerts_sent = []
    today_key = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')

    providers = [
        ('anthropic', 'Anthropic', balances.get('anthropic', {}).get('balance_usd')),
        ('deepseek', 'DeepSeek', balances.get('deepseek', {}).get('balance_usd')),
    ]

    for key, label, balance in providers:
        if balance is None:
            continue
        alert_key = f'low_balance_{key}_{today_key}'
        if balance < LOW_BALANCE_THRESHOLD:
            if not _alert_already_sent(alert_key):
                msg = (
                    f'⚠️ *{label} Low Balance*\n'
                    f'Balance: ${balance:.2f} (£{balance * GBP_PER_USD:.2f})\n'
                    f'Threshold: ${LOW_BALANCE_THRESHOLD:.2f}'
                )
                print(f'[cost-summary] LOW BALANCE ALERT: {label} ${balance:.2f}')
                if send:
                    if _telegram_send(msg):
                        _mark_alert_sent(alert_key)
                alerts_sent.append(msg)
        else:
            # Balance recovered — clear the flag so alert fires again if it dips
            _clear_alert(alert_key)

    return alerts_sent


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(send: bool = True, update_balance: bool = False) -> str:
    yesterday_start, yesterday_end = _yesterday_range()
    month_start, month_end = _month_range()
    now = datetime.datetime.now(datetime.timezone.utc)
    yesterday_dt = now - datetime.timedelta(days=1)
    date_str = yesterday_dt.strftime('%d %b %Y')

    yesterday_raw = query_usage(yesterday_start, yesterday_end)
    month_raw = query_usage(month_start, month_end)

    yesterday_by_model = group_by_friendly(yesterday_raw)
    month_by_model = group_by_friendly(month_raw)

    yesterday_total_usd = sum(v['cost_usd'] for v in yesterday_by_model.values())
    month_total_usd = sum(v['cost_usd'] for v in month_by_model.values())
    yesterday_total_gbp = yesterday_total_usd * GBP_PER_USD

    # Update balances if this is the daily summary run
    if update_balance:
        balances = update_balances(yesterday_raw)
    else:
        balances = _load_balances()

    anthropic_bal = balances.get('anthropic', {}).get('balance_usd', ANTHROPIC_SEED_USD)
    deepseek_bal = balances.get('deepseek', {}).get('balance_usd')
    deepseek_synced = balances.get('deepseek', {}).get('last_synced', 'never')

    # Per-model lines for yesterday
    model_lines = []
    for name in MODEL_ORDER:
        data = yesterday_by_model.get(name, {})
        cost = data.get('cost_usd', 0.0)
        toks = data.get('tokens', 0)
        if cost > 0 or toks > 0:
            model_lines.append(f'{name}: ${cost:.4f} ({_fmt_tokens(toks)} tokens)')
        else:
            model_lines.append(f'{name}: $0.0000 (0 tokens)')

    model_section = '\n'.join(model_lines)

    # Balance lines
    deepseek_bal_str = (
        f'${deepseek_bal:.2f} (£{deepseek_bal * GBP_PER_USD:.2f}) — synced {deepseek_synced}'
        if deepseek_bal is not None else 'unknown'
    )
    anthropic_bal_str = f'${anthropic_bal:.2f} (£{anthropic_bal * GBP_PER_USD:.2f}) — calculated'

    # Low balance warnings inline
    low_warnings = []
    if anthropic_bal < LOW_BALANCE_THRESHOLD:
        low_warnings.append(f'⚠️ Anthropic balance low: ${anthropic_bal:.2f}')
    if deepseek_bal is not None and deepseek_bal < LOW_BALANCE_THRESHOLD:
        low_warnings.append(f'⚠️ DeepSeek balance low: ${deepseek_bal:.2f}')
    warning_section = ('\n' + '\n'.join(low_warnings)) if low_warnings else ''

    report = (
        f'📊 *Daily AI Cost Summary*\n'
        f'Date: {date_str}\n\n'
        f'{model_section}\n\n'
        f"Yesterday: ${yesterday_total_usd:.4f} (£{yesterday_total_gbp:.2f})\n"
        f'Month to date: ${month_total_usd:.4f}\n\n'
        f'💳 *Balances*\n'
        f'Anthropic: {anthropic_bal_str}\n'
        f'DeepSeek: {deepseek_bal_str}'
        f'{warning_section}'
    )

    print(report)

    if send:
        ok = _telegram_send(report)
        print(f'[cost-summary] Daily summary sent: {ok}')
        if ok:
            _write_job_status(JOB_ID_SUMMARY, 'Daily Cost Summary')

    if update_balance:
        check_low_balance_alerts(balances, send=send)

    return report


# ---------------------------------------------------------------------------
# Budget check (hourly cron — just check stored balances, no API call)
# ---------------------------------------------------------------------------

def budget_check_only() -> None:
    balances = _load_balances()
    anthropic_bal = balances.get('anthropic', {}).get('balance_usd', ANTHROPIC_SEED_USD)
    deepseek_bal = balances.get('deepseek', {}).get('balance_usd')

    print(f'[cost-summary] Anthropic balance: ${anthropic_bal:.4f}')
    if deepseek_bal is not None:
        print(f'[cost-summary] DeepSeek balance: ${deepseek_bal:.4f}')

    alerts = check_low_balance_alerts(balances, send=True)
    if alerts:
        print(f'[cost-summary] {len(alerts)} low balance alert(s) sent.')
    else:
        print('[cost-summary] Balances OK — no alerts needed.')

    # Budget check success = ran cleanly, regardless of whether an alert was sent
    _write_job_status(JOB_ID_BUDGET_CHECK, 'Budget Alert Check')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='OpenClaw Daily Cost Summary')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--summary', action='store_true',
                       help='Send daily summary via Telegram and update balances')
    group.add_argument('--budget-check', action='store_true',
                       help='Only send alert if balance < $2 (uses stored values)')
    group.add_argument('--print', action='store_true', dest='print_only',
                       help='Print report without sending or updating balances')
    args = parser.parse_args()

    if args.summary:
        build_report(send=True, update_balance=True)
    elif args.budget_check:
        budget_check_only()
    elif args.print_only:
        build_report(send=False, update_balance=False)


if __name__ == '__main__':
    main()
