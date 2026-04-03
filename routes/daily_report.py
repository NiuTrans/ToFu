"""routes/daily_report.py — Daily task-centric report with LLM analysis.

Generates LLM-powered task analysis for conversations.  Persists reports
to ``~/.chatui/daily_reports/YYYY-MM-DD.json`` so past days are cached.

Endpoints:
  POST /api/daily-report              — Analyse today (or specified date)
  GET  /api/daily-report/<date>       — Load cached report
  POST /api/daily-report/backfill/<date> — Server-side backfill for a past day
  GET  /api/daily-report/calendar/<year>/<month> — Month overview (tasks + cost)

Background:
  _start_report_scheduler() — daemon thread that auto-backfills yesterday if
  the report is missing, checked on server boot and daily at midnight+1h.
"""

import datetime as _dt
import json
import os
import random
import re
import threading
import time

from flask import Blueprint, jsonify, request

from lib.log import get_logger
from routes.common import DEFAULT_USER_ID, _db_safe

logger = get_logger(__name__)

daily_report_bp = Blueprint('daily_report', __name__)

# ── Report storage ──────────────────────────────────────────
from lib.config_dir import config_path as _config_path

_REPORTS_DIR = _config_path('daily_reports')
os.makedirs(_REPORTS_DIR, exist_ok=True)

# ── Active generation jobs ──────────────────────────────────
_active_jobs = {}     # date_str → {status, progress, error, started_at}
_jobs_lock = threading.Lock()

# ── Calendar endpoint TTL cache (avoids 5s+ repeated full-table scans) ──
_calendar_cache = {}   # (year, month) → {'data': response_dict, 'ts': monotonic}
_CALENDAR_CACHE_TTL = 30  # seconds


def _update_job(date_str, status, progress=None, error=None):
    """Thread-safe update of background generation job status."""
    with _jobs_lock:
        if date_str not in _active_jobs:
            _active_jobs[date_str] = {'started_at': time.time()}
        job = _active_jobs[date_str]
        job['status'] = status
        if progress is not None:
            job['progress'] = progress
        if error is not None:
            job['error'] = error


def _get_job(date_str):
    """Thread-safe read of job status.  Returns dict copy or None."""
    with _jobs_lock:
        job = _active_jobs.get(date_str)
        return dict(job) if job else None


def _clear_job(date_str):
    """Remove finished job from tracking dict."""
    with _jobs_lock:
        _active_jobs.pop(date_str, None)


def _report_path(date_str):
    """File path for a daily report.  date_str = 'YYYY-MM-DD'."""
    return os.path.join(_REPORTS_DIR, f'{date_str}.json')


def _save_report(date_str, report_data):
    """Persist a daily report to disk."""
    try:
        payload = dict(report_data)
        payload['date'] = date_str
        payload['generated_at'] = int(time.time() * 1000)
        for k in ('ok', 'error'):
            payload.pop(k, None)
        with open(_report_path(date_str), 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        n = len(payload.get('streams', payload.get('tasks', [])))
        logger.info('[DailyReport] Saved report for %s (%d items)', date_str, n)
        # Invalidate calendar cache for this month so fresh data appears
        try:
            parts = date_str.split('-')
            cache_key = (int(parts[0]), int(parts[1]))
            _calendar_cache.pop(cache_key, None)
        except (ValueError, IndexError) as e:
            logger.debug('[DailyReport] Cache key parse failed for %s: %s', date_str, e)
    except Exception as e:
        logger.error('[DailyReport] Failed to save %s: %s', date_str, e, exc_info=True)


def _load_report(date_str):
    """Load a cached report.  Returns dict or None.

    Handles both legacy per-conversation format (tasks) and new
    work-stream format (streams).
    """
    path = _report_path(date_str)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            report = json.load(f)
        # Normalize stream statuses
        for s in report.get('streams', []):
            if s.get('status') not in ('done', 'in_progress', 'blocked'):
                s['status'] = 'in_progress'
        # Normalize legacy per-task statuses
        for t in report.get('tasks', []):
            if t.get('status') not in ('done', 'incomplete'):
                t['status'] = 'incomplete'
        return report
    except Exception as e:
        logger.warning('[DailyReport] Failed to load %s: %s', date_str, e)
        return None


# ── LLM system prompt ──────────────────────────────────────

_ANALYSIS_SYSTEM = """\
You are a work journal assistant. Analyse the user's AI conversations and
produce a concise daily summary. Output a JSON OBJECT (not array) with THREE keys:

{
  "streams": [ ... work stream objects ... ],
  "tomorrow": [ ... todo objects ... ],
  "yesterday_done": [ "exact text of completed yesterday TODO 1", ... ]
}

═══ STREAMS ═══
Group related conversations into coherent work areas (5-15 clusters).

Each stream:
{
  "title": "specific title, max 20 chars",
  "summary": "ONE sentence: the key outcome or what happened today",
  "status": "done" | "in_progress" | "blocked",
  "conv_ids": ["exact conversation IDs from input"]
}

Summary rules:
- ONE sentence only. Concise. Not "discussed X" but "fixed X" / "identified root cause of X".
- If blocked, the summary should say WHY it's blocked.
- Trivial quick Q&A can merge into one "零碎问答" stream.

═══ TOMORROW ═══
Synthesize 3-8 TODO items from ALL unfinished work across streams.
Each item is a JSON OBJECT with three keys:
{
  "text": "short actionable title (max 30 chars, specific)",
  "detail": "A concrete, actionable prompt (1-3 sentences) that can be sent directly to an AI assistant to start this task. Include specific file names, function names, error messages, module paths, or other context from today's conversations so the assistant can immediately understand the task.",
  "tools": ["search"]  // subset of: search, code, browser, fetch, project
}

Available tool names:
- "search"  — web search (for research, looking up docs/APIs/errors)
- "code"    — code execution (for running scripts, testing, data analysis)
- "browser" — browser automation (for web interaction, testing UIs)
- "fetch"   — URL fetching (for reading specific web pages / docs)
- "project" — code project co-pilot (for editing files, reading code, debugging)

Title rules:
Bad: "继续图像相关工作" → Good: "修复多轮图片回显"
Bad: "处理评测问题" → Good: "适配M17评测脚本"

Detail rules:
- Write as if you're giving a brief to an AI coding assistant
- Reference specific files, functions, error messages, or URLs from today's work
- Be concrete: "Fix the _build_transcript function in routes/daily_report.py that truncates CJK text" not "Fix the truncation bug"

Yesterday's unfinished items are automatically categorized as "未完成".
If an unfinished item is still relevant and worth continuing tomorrow,
you SHOULD re-add it to tomorrow.  But don't blindly copy all items —
only include ones that are genuinely actionable for the next day.
If everything is done today, return an empty array.

═══ YESTERDAY_DONE ═══
If the input includes a "YESTERDAY'S TODO STATUS" section:
- Review each ✗ item and check if today's work (visible in streams) addressed it.
- If a TODO was clearly worked on today, include its EXACT original text in
  the "yesterday_done" array (copy-paste the text after the ✗ marker).
- Only include items that were actually worked on. If not addressed, omit.
- If no yesterday TODOs exist, return an empty array.

═══ RULES ═══
- All text in the SAME language as the user (Chinese → Chinese)
- Return ONLY the raw JSON object. No markdown fences, no explanation.
"""

# ── Tool mapping for quick_action generation ──────────────
_TODO_TOOL_DEFAULTS = {
    'searchMode': 'off',
    'fetchEnabled': True,  # always on
    'codeExecEnabled': False,
    'browserEnabled': False,
    'projectEnabled': False,
}
_TODO_TOOL_MAP = {
    'search':  {'searchMode': 'multi'},
    'code':    {'codeExecEnabled': True},
    'browser': {'browserEnabled': True},
    'fetch':   {},  # fetchEnabled always on, no override needed
    'project': {'projectEnabled': True},
}

_QUOTES = [
    "人生苦短，我用 AI。",
    "今天的你比昨天更会提问了 ✨",
    "每一次对话都是一次思维的升级。",
    "AI 是工具，你才是灵魂。",
    "效率 × 创造力 = 你 + Tofu 🧈",
    "你和 AI 的默契度又提升了 1 级。",
    "Knowledge is power, and you're charging up. ⚡",
    "Done is better than perfect.",
    "Ship it. 🚀",
]


# ═════════════════════════════════════════════════════════════
#  Server-side cost calculation
# ═════════════════════════════════════════════════════════════

# Legacy preset → model_id migration table (mirrors core.js _LEGACY_PRESET_TO_MODEL)
_LEGACY_PRESET_TO_MODEL = {
    'qwen': 'qwen3.5-plus', 'low': 'qwen3.5-plus',
    'gemini': 'gemini-3.1-flash-lite-preview', 'gemini_flash': 'gemini-3-flash-preview',
    'minimax': 'MiniMax-M2.7', 'doubao': 'Doubao-Seed-2.0-pro',
    'opus': 'aws.claude-opus-4.6',
    'medium': 'aws.claude-opus-4.6', 'high': 'aws.claude-opus-4.6', 'max': 'aws.claude-opus-4.6',
}


def _qwen_cny(tokens, tok_type, model_id=''):
    """Qwen tiered CNY pricing — mirrors core.js _qwenCny().

    Args:
        tokens: Token count.
        tok_type: 'input' or 'output'.
        model_id: Model identifier for per-model tier lookup.

    Returns:
        Cost in CNY.
    """
    from lib import QWEN_PRICING_CNY
    # Per-model tiers: lookup model, fallback to '_default'
    model_tiers = QWEN_PRICING_CNY.get(model_id) or QWEN_PRICING_CNY.get('_default', {})
    tiers = model_tiers.get(tok_type, [])
    for max_tokens, price_per_1m in tiers:
        if tokens <= max_tokens:
            return tokens * price_per_1m / 1e6
    # Beyond last tier — use last tier's price
    if tiers:
        return tokens * tiers[-1][1] / 1e6
    return 0.0


def _calc_msg_cost_cny(usage, model_or_preset=''):
    """Calculate cost in CNY for a single message's usage dict.

    This is a faithful Python port of the frontend ``calcCostCny()``
    in ``core.js``, using the same MODEL_PRICING table and logic.

    Args:
        usage: Token usage dict (prompt_tokens, completion_tokens, etc.).
        model_or_preset: Model ID or legacy preset key.

    Returns:
        Cost in CNY (float), or 0.0 if no tokens.
    """
    if not usage:
        return 0.0

    from lib import DEFAULT_USD_CNY_RATE, MODEL_PRICING
    from lib.pricing import get_pricing_data

    # Resolve legacy preset
    model_id = model_or_preset or ''
    model_id = _LEGACY_PRESET_TO_MODEL.get(model_id, model_id)

    inp = usage.get('prompt_tokens') or usage.get('input_tokens') or 0
    out = usage.get('completion_tokens') or usage.get('output_tokens') or 0
    cache_write = usage.get('cache_write_tokens') or usage.get('cache_creation_input_tokens') or 0
    cache_read = usage.get('cache_read_tokens') or usage.get('cache_read_input_tokens') or 0
    think_tok = usage.get('reasoning_tokens') or usage.get('thinking_tokens') or 0
    if think_tok > 0 and out == 0:
        out = think_tok
    if inp == 0 and out == 0 and cache_write == 0 and cache_read == 0:
        return 0.0

    # Get live exchange rate from pricing module
    pricing_data = get_pricing_data()
    rate = pricing_data.get('usdToCny') or DEFAULT_USD_CNY_RATE

    # ── Qwen tiered pricing (CNY-native) ──
    if re.search(r'qwen', model_id, re.IGNORECASE):
        inp_cny = _qwen_cny(inp, 'input', model_id)
        out_cny = _qwen_cny(out, 'output', model_id)
        return round(inp_cny + out_cny, 4)

    # ── Generic USD pricing from MODEL_PRICING table ──
    base_in = pricing_data.get('inputPrice', 15.0)
    out_p = pricing_data.get('outputPrice', 75.0)
    cw_mul = 1.25
    cr_mul = 0.10

    mp = MODEL_PRICING.get(model_id)
    if mp:
        base_in = mp.get('input', 0)
        out_p = mp.get('output', 0)
        if 'cacheWriteMul' in mp:
            cw_mul = mp['cacheWriteMul']
        if 'cacheReadMul' in mp:
            cr_mul = mp['cacheReadMul']

    input_cost_usd = 0.0
    cw_cost_usd = 0.0
    cr_cost_usd = 0.0
    output_cost_usd = out * out_p / 1e6

    if cache_write > 0 or cache_read > 0:
        standard_inp = max(0, inp - cache_write - cache_read)
        input_cost_usd = standard_inp * base_in / 1e6
        cw_cost_usd = cache_write * base_in * cw_mul / 1e6
        cr_cost_usd = cache_read * base_in * cr_mul / 1e6
    else:
        input_cost_usd = inp * base_in / 1e6

    cost_usd = input_cost_usd + cw_cost_usd + cr_cost_usd + output_cost_usd
    return round(cost_usd * rate, 4)


def _get_monthly_costs(year, month):
    """Calculate per-day cost breakdown for an entire month from DB.

    Scans all conversations and their messages, assigns costs to days
    based on message timestamps (with interpolation for old messages
    lacking timestamps, same as the frontend logic).

    Args:
        year: Calendar year (int).
        month: Calendar month 1-12 (int).

    Returns:
        dict mapping day-of-month (int) → {'cost': float, 'conversations': {conv_id: {'name': str, 'cost': float, 'tokens': int}}}.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.utils import safe_json

    t0 = time.monotonic()
    month_start = _dt.date(year, month, 1)
    if month < 12:
        month_end = _dt.date(year, month + 1, 1)
    else:
        month_end = _dt.date(year + 1, 1, 1)
    ms_start = int(_dt.datetime.combine(month_start, _dt.time.min).timestamp() * 1000)
    ms_end = int(_dt.datetime.combine(month_end, _dt.time.min).timestamp() * 1000)

    try:
        db = get_thread_db(DOMAIN_CHAT)
        # SQL-level date filter: only fetch convs updated within or after target month
        # (created_at / updated_at are BIGINT epoch-ms)
        rows = db.execute(
            'SELECT id, title, messages, created_at, updated_at, settings '
            'FROM conversations WHERE user_id=? AND '
            'COALESCE(updated_at, created_at, 0) >= ? '
            'ORDER BY updated_at DESC',
            (DEFAULT_USER_ID, ms_start)
        ).fetchall()
    except Exception as e:
        logger.error('[DailyReport] Monthly cost DB query failed %d-%02d: %s',
                     year, month, e, exc_info=True)
        return {}

    days = {}   # day_num → {cost, conversations}

    for r in rows:
        msgs = safe_json(r['messages'], default=[], label='cost-messages')
        if not isinstance(msgs, list) or not msgs:
            continue

        # Determine conversation-level model
        settings = safe_json(r.get('settings'), default={}, label='cost-settings')
        if not isinstance(settings, dict):
            settings = {}
        conv_model = (settings.get('model') or settings.get('preset')
                      or settings.get('effort') or '')

        conv_start = _safe_int_ts(r['created_at'] or r['updated_at'] or 0)
        conv_end = _safe_int_ts(r['updated_at'] or r['created_at'] or 0)
        total_msgs = len(msgs)
        conv_title = r['title'] or ''
        if not conv_title and msgs:
            first_content = msgs[0].get('content', '')
            if isinstance(first_content, str):
                conv_title = first_content[:30]
        conv_title = conv_title or 'Untitled'
        conv_id = r['id']

        for mi, msg in enumerate(msgs):
            usage = msg.get('usage')
            if not usage:
                continue

            # Timestamp resolution (mirrors frontend logic)
            ts = _safe_int_ts(msg.get('timestamp', 0))
            if not ts:
                if (conv_start and conv_end and conv_start != conv_end
                        and total_msgs > 1):
                    ts = conv_start + int(
                        (conv_end - conv_start) * mi / (total_msgs - 1))
                else:
                    ts = conv_start
            if not ts:
                continue

            # Check if this message falls in the target month
            if ts < ms_start or ts >= ms_end:
                continue

            d = _dt.datetime.fromtimestamp(ts / 1000)
            if d.year != year or d.month != month:
                continue
            day_num = d.day

            # Per-message model (most accurate), fallback to conv-level
            msg_model = (msg.get('model') or msg.get('preset')
                         or msg.get('effort') or conv_model)

            cost_cny = _calc_msg_cost_cny(usage, msg_model)
            if cost_cny <= 0:
                continue

            if day_num not in days:
                days[day_num] = {'cost': 0.0, 'conversations': {}}
            days[day_num]['cost'] += cost_cny

            if conv_id not in days[day_num]['conversations']:
                days[day_num]['conversations'][conv_id] = {
                    'name': conv_title,
                    'cost': 0.0,
                    'tokens': 0,
                }
            entry = days[day_num]['conversations'][conv_id]
            entry['cost'] += cost_cny
            entry['tokens'] += (
                (usage.get('input_tokens') or usage.get('prompt_tokens') or 0) +
                (usage.get('output_tokens') or usage.get('completion_tokens') or 0))

    # Round final numbers
    for day_data in days.values():
        day_data['cost'] = round(day_data['cost'], 4)
        for conv_entry in day_data['conversations'].values():
            conv_entry['cost'] = round(conv_entry['cost'], 4)

    elapsed = time.monotonic() - t0
    total_cost = sum(d['cost'] for d in days.values())
    logger.info('[DailyReport] Monthly costs %d-%02d: %d days with costs, '
                '¥%.2f total (%.1fs)',
                year, month, len(days), total_cost, elapsed)
    return days


def _normalize_todo_text(text):
    """Normalize TODO text for dedup comparison.

    Strips whitespace, punctuation, and lowercases to detect near-duplicates
    like '修复图片回显' vs '修复图片回显问题'.
    """
    return re.sub(r'[\s\W]+', '', text.strip().lower())


def _fuzzy_todo_match(text_a, text_b, threshold=0.35):
    """Check if two TODO texts are similar enough to be considered duplicates.

    Uses multiple signals: exact/substring match, character-set Jaccard,
    and LCS ratio.  Short Chinese texts are hard to compare, so we combine
    metrics: if ANY metric exceeds its threshold, it's a match.

    Also used by _mark_yesterday_todos_done to fuzzy-match LLM output
    against stored TODO texts.
    """
    norm_a = _normalize_todo_text(text_a)
    norm_b = _normalize_todo_text(text_b)
    if not norm_a or not norm_b:
        return False
    # Fast path: exact or substring
    if norm_a == norm_b:
        return True
    if len(norm_a) > 3 and len(norm_b) > 3:
        if norm_a in norm_b or norm_b in norm_a:
            return True
    # Character-set Jaccard (good for shuffled words)
    set_a, set_b = set(norm_a), set(norm_b)
    char_jaccard = len(set_a & set_b) / len(set_a | set_b) if set_a | set_b else 0
    if char_jaccard >= threshold:
        return True
    # LCS ratio (good for paraphrased but sequentially similar texts)
    m, n = len(norm_a), len(norm_b)
    if m > 0 and n > 0:
        prev = [0] * (n + 1)
        for i in range(1, m + 1):
            cur = [0] * (n + 1)
            for j in range(1, n + 1):
                if norm_a[i - 1] == norm_b[j - 1]:
                    cur[j] = prev[j - 1] + 1
                else:
                    cur[j] = max(prev[j], cur[j - 1])
            prev = cur
        lcs_ratio = prev[n] / max(m, n)
        if lcs_ratio >= threshold:
            return True
    return False


# ═════════════════════════════════════════════════════════════
#  Core: analyse a list of conversation digests
# ═════════════════════════════════════════════════════════════

def _get_yesterday_carryover(target_date):
    """Load yesterday's unfinished TODO items and blocked streams.

    Returns a list of short carryover strings for LLM context.
    """
    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = _load_report(yesterday)
        if not prev:
            return []
        items = []
        # Yesterday's TODO items that weren't checked off
        for todo in prev.get('tomorrow', []):
            if not todo.get('done') and todo.get('text'):
                items.append(todo['text'])
        # Blocked/in-progress stream titles
        for s in prev.get('streams', []):
            if s.get('status') in ('in_progress', 'blocked'):
                items.append(s.get('title', ''))
        return [x for x in items if x.strip()]
    except Exception as e:
        logger.debug('[DailyReport] Carryover load failed: %s', e)
        return []


def _get_today_inherited_todos(target_date):
    """Load yesterday's unfinished TODO items as structured dicts for display.

    These are items from the previous day's ``tomorrow[]`` that haven't
    been checked off.  They appear in the current day's "今日待办" section.

    Returns list of dicts: [{id, text, done, _inherited, _origin_date}, ...].
    """
    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = _load_report(yesterday)
        if not prev:
            return []
        items = []
        for todo in prev.get('tomorrow', []):
            if not todo.get('done') and todo.get('text'):
                item = {
                    'id': todo.get('id', ''),
                    'text': todo['text'],
                    'done': False,
                    '_inherited': True,
                    '_origin_date': yesterday,
                }
                # Carry forward quick_action if present
                if todo.get('quick_action'):
                    item['quick_action'] = todo['quick_action']
                items.append(item)
        return items
    except Exception as e:
        logger.debug('[DailyReport] Inherited todos load failed: %s', e)
        return []


def _get_yesterday_todo_accountability(target_date):
    """Load yesterday's TODO items with completion status for LLM context.

    Returns list of (text, done_bool) tuples for the LLM prompt.
    """
    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = _load_report(yesterday)
        if not prev:
            return []
        results = []
        for todo in prev.get('tomorrow', []):
            if todo.get('text'):
                results.append((todo['text'], bool(todo.get('done'))))
        return results
    except Exception as e:
        logger.debug('[DailyReport] Todo accountability load failed: %s', e)
        return []


def _mark_yesterday_todos_done(target_date, yesterday_done, todo_status):
    """Write back completion status to yesterday's report file.

    When the LLM identifies that yesterday's TODO items were addressed
    by today's work, this function marks those items as ``done: True``
    in yesterday's saved report JSON.

    Args:
        target_date: Today's date string 'YYYY-MM-DD'.
        yesterday_done: List of TODO texts the LLM says were completed.
        todo_status: List of (text, done_bool) from yesterday's TODOs
                     (used to find items that were already done).
    """
    if not yesterday_done or not todo_status:
        return

    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = _load_report(yesterday)
        if not prev:
            return

        changed = 0
        for todo in prev.get('tomorrow', []):
            if todo.get('done') and not todo.get('_auto_closed'):
                continue  # genuinely done (manually or by previous analysis)
            todo_text = todo.get('text', '')
            if not todo_text:
                continue
            # Check if LLM flagged this as done (fuzzy match since LLM may
            # slightly alter the text even when asked to copy-paste)
            for done_text in yesterday_done:
                if not isinstance(done_text, str):
                    continue
                if _fuzzy_todo_match(todo_text, done_text):
                    todo['done'] = True
                    todo.pop('_auto_closed', None)  # promote to genuinely done
                    changed += 1
                    logger.debug('[DailyReport] Marked yesterday TODO as done: %s',
                                 todo_text)
                    break

        if changed:
            _save_report(yesterday, prev)
            logger.info('[DailyReport] Wrote back %d completed TODOs to %s',
                        changed, yesterday)
    except Exception as e:
        logger.warning('[DailyReport] Failed to write back yesterday TODOs: %s', e)


def _close_yesterday_remaining_todos(target_date):
    """Close ALL remaining undone TODOs in yesterday's report.

    Once today's report is generated, yesterday's plan is finalized:
    items already marked done by ``_mark_yesterday_todos_done()`` stay done;
    everything else is auto-closed and returned as "unfinished".

    This ensures ``_get_today_inherited_todos()`` returns empty after
    report generation, replacing the ambiguous "今日待办" with a clear
    "未完成" (Unfinished) category.

    On force re-generation, items previously ``_auto_closed`` are
    re-included in the unfinished list (they remain closed).

    Returns:
        List of unfinished item dicts: ``[{text, _origin_date}, ...]``.
    """
    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = _load_report(yesterday)
        if not prev:
            return []

        unfinished = []
        changed = 0
        for todo in prev.get('tomorrow', []):
            todo_text = todo.get('text', '')
            if not todo_text:
                continue
            if todo.get('done') and not todo.get('_auto_closed'):
                continue  # genuinely done — skip
            # Either not yet closed, or previously _auto_closed (re-generation)
            if not todo.get('done'):
                todo['done'] = True
                todo['_auto_closed'] = True
                changed += 1
            # In either case, this is an unfinished item
            uf_item = {
                'text': todo_text,
                '_origin_date': yesterday,
            }
            if todo.get('quick_action'):
                uf_item['quick_action'] = todo['quick_action']
            unfinished.append(uf_item)

        if changed:
            _save_report(yesterday, prev)
            logger.info('[DailyReport] Auto-closed %d remaining TODOs from %s',
                        changed, yesterday)

        return unfinished
    except Exception as e:
        logger.warning('[DailyReport] Failed to close yesterday remaining TODOs: %s', e)
        return []


def _analyse_conversations(convs, target_date):
    """Run LLM analysis on conversation digests → work streams.

    Groups related conversations into 5-15 coherent work streams,
    incorporates yesterday's unfinished items as carryover.

    Returns a complete result dict (streams, carryover, stats, error).
    """
    import uuid as _uuid

    t0 = time.monotonic()
    total_rounds = sum(c.get('rounds', 0) for c in convs)
    stats = {
        'totalConversations': len(convs),
        'totalMessages': sum(c.get('rounds', 0) * 2 for c in convs),
    }
    logger.info('[DailyReport] Starting stream analysis: %d convs, ~%d rounds for %s',
                len(convs), total_rounds, target_date)

    carryover = _get_yesterday_carryover(target_date)

    if not convs:
        logger.info('[DailyReport] No conversations to analyse for %s', target_date)
        # Surface yesterday's carryover as tomorrow items
        tomorrow_items = [
            {'id': f'todo-{_uuid.uuid4().hex[:8]}', 'text': t, 'done': False}
            for t in carryover[:12] if t
        ]
        return {
            'ok': True,
            'streams': [],
            'tomorrow': tomorrow_items,
            'carryover': carryover,
            'tasks': [],
            'quote': random.choice(_QUOTES),
            'persona': _pick_persona(stats),
            'stats': stats,
        }

    # ── Normalize field names ──
    for c in convs:
        if 'conv_id' in c and 'id' not in c:
            c['id'] = c['conv_id']
        if 'tools' in c and 'toolsUsed' not in c:
            c['toolsUsed'] = c['tools']

    # ── Build rich digest for LLM (up to 80 convs) ──
    digest_lines = []
    for i, c in enumerate(convs[:80]):
        cid = c.get('id', '') or str(i)
        parts = [f'[{cid}] {c.get("title", "?")[:80]}']
        parts.append(f'  Rounds: {c.get("rounds", 0)}, '
                     f'Tools: {",".join(c.get("toolsUsed", [])) or "none"}')
        transcript = c.get('transcript', '')
        if transcript:
            # Tighter budget per conv to fit more
            parts.append(f'  {transcript[:400]}')
        digest_lines.append('\n'.join(parts))

    # If >80, add summary of remaining
    overflow = len(convs) - 80
    if overflow > 0:
        digest_lines.append(
            f'\n(... and {overflow} more conversations with similar activity)')

    # ── Carryover context (unfinished streams) ──
    carryover_text = ''
    if carryover:
        co_lines = ['UNFINISHED FROM YESTERDAY:']
        for item in carryover:
            co_lines.append(f'  - {item}')
        carryover_text = '\n'.join(co_lines) + '\n\n'

    # ── TODO accountability (done/undone from yesterday's plan) ──
    todo_status = _get_yesterday_todo_accountability(target_date)
    if todo_status:
        acc_lines = ["YESTERDAY'S TODO STATUS:"]
        for text, done in todo_status:
            marker = '✓' if done else '✗'
            acc_lines.append(f'  {marker} {text}')
        carryover_text += '\n'.join(acc_lines) + '\n\n'

    user_prompt = (
        f'{carryover_text}'
        f'The user had {len(convs)} AI conversations on {target_date}.\n'
        f'Group into work streams and synthesize tomorrow TODOs.\n\n'
        + '\n'.join(digest_lines)
    )

    logger.info('[DailyReport] Calling LLM for %s (%d convs, %d carryover, ~%d chars)',
                target_date, len(convs), len(carryover), len(user_prompt))

    raw_streams, raw_tomorrow, raw_yesterday_done, error_msg = _run_llm_analysis(
        user_prompt, len(convs))

    # ── Write back yesterday's completion status ──
    _mark_yesterday_todos_done(target_date, raw_yesterday_done, todo_status)

    # ── Close remaining yesterday TODOs → "unfinished" category ──
    # Once today's report is generated, yesterday's undone items are finalized
    # as "未完成" instead of lingering as "今日待办".
    unfinished = _close_yesterday_remaining_todos(target_date)

    # ── Post-process streams ──
    all_conv_ids = {str(c.get('id', '')) for c in convs}
    final_streams = []
    claimed_ids = set()
    conv_map = {str(c.get('id', '')): c for c in convs}

    for s in raw_streams:
        stream = {
            'id': f'stream-{_uuid.uuid4().hex[:8]}',
            'title': s.get('title', '(未命名)'),
            'summary': s.get('summary', ''),
            'status': s.get('status', 'in_progress'),
            'conv_ids': [],
            'conv_count': 0,
        }
        # Normalize status
        if stream['status'] not in ('done', 'in_progress', 'blocked'):
            stream['status'] = 'in_progress'

        # Validate conv_ids
        raw_ids = s.get('conv_ids', [])
        if isinstance(raw_ids, list):
            valid_ids = [str(cid) for cid in raw_ids if str(cid) in all_conv_ids]
            stream['conv_ids'] = valid_ids
            claimed_ids.update(valid_ids)

        stream['conv_count'] = len(stream['conv_ids'])
        final_streams.append(stream)

    # ── Handle unclaimed conversations ──
    unclaimed = all_conv_ids - claimed_ids
    if unclaimed and len(unclaimed) >= 2:
        unc_convs = [conv_map[cid] for cid in unclaimed if cid in conv_map]
        final_streams.append({
            'id': f'stream-{_uuid.uuid4().hex[:8]}',
            'title': '零碎问答',
            'summary': f'{len(unc_convs)} 个独立对话',
            'status': 'done',
            'conv_ids': list(unclaimed),
            'conv_count': len(unc_convs),
        })
    elif unclaimed:
        for uid in unclaimed:
            if final_streams:
                final_streams[-1]['conv_ids'].append(uid)
                final_streams[-1]['conv_count'] += 1

    # ── Build tomorrow TODO items (handle both string and dict formats) ──
    tomorrow_items = []
    for i, raw_item in enumerate(raw_tomorrow[:12]):
        text = ''
        detail = ''
        tools = []
        if isinstance(raw_item, str):
            text = raw_item.strip()
        elif isinstance(raw_item, dict):
            text = (raw_item.get('text') or '').strip()
            detail = (raw_item.get('detail') or '').strip()
            tools = raw_item.get('tools', []) or []
            if not isinstance(tools, list):
                tools = []
        if not text:
            continue
        item = {
            'id': f'todo-{_uuid.uuid4().hex[:8]}',
            'text': text[:60],
            'done': False,
        }
        # Build quick_action for launching a conversation
        quick_action = dict(_TODO_TOOL_DEFAULTS)
        for tool_name in tools:
            if isinstance(tool_name, str) and tool_name in _TODO_TOOL_MAP:
                quick_action.update(_TODO_TOOL_MAP[tool_name])
        quick_action['prefill'] = detail or text
        item['quick_action'] = quick_action
        tomorrow_items.append(item)

    # ── Filter unfinished: remove items the LLM carried into tomorrow ──
    # Items that the LLM re-added to tomorrow should only appear in the
    # "明日计划" section, not in "未完成".  Unfinished items with no
    # matching tomorrow entry are truly abandoned/expired.
    if unfinished and tomorrow_items:
        tomorrow_texts = [it['text'] for it in tomorrow_items]
        filtered_unfinished = []
        for uf in unfinished:
            uf_text = uf.get('text', '')
            carried = any(
                _fuzzy_todo_match(uf_text, tt)
                for tt in tomorrow_texts
            )
            if carried:
                # Mark the tomorrow item as carried forward for UI badge
                for it in tomorrow_items:
                    if _fuzzy_todo_match(uf_text, it['text']):
                        it['_carried'] = True
                        break
                logger.debug('[DailyReport] Unfinished item carried to tomorrow: '
                             '"%s"', uf_text)
            else:
                filtered_unfinished.append(uf)
        if len(filtered_unfinished) < len(unfinished):
            logger.info('[DailyReport] Unfinished items: %d total, %d carried to '
                        'tomorrow, %d truly unfinished',
                        len(unfinished),
                        len(unfinished) - len(filtered_unfinished),
                        len(filtered_unfinished))
        unfinished = filtered_unfinished

    done_cnt = sum(1 for s in final_streams if s.get('status') == 'done')
    ip_cnt = sum(1 for s in final_streams if s.get('status') == 'in_progress')
    blk_cnt = sum(1 for s in final_streams if s.get('status') == 'blocked')
    elapsed = time.monotonic() - t0
    logger.info('[DailyReport] Analysis %s completed in %.1fs: %d convs → '
                '%d streams (done=%d ip=%d blk=%d), %d tomorrow items',
                target_date, elapsed, len(convs), len(final_streams),
                done_cnt, ip_cnt, blk_cnt, len(tomorrow_items))

    return {
        'ok': True,
        'streams': final_streams,
        'tomorrow': tomorrow_items,
        'carryover': carryover,
        'unfinished': unfinished,
        'tasks': [],   # compat for manual todos
        'quote': random.choice(_QUOTES),
        'persona': _pick_persona(stats),
        'stats': stats,
        'error': error_msg,
    }


# ═════════════════════════════════════════════════════════════
#  Server-side backfill: extract convs from DB for a past day
# ═════════════════════════════════════════════════════════════

def _build_transcript_from_messages(msgs, day_start_ms, day_end_ms):
    """Build a compact transcript from raw message dicts for a date range.

    Mimics the frontend's _buildConvTranscript() logic.
    """
    turns = []
    for msg in msgs:
        ts = _safe_int_ts(msg.get('timestamp', 0))
        # If no timestamp, include the message (old data)
        if ts and (ts < day_start_ms or ts >= day_end_ms):
            continue
        role = msg.get('role', '')
        content = msg.get('content', '')
        if isinstance(content, list):
            # Multi-modal messages — extract text parts
            content = ' '.join(
                (p if isinstance(p, str) else p.get('text', ''))
                for p in content
            )
        if not isinstance(content, str):
            content = ''

        if role == 'user' and content.strip():
            turns.append({'role': 'USER', 'text': content})
        elif role == 'assistant':
            tool_names = []
            for r in (msg.get('searchRounds', []) or []):
                for call in (r.get('calls', []) or r.get('toolCalls', []) or []):
                    tn = ''
                    if isinstance(call, dict):
                        fn = call.get('function', {})
                        tn = fn.get('name', '') if isinstance(fn, dict) else ''
                        if not tn:
                            tn = call.get('name', '')
                    if tn:
                        tool_names.append(tn)
            turns.append({'role': 'ASSISTANT', 'text': content, 'tools': tool_names})

    if not turns:
        return ''

    BUDGET = 800
    result = ''
    for i, t in enumerate(turns):
        is_first = (i == 0)
        is_last_two = (i >= len(turns) - 3)
        limit = 250 if (is_first or is_last_two) else 60

        snippet = re.sub(r'\n+', ' ', t['text'])[:limit]
        ellipsis = '…' if len(t['text']) > limit else ''
        result += f'{t["role"]}: {snippet}{ellipsis}\n'

        if t.get('tools'):
            result += f'[tools: {", ".join(t["tools"][:6])}]\n'

        if len(result) > BUDGET:
            break

    return result.strip()


def _safe_int_ts(value, fallback=0):
    """Safely convert a timestamp value to int, handling str/float/None."""
    if value is None:
        return fallback
    try:
        return int(value)
    except (ValueError, TypeError):
        return fallback


def _extract_convs_for_date(date_str, progress_cb=None):
    """Load conversations from DB that have activity on *date_str*.

    Args:
        date_str: ISO date string 'YYYY-MM-DD'.
        progress_cb: Optional callback(current, total) for progress tracking.

    Returns list of digest dicts ready for _analyse_conversations().
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.utils import safe_json

    t0 = time.monotonic()
    try:
        dt = _dt.date.fromisoformat(date_str)
    except ValueError:
        logger.warning('[DailyReport] Invalid date for backfill: %s', date_str)
        return []

    day_start_ms = int(_dt.datetime.combine(dt, _dt.time.min).timestamp() * 1000)
    day_end_ms = int(_dt.datetime.combine(dt + _dt.timedelta(days=1), _dt.time.min).timestamp() * 1000)
    logger.debug('[DailyReport] Extracting convs for %s (range %d–%d)',
                 date_str, day_start_ms, day_end_ms)

    try:
        db = get_thread_db(DOMAIN_CHAT)
        # SQL-level date filter: only fetch convs updated on or after target day
        # (created_at / updated_at are BIGINT epoch-ms)
        rows = db.execute(
            'SELECT id, title, messages, created_at, updated_at '
            'FROM conversations WHERE user_id=? AND '
            'COALESCE(updated_at, created_at, 0) >= ? '
            'ORDER BY updated_at DESC',
            (DEFAULT_USER_ID, day_start_ms)
        ).fetchall()
    except Exception as e:
        logger.error('[DailyReport] DB query failed for backfill %s: %s',
                     date_str, e, exc_info=True)
        return []

    logger.debug('[DailyReport] Scanning %d conversations (filtered) for date %s',
                 len(rows), date_str)

    digests = []
    for row_idx, r in enumerate(rows):
        if progress_cb and row_idx % 50 == 0:
            progress_cb(row_idx, len(rows))
        msgs = safe_json(r['messages'], default=[], label='backfill-messages')
        if not isinstance(msgs, list) or not msgs:
            continue

        # Check if conversation has activity on this day
        has_activity = False
        rounds = 0
        tools_used = set()

        for msg in msgs:
            ts = _safe_int_ts(msg.get('timestamp', 0))
            # For old data without timestamps, use conv timestamps
            if not ts:
                raw_ts = r['updated_at'] or r['created_at'] or 0
                ts = _safe_int_ts(raw_ts)
            if ts < day_start_ms or ts >= day_end_ms:
                continue
            has_activity = True
            if msg.get('role') == 'user':
                rounds += 1
            elif msg.get('role') == 'assistant':
                for sr in (msg.get('searchRounds', []) or []):
                    for call in (sr.get('calls', []) or sr.get('toolCalls', []) or []):
                        if isinstance(call, dict):
                            fn = call.get('function', {})
                            tn = fn.get('name', '') if isinstance(fn, dict) else ''
                            if not tn:
                                tn = call.get('name', '')
                            if tn:
                                tools_used.add(tn)

        if not has_activity:
            continue

        transcript = _build_transcript_from_messages(msgs, day_start_ms, day_end_ms)
        if not transcript and rounds == 0:
            continue

        digests.append({
            'id': r['id'],
            'title': r['title'] or '',
            'transcript': transcript,
            'toolsUsed': list(tools_used)[:10],
            'rounds': max(rounds, 1),
            'model': '',
        })

    elapsed = time.monotonic() - t0
    logger.info('[DailyReport] Backfill %s: found %d conversations with activity '
                '(scanned %d total in %.1fs)',
                date_str, len(digests), len(rows), elapsed)
    return digests


# ═════════════════════════════════════════════════════════════
#  Endpoints
# ═════════════════════════════════════════════════════════════

@daily_report_bp.route('/api/daily-report', methods=['POST'])
@_db_safe
def generate_daily_report():
    """Analyse conversations for a given date using DB-based extraction.

    Always extracts conversations from the database for accurate counts.
    Body: {date?: 'YYYY-MM-DD', force?: true}
    """
    t0 = time.monotonic()
    data = request.get_json(silent=True) or {}
    target_date = data.get('date', _dt.date.today().isoformat())
    force = data.get('force', False)

    logger.info('[DailyReport] POST request: date=%s, force=%s', target_date, force)

    if not re.match(r'^\d{4}-\d{2}-\d{2}$', target_date):
        logger.warning('[DailyReport] Invalid date format: %s', target_date)
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    # Return cached report unless force regenerate
    if not force:
        existing = _load_report(target_date)
        if existing and (existing.get('streams') or existing.get('tasks')):
            n = len(existing.get('streams', existing.get('tasks', [])))
            logger.info('[DailyReport] POST %s: returning cached (%d items)',
                        target_date, n)
            return jsonify({'ok': True, **existing})

    # Extract conversations from DB
    convs = _extract_convs_for_date(target_date)
    if not convs:
        # Still create an empty report so manual tasks can be added
        empty_result = {
            'ok': True, 'tasks': [],
            'quote': random.choice(_QUOTES),
            'persona': _pick_persona({}),
            'stats': {'totalConversations': 0},
        }
        return jsonify(empty_result)

    result = _analyse_conversations(convs, target_date)

    # Merge manual tasks from existing report if any
    existing = _load_report(target_date)
    if existing and existing.get('tasks'):
        manual_tasks = [t for t in existing['tasks'] if t.get('_todo')]
        if manual_tasks:
            result.setdefault('tasks', []).extend(manual_tasks)

    # Persist if analysis succeeded
    if (result.get('streams') or result.get('tomorrow')) and not result.get('error'):
        _save_report(target_date, result)

    elapsed = time.monotonic() - t0
    stream_count = len(result.get('streams', []))
    done_count = sum(1 for s in result.get('streams', []) if s.get('status') == 'done')
    logger.info('[DailyReport] POST %s completed in %.1fs: %d convs → %d streams '
                '(%d done, %d open), error=%s',
                target_date, elapsed, len(convs), stream_count, done_count,
                stream_count - done_count, result.get('error', 'none'))
    return jsonify(result)


@daily_report_bp.route('/api/daily-report/<date_str>')
def get_cached_report(date_str):
    """Get a previously generated report for a specific date."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    today_todos = _get_today_inherited_todos(date_str)

    report = _load_report(date_str)
    if report is None:
        # No report — check if previous day has tomorrow items to inherit
        if today_todos:
            logger.debug('[DailyReport] GET %s: no report, inheriting %d todos from prev day',
                         date_str, len(today_todos))
            conv_count = _count_convs_for_date(date_str)
            return jsonify({
                'ok': True, 'streams': [], 'tomorrow': [],
                'today_todos': today_todos,
                'tasks': [],
                'stats': {'totalConversations': conv_count},
                '_inherited': True,
                'quote': random.choice(_QUOTES),
            })
        logger.debug('[DailyReport] GET %s: no cached report', date_str)
        return jsonify({'ok': False, 'error': 'No report for this date'}), 404

    logger.debug('[DailyReport] GET %s: returning cached (%d streams, %d today_todos)',
                 date_str, len(report.get('streams', [])), len(today_todos))
    return jsonify({'ok': True, 'today_todos': today_todos, **report})


@daily_report_bp.route('/api/daily-report/backfill/<date_str>', methods=['POST'])
@_db_safe
def backfill_report(date_str):
    """Server-side backfill: extract conversations from DB and analyse.

    Used for past days when the frontend didn't generate a report.
    Also callable from the calendar UI.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    # Don't re-generate if cached
    existing = _load_report(date_str)
    if existing and (existing.get('streams') or existing.get('tasks')):
        n = len(existing.get('streams', existing.get('tasks', [])))
        logger.info('[DailyReport] Backfill %s: already cached (%d items)',
                     date_str, n)
        return jsonify({'ok': True, **existing})

    t0 = time.monotonic()
    convs = _extract_convs_for_date(date_str)
    if not convs:
        return jsonify({'ok': True, 'tasks': [],
                        'quote': random.choice(_QUOTES),
                        'persona': _pick_persona({}),
                        'stats': {'totalConversations': 0}})

    result = _analyse_conversations(convs, date_str)

    if result.get('streams') and not result.get('error'):
        _save_report(date_str, result)

    elapsed = time.monotonic() - t0
    stream_count = len(result.get('streams', []))
    logger.info('[DailyReport] Backfill %s completed in %.1fs: %d convs → %d streams, error=%s',
                date_str, elapsed, len(convs), stream_count,
                result.get('error', 'none'))
    return jsonify(result)


@daily_report_bp.route('/api/daily-report/calendar/<int:year>/<int:month>')
def get_calendar_month(year, month):
    """Month overview: which days have cached reports + task counts."""
    if month < 1 or month > 12:
        return jsonify({'ok': False, 'error': 'Invalid month'}), 400

    prefix = f'{year:04d}-{month:02d}-'
    days = {}
    try:
        for fname in os.listdir(_REPORTS_DIR):
            if fname.startswith(prefix) and fname.endswith('.json'):
                try:
                    day_num = int(fname[len(prefix):].replace('.json', ''))
                except ValueError:
                    continue
                report = _load_report(fname.replace('.json', ''))
                if report and ('streams' in report or 'tasks' in report):
                    streams = report.get('streams', [])
                    tasks = report.get('tasks', [])
                    if streams:
                        done = sum(1 for s in streams if s.get('status') == 'done')
                        total = len(streams)
                    elif tasks:
                        done = sum(1 for t in tasks if t.get('status') == 'done')
                        total = len(tasks)
                    else:
                        continue
                    date_key = f'{year:04d}-{month:02d}-{day_num:02d}'
                    days[date_key] = {
                        'total': total,
                        'done': done,
                        'incomplete': total - done,
                    }
    except Exception as e:
        logger.warning('[DailyReport] Calendar scan %d-%02d: %s', year, month, e)

    # ── TTL cache for expensive DB scans (conv_days + cost_days) ──
    cache_key = (year, month)
    cached = _calendar_cache.get(cache_key)
    if cached and (time.monotonic() - cached['ts']) < _CALENDAR_CACHE_TTL:
        conv_days = cached['conv_days']
        cost_days = cached['cost_days']
        logger.debug('[DailyReport] Calendar %d-%02d: cache hit (age %.1fs)',
                     year, month, time.monotonic() - cached['ts'])
    else:
        # ── Compute conv_days from DB (which days have conversations) ──
        conv_days = {}
        try:
            from lib.database import DOMAIN_CHAT, get_thread_db
            from lib.utils import safe_json

            month_start = _dt.date(year, month, 1)
            if month < 12:
                month_end = _dt.date(year, month + 1, 1)
            else:
                month_end = _dt.date(year + 1, 1, 1)
            ms_start = int(_dt.datetime.combine(month_start, _dt.time.min).timestamp() * 1000)
            ms_end = int(_dt.datetime.combine(month_end, _dt.time.min).timestamp() * 1000)

            db = get_thread_db(DOMAIN_CHAT)
            # SQL-level date filter: only fetch convs updated within or after target month
            # (created_at / updated_at are BIGINT epoch-ms)
            rows = db.execute(
                'SELECT id, messages, created_at, updated_at '
                'FROM conversations WHERE user_id=? AND '
                'COALESCE(updated_at, created_at, 0) >= ? '
                'ORDER BY updated_at DESC',
                (DEFAULT_USER_ID, ms_start)
            ).fetchall()
            for r in rows:
                msgs = safe_json(r['messages'], default=[], label='cal-conv-days')
                if not isinstance(msgs, list) or not msgs:
                    continue
                for msg in msgs:
                    ts = _safe_int_ts(msg.get('timestamp', 0))
                    if not ts:
                        ts = _safe_int_ts(r['updated_at'] or r['created_at'] or 0)
                    if ms_start <= ts < ms_end:
                        day_num = _dt.datetime.fromtimestamp(ts / 1000).day
                        conv_days[day_num] = conv_days.get(day_num, 0) + 1
                        break
        except Exception as e:
            logger.warning('[DailyReport] Calendar conv-days %d-%02d: %s', year, month, e)

        # ── Server-side per-day cost calculation ──
        cost_days = {}
        try:
            raw_costs = _get_monthly_costs(year, month)
            for day_num, day_data in raw_costs.items():
                cost_days[day_num] = {
                    'cost': day_data['cost'],
                    'conversations': day_data['conversations'],
                }
        except Exception as e:
            logger.warning('[DailyReport] Calendar cost calc %d-%02d: %s', year, month, e)

        # Store in cache
        _calendar_cache[cache_key] = {
            'conv_days': conv_days,
            'cost_days': cost_days,
            'ts': time.monotonic(),
        }

    logger.debug('[DailyReport] Calendar %d-%02d: %d days with reports, %d days with convs, '
                 '%d days with costs',
                 year, month, len(days), len(conv_days), len(cost_days))
    return jsonify({'ok': True, 'year': year, 'month': month, 'days': days,
                    'conv_days': conv_days, 'cost_days': cost_days})


@daily_report_bp.route('/api/daily-report/task-status', methods=['PATCH'])
@_db_safe
def update_task_status():
    """Update the status of a single task in a daily report.

    Allows users to manually override the LLM-assigned completion status.
    Body: {date: 'YYYY-MM-DD', conv_id: '...', status: 'done'|'incomplete'}
    """
    data = request.get_json(silent=True) or {}
    date_str = data.get('date', '')
    item_id = data.get('stream_id', '') or data.get('conv_id', '') or data.get('task_id', '')
    new_status = data.get('status', '')

    if not all([date_str, item_id, new_status]):
        return jsonify({'ok': False, 'error': 'Missing required fields'}), 400
    valid_statuses = ('done', 'in_progress', 'blocked', 'incomplete')
    if new_status not in valid_statuses:
        return jsonify({'ok': False, 'error': f'Invalid status — must be one of {valid_statuses}'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    report = _load_report(date_str)
    if not report:
        return jsonify({'ok': False, 'error': 'No report for this date'}), 404

    found = False
    # Try streams first
    for stream in report.get('streams', []):
        if stream.get('id') == item_id:
            old_status = stream.get('status', '?')
            stream['status'] = new_status
            stream['_manual'] = True
            if new_status == 'done':
                stream['remaining'] = None
            found = True
            logger.info('[DailyReport] Stream status updated %s: %s → %s (id=%s)',
                        date_str, old_status, new_status, item_id)
            break

    # Fall back to tasks (manual todos)
    if not found:
        for task in report.get('tasks', []):
            if task.get('conv_id') == item_id or task.get('id') == item_id:
                old_status = task.get('status', '?')
                task['status'] = new_status
                task['_manual'] = True
                found = True
                logger.info('[DailyReport] Task status updated %s: %s → %s (id=%s)',
                            date_str, old_status, new_status, item_id)
                break

    if not found:
        return jsonify({'ok': False, 'error': 'Item not found in report'}), 404

    _save_report(date_str, report)
    return jsonify({'ok': True, 'status': new_status})


@daily_report_bp.route('/api/daily-report/todo-toggle', methods=['PATCH'])
@_db_safe
def toggle_tomorrow_todo():
    """Toggle the done state of a tomorrow TODO item.

    Body: {date: 'YYYY-MM-DD', todo_id: 'todo-...', done: true|false}
    """
    data = request.get_json(silent=True) or {}
    date_str = data.get('date', '')
    todo_id = data.get('todo_id', '')
    done = data.get('done', False)

    if not date_str or not todo_id:
        return jsonify({'ok': False, 'error': 'Missing date or todo_id'}), 400

    report = _load_report(date_str)
    if not report:
        return jsonify({'ok': False, 'error': 'No report for this date'}), 404

    found = False
    for item in report.get('tomorrow', []):
        if item.get('id') == todo_id:
            item['done'] = bool(done)
            found = True
            break

    if not found:
        return jsonify({'ok': False, 'error': 'TODO item not found'}), 404

    _save_report(date_str, report)
    logger.info('[DailyReport] Tomorrow TODO toggled %s: %s done=%s', date_str, todo_id, done)
    return jsonify({'ok': True})


@daily_report_bp.route('/api/daily-report/inherited-todo-toggle', methods=['PATCH'])
@_db_safe
def toggle_inherited_todo():
    """Toggle a TODO item that was inherited from a previous day's report.

    This is a cross-day operation: the item lives in ``origin_date``'s
    ``tomorrow[]`` array, but the user is toggling it from ``view_date``'s
    "今日待办" section.

    Body: {origin_date: 'YYYY-MM-DD', todo_id: 'todo-...', done: bool}
    """
    data = request.get_json(silent=True) or {}
    origin_date = data.get('origin_date', '')
    todo_id = data.get('todo_id', '')
    done = data.get('done', False)

    if not origin_date or not todo_id:
        return jsonify({'ok': False, 'error': 'Missing origin_date or todo_id'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', origin_date):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    report = _load_report(origin_date)
    if not report:
        return jsonify({'ok': False, 'error': 'No report for origin date'}), 404

    found = False
    for item in report.get('tomorrow', []):
        if item.get('id') == todo_id:
            item['done'] = bool(done)
            found = True
            break

    if not found:
        return jsonify({'ok': False, 'error': 'TODO item not found in origin report'}), 404

    _save_report(origin_date, report)
    logger.info('[DailyReport] Inherited TODO toggled: origin=%s id=%s done=%s',
                origin_date, todo_id, done)
    return jsonify({'ok': True})


@daily_report_bp.route('/api/daily-report/inherited-todo', methods=['DELETE', 'POST'])
@_db_safe
def delete_inherited_todo():
    """Delete a TODO item inherited from a previous day's report.

    This removes the item from the origin date's ``tomorrow[]`` array,
    so it won't appear in any subsequent day's "今日待办" section.

    Body: ``{origin_date: 'YYYY-MM-DD', todo_id: 'todo-...'}``
    """
    data = request.get_json(silent=True) or {}
    origin_date = data.get('origin_date', '')
    todo_id = data.get('todo_id', '')

    if not origin_date or not todo_id:
        return jsonify({'ok': False, 'error': 'Missing origin_date or todo_id'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', origin_date):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    report = _load_report(origin_date)
    if not report:
        return jsonify({'ok': False, 'error': 'No report for origin date'}), 404

    tomorrow = report.get('tomorrow', [])
    new_tomorrow = [t for t in tomorrow if t.get('id') != todo_id]

    if len(new_tomorrow) == len(tomorrow):
        return jsonify({'ok': False, 'error': 'TODO item not found in origin report'}), 404

    report['tomorrow'] = new_tomorrow
    _save_report(origin_date, report)
    logger.info('[DailyReport] Inherited TODO deleted: origin=%s id=%s', origin_date, todo_id)
    return jsonify({'ok': True})


def _count_convs_for_date(date_str):
    """Count conversations with activity on a given date (DB query).

    Returns:
        int: Number of conversations, or 0 on error.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.utils import safe_json

    try:
        dt = _dt.date.fromisoformat(date_str)
    except ValueError:
        return 0

    day_start_ms = int(_dt.datetime.combine(dt, _dt.time.min).timestamp() * 1000)
    day_end_ms = int(_dt.datetime.combine(dt + _dt.timedelta(days=1), _dt.time.min).timestamp() * 1000)

    try:
        db = get_thread_db(DOMAIN_CHAT)
        # SQL-level date filter: only fetch convs updated on or after target day
        rows = db.execute(
            'SELECT id, messages, created_at, updated_at '
            'FROM conversations WHERE user_id=? AND '
            'COALESCE(updated_at, created_at, 0) >= ? '
            'ORDER BY updated_at DESC',
            (DEFAULT_USER_ID, day_start_ms)
        ).fetchall()
    except Exception as e:
        logger.error('[DailyReport] conv-count DB error for %s: %s', date_str, e, exc_info=True)
        return 0

    count = 0
    for r in rows:
        msgs = safe_json(r['messages'], default=[], label='conv-count-messages')
        if not isinstance(msgs, list) or not msgs:
            continue
        for msg in msgs:
            ts = _safe_int_ts(msg.get('timestamp', 0))
            if not ts:
                raw_ts = r['updated_at'] or r['created_at'] or 0
                ts = _safe_int_ts(raw_ts)
            if day_start_ms <= ts < day_end_ms:
                count += 1
                break

    return count


@daily_report_bp.route('/api/daily-report/conv-count/<date_str>')
def get_conv_count(date_str):
    """Return the number of conversations with activity on a given date.

    Queries the database directly — reliable count regardless of frontend state.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    count = _count_convs_for_date(date_str)
    logger.debug('[DailyReport] conv-count %s: %d conversations', date_str, count)
    return jsonify({'ok': True, 'count': count, 'date': date_str})


@daily_report_bp.route('/api/daily-report/task', methods=['POST'])
@_db_safe
def add_manual_task():
    """Add a manually created TODO item to a daily report.

    Body: ``{date: 'YYYY-MM-DD', task: 'task description'}``
    Creates the report file if it doesn't exist.
    """
    data = request.get_json(silent=True) or {}
    date_str = data.get('date', '')
    task_text = (data.get('task', '') or '').strip()

    if not date_str or not task_text:
        return jsonify({'ok': False, 'error': 'Missing date or task'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    import uuid
    todo_id = f'todo-{uuid.uuid4().hex[:8]}'

    report = _load_report(date_str) or {
        'streams': [], 'tomorrow': [], 'tasks': [],
        'stats': {'totalConversations': 0},
        'quote': random.choice(_QUOTES),
    }
    report.setdefault('tomorrow', []).append({
        'id': todo_id,
        'text': task_text[:60],
        'done': False,
    })
    _save_report(date_str, report)
    logger.info('[DailyReport] TODO added to %s: %s (id=%s)', date_str, task_text[:60], todo_id)
    return jsonify({'ok': True, 'task_id': todo_id, 'report': report})


@daily_report_bp.route('/api/daily-report/task', methods=['DELETE'])
@_db_safe
def delete_manual_task():
    """Delete a TODO item from a daily report.

    Body: ``{date: 'YYYY-MM-DD', task_id: 'todo-...'}``
    """
    data = request.get_json(silent=True) or {}
    date_str = data.get('date', '')
    task_id = data.get('task_id', '')

    if not date_str or not task_id:
        return jsonify({'ok': False, 'error': 'Missing date or task_id'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    report = _load_report(date_str)
    if not report:
        return jsonify({'ok': False, 'error': 'No report for this date'}), 404

    tomorrow = report.get('tomorrow', [])
    new_tomorrow = [t for t in tomorrow if t.get('id') != task_id]

    if len(new_tomorrow) == len(tomorrow):
        return jsonify({'ok': False, 'error': 'TODO item not found'}), 404

    report['tomorrow'] = new_tomorrow
    _save_report(date_str, report)
    logger.info('[DailyReport] TODO deleted from %s: %s', date_str, task_id)
    return jsonify({'ok': True, 'report': report})


# ═════════════════════════════════════════════════════════════
#  Async generation (background thread + status polling)
# ═════════════════════════════════════════════════════════════

def _generate_in_background(date_str, force):
    """Background thread: extract convs → LLM analysis → save report.

    Updates ``_active_jobs[date_str]`` with progress stages:
    starting → extracting → analyzing → saving → done (or error).
    """
    try:
        logger.info('[DailyReport] Background generation started for %s (force=%s)',
                    date_str, force)

        # Phase 1: Extract conversations from DB
        _update_job(date_str, 'generating', progress={
            'stage': 'extracting',
            'message': '正在扫描对话…',
            'current': 0, 'total': 0,
        })

        def _extraction_progress(current, total):
            _update_job(date_str, 'generating', progress={
                'stage': 'extracting',
                'message': f'扫描对话 {current}/{total}',
                'current': current, 'total': total,
            })

        convs = _extract_convs_for_date(date_str, progress_cb=_extraction_progress)

        if not convs:
            result = {
                'tasks': [],
                'quote': random.choice(_QUOTES),
                'persona': _pick_persona({}),
                'stats': {'totalConversations': 0},
            }
            # Merge manual tasks from existing report
            existing = _load_report(date_str)
            if existing:
                manual = [t for t in existing.get('tasks', []) if t.get('_todo')]
                if manual:
                    result['tasks'] = manual
            if result['tasks']:
                _save_report(date_str, result)
            _update_job(date_str, 'done')
            logger.info('[DailyReport] Background generation %s: no convs found', date_str)
            return

        # Phase 2: LLM Analysis
        _update_job(date_str, 'generating', progress={
            'stage': 'analyzing',
            'message': f'LLM 分析 {len(convs)} 个对话…',
            'current': 0, 'total': len(convs),
        })

        result = _analyse_conversations(convs, date_str)

        # Merge manual tasks from existing report
        existing = _load_report(date_str)
        if existing and existing.get('tasks'):
            manual = [t for t in existing['tasks'] if t.get('_todo')]
            if manual:
                result.setdefault('tasks', []).extend(manual)

        # Phase 3: Save
        _update_job(date_str, 'generating', progress={
            'stage': 'saving', 'message': '保存报告…',
        })

        if (result.get('streams') or result.get('tomorrow')) and not result.get('error'):
            _save_report(date_str, result)
        elif result.get('error'):
            logger.warning('[DailyReport] Background generation %s: not saving error result: %s',
                           date_str, result['error'])

        _update_job(date_str, 'done')

        stream_count = len(result.get('streams', []))
        done_count = sum(1 for s in result.get('streams', []) if s.get('status') == 'done')
        logger.info('[DailyReport] Background generation %s completed: %d streams (%d done)',
                    date_str, stream_count, done_count)

    except Exception as e:
        logger.error('[DailyReport] Background generation %s failed: %s',
                     date_str, e, exc_info=True)
        _update_job(date_str, 'error', error=str(e))


@daily_report_bp.route('/api/daily-report/generate', methods=['POST'])
@_db_safe
def start_generation():
    """Start async report generation.  Returns immediately.

    Body: {date?: 'YYYY-MM-DD', force?: true}
    Poll ``/api/daily-report/status/<date>`` for progress.
    """
    data = request.get_json(silent=True) or {}
    target_date = data.get('date', _dt.date.today().isoformat())
    force = data.get('force', False)

    if not re.match(r'^\d{4}-\d{2}-\d{2}$', target_date):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    # Already generating?
    job = _get_job(target_date)
    if job and job.get('status') == 'generating':
        logger.debug('[DailyReport] Generate %s: already running', target_date)
        return jsonify({'ok': True, 'status': 'generating',
                        'progress': job.get('progress', {})})

    # Check cache (unless forced)
    if not force:
        existing = _load_report(target_date)
        if existing and (existing.get('streams') or existing.get('tasks')):
            return jsonify({'ok': True, 'status': 'done', 'report': existing})

    # Launch background thread
    _update_job(target_date, 'generating',
                progress={'stage': 'starting', 'message': '正在启动…'})
    t = threading.Thread(
        target=_generate_in_background,
        args=(target_date, force),
        daemon=True,
        name=f'report-gen-{target_date}',
    )
    t.start()
    logger.info('[DailyReport] Background generation launched for %s (force=%s)',
                target_date, force)

    return jsonify({'ok': True, 'status': 'generating',
                    'progress': {'stage': 'starting', 'message': '正在启动…'}})


@daily_report_bp.route('/api/daily-report/status/<date_str>')
def get_generation_status(date_str):
    """Poll generation progress for a date.

    Returns one of:
      ``{status: 'idle'}``                                — nothing running or cached
      ``{status: 'generating', progress: {…}}``           — work in progress
      ``{status: 'done', report: {…}}``                   — finished
      ``{status: 'error', error: '…'}``                   — failed
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'ok': False, 'error': 'Invalid date format'}), 400

    job = _get_job(date_str)

    today_todos = _get_today_inherited_todos(date_str)

    if not job:
        # No active job — check disk cache
        existing = _load_report(date_str)
        if existing and (existing.get('streams') is not None or existing.get('tasks') is not None):
            existing['today_todos'] = today_todos
            return jsonify({'ok': True, 'status': 'done', 'report': existing})
        # Check if previous day has inherited todos
        if today_todos:
            conv_count = _count_convs_for_date(date_str)
            return jsonify({
                'ok': True, 'status': 'done',
                'report': {
                    'streams': [], 'tomorrow': [], 'tasks': [],
                    'today_todos': today_todos,
                    'stats': {'totalConversations': conv_count},
                    '_inherited': True,
                    'quote': random.choice(_QUOTES),
                },
            })
        return jsonify({'ok': True, 'status': 'idle'})

    status = job.get('status', 'idle')

    if status == 'done':
        _clear_job(date_str)
        report = _load_report(date_str) or {'tasks': []}
        report['today_todos'] = today_todos
        return jsonify({'ok': True, 'status': 'done', 'report': report})

    if status == 'error':
        error_msg = job.get('error', 'Unknown error')
        _clear_job(date_str)
        return jsonify({'ok': True, 'status': 'error', 'error': error_msg})

    # Still generating
    return jsonify({'ok': True, 'status': 'generating',
                    'progress': job.get('progress', {})})


# ═════════════════════════════════════════════════════════════
#  Background scheduler — auto-backfill yesterday
# ═════════════════════════════════════════════════════════════

_scheduler_started = False


def _backfill_yesterday_if_missing():
    """Check if yesterday's report exists; if not, generate from DB."""
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    if _load_report(yesterday) is not None:
        logger.debug('[DailyReport] Yesterday %s already has a report', yesterday)
        return

    logger.info('[DailyReport] Auto-backfill for yesterday %s', yesterday)
    try:
        convs = _extract_convs_for_date(yesterday)
        if not convs:
            logger.info('[DailyReport] No conversations found for %s, skipping', yesterday)
            return

        result = _analyse_conversations(convs, yesterday)
        if result.get('streams') and not result.get('error'):
            _save_report(yesterday, result)
            logger.info('[DailyReport] Auto-backfill %s: %d streams saved', yesterday,
                        len(result['streams']))
        else:
            logger.warning('[DailyReport] Auto-backfill %s: analysis failed: %s',
                           yesterday, result.get('error', 'unknown'))
    except Exception as e:
        logger.error('[DailyReport] Auto-backfill %s failed: %s',
                     yesterday, e, exc_info=True)


def _scheduler_loop():
    """Background loop: run backfill check at startup and every 6 hours."""
    # Initial delay to let server fully start
    time.sleep(60)
    logger.info('[DailyReport] Scheduler started — checking yesterday')

    while True:
        try:
            _backfill_yesterday_if_missing()
        except Exception as e:
            logger.error('[DailyReport] Scheduler cycle error: %s', e, exc_info=True)
        # Sleep 6 hours between checks
        time.sleep(6 * 3600)


def start_report_scheduler():
    """Start the background scheduler daemon thread.

    Called once from server.py or from blueprint registration.
    Safe to call multiple times — only starts one thread.
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True,
                         name='daily-report-scheduler')
    t.start()
    logger.info('[DailyReport] Background scheduler thread launched')


# ═════════════════════════════════════════════════════════════
#  LLM Analysis
# ═════════════════════════════════════════════════════════════

def _extract_json_result(text):
    """Robustly extract JSON from LLM output.

    Handles both new format ``{streams: [...], tomorrow: [...]}``
    and legacy format ``[...]``.

    Returns (streams_list, tomorrow_list, yesterday_done_list) tuple.
    """
    if not text:
        return [], [], []

    s = text.strip()

    # Strip markdown fences
    if s.startswith('```'):
        s = re.sub(r'^```\w*\n?', '', s)
        s = re.sub(r'\n?```\s*$', '', s)
        s = s.strip()

    def _unpack(result):
        """Unpack parsed JSON into (streams, tomorrow, yesterday_done)."""
        if isinstance(result, dict):
            streams = result.get('streams', [])
            tomorrow = result.get('tomorrow', [])
            yd = result.get('yesterday_done', [])
            if isinstance(streams, list):
                return (streams,
                        tomorrow if isinstance(tomorrow, list) else [],
                        yd if isinstance(yd, list) else [])
        if isinstance(result, list):
            return result, [], []
        return None

    # Direct parse
    try:
        result = json.loads(s)
        unpacked = _unpack(result)
        if unpacked:
            return unpacked
    except json.JSONDecodeError as e:
        logger.debug('Direct JSON parse failed, trying extraction: %s', e)

    # Find outermost { or [
    for opener, closer in [('{', '}'), ('[', ']')]:
        start = s.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(s)):
            ch = s[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(s[start:i + 1])
                        unpacked = _unpack(result)
                        if unpacked:
                            return unpacked
                    except json.JSONDecodeError as e:
                        logger.debug('Extracted JSON parse failed: %s', e)
                    break

    return [], [], []


def _run_llm_analysis(user_prompt, conv_count):
    """Call the LLM via smart_chat.

    Returns ``(streams_list, tomorrow_list, yesterday_done_list, error_msg|None)``.
    """
    try:
        from lib.llm_dispatch import smart_chat

        messages = [
            {'role': 'system', 'content': _ANALYSIS_SYSTEM},
            {'role': 'user', 'content': user_prompt},
        ]

        content, usage = smart_chat(
            messages,
            max_tokens=min(4096, 400 * conv_count),
            temperature=0.3,
            capability='text',
            log_prefix='[DailyReport]',
            max_retries=3,
            timeout=90,
        )

        dispatch_info = usage.get('_dispatch', {}) if isinstance(usage, dict) else {}
        logger.info('[DailyReport] LLM via %s:%s in %dms',
                    dispatch_info.get('key', '?'),
                    dispatch_info.get('model', '?'),
                    dispatch_info.get('latency_ms', 0))

        if not content:
            logger.warning('[DailyReport] LLM returned empty content')
            return [], [], [], 'LLM returned empty response'

        streams, tomorrow, yesterday_done = _extract_json_result(content)
        if not streams:
            logger.warning('[DailyReport] JSON extraction failed: %.500s', content)
            return [], [], [], 'Failed to parse LLM JSON output'

        logger.info('[DailyReport] Parsed %d streams, %d tomorrow items, '
                    '%d yesterday_done',
                    len(streams), len(tomorrow), len(yesterday_done))
        return streams, tomorrow, yesterday_done, None

    except json.JSONDecodeError as e:
        logger.warning('[DailyReport] JSON parse error: %s', e)
        return [], [], [], f'JSON parse error: {e}'

    except Exception as e:
        logger.error('[DailyReport] LLM analysis failed: %s', e, exc_info=True)
        return [], [], [], f'LLM call failed: {e}'


def _pick_persona(stats):
    """Pick a fun persona based on usage patterns."""
    tc = stats.get('totalConversations', 0)
    sc = stats.get('searchCount', 0)
    pc = stats.get('projectCount', 0)
    cr = stats.get('codeRelated', False)
    tt = stats.get('toolTypesUsed', [])
    tm = stats.get('totalMessages', 0)
    ah = stats.get('activeHours', 0)

    if tc == 0:
        return {'emoji': '😴', 'name': '休眠树懒', 'desc': '今天还没开始呢'}
    if cr and pc >= 3:
        return {'emoji': '🐙', 'name': '八爪鱼程序员', 'desc': '多线程编码，触手可及每一个 bug'}
    if sc >= 10:
        return {'emoji': '🦅', 'name': '信息猎鹰', 'desc': '锐利的双眼扫过互联网的每一个角落'}
    if len(tt) >= 5:
        return {'emoji': '🦊', 'name': '瑞士军刀狐', 'desc': '十八般武艺样样精通'}
    if ah >= 8:
        return {'emoji': '🐺', 'name': '耐力狼', 'desc': '从早到晚持续战斗'}
    if tm >= 50:
        return {'emoji': '🐬', 'name': '社交海豚', 'desc': '与 AI 交流如鱼得水'}
    if cr:
        return {'emoji': '🐍', 'name': '代码蟒蛇', 'desc': '优雅地缠绕每一个逻辑链条'}
    if tc >= 5:
        return {'emoji': '🦋', 'name': '话题蝴蝶', 'desc': '在不同领域之间优雅穿梭'}
    if tc <= 2 and tm <= 10:
        return {'emoji': '🐱', 'name': '慵懒猫咪', 'desc': '高效摸鱼，点到为止'}
    return {'emoji': '🦉', 'name': '智慧猫头鹰', 'desc': '深思熟虑，每一次提问都恰到好处'}
