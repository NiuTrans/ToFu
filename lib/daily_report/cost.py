"""Server-side cost calculation for the daily-report calendar.

Strategy: past days come from the durable daily-cost cache; current day is
always live-computed; the calendar endpoint wraps the whole thing in a
30-second in-process TTL cache to absorb burst-polling.
"""

import datetime as _dt
import re
import time
import uuid

from lib.cost import normalize_usage
from lib.identity import require_user_id
from lib.log import get_logger
from lib.utils import safe_json

from .conversations import _safe_int_ts
logger = get_logger(__name__)


# ── Calendar endpoint TTL cache (avoids 5s+ repeated full-table scans) ──
# Shared with storage._save_report / invalidate_day_cost_cache /
# routes.daily_report.get_calendar_month — they all pop / clear the
# entry for a month when a report is saved or cost data invalidated.
_calendar_cache: dict = {}
# (owner_user_id, year, month) → {'data': dict, 'ts': monotonic, ...}
_CALENDAR_CACHE_TTL = 30  # seconds


# Legacy preset → model_id migration table (mirrors core.js _LEGACY_PRESET_TO_MODEL)
_LEGACY_PRESET_TO_MODEL = {
    'qwen': 'qwen3.5-plus', 'low': 'qwen3.5-plus',
    'gemini': 'gemini-3-flash-preview', 'gemini_flash': 'gemini-3-flash-preview',
    'minimax': 'MiniMax-M2.7', 'doubao': 'Doubao-Seed-2.0-pro',
    'opus': 'aws.claude-opus-4.7',
    'medium': 'aws.claude-opus-4.7', 'high': 'aws.claude-opus-4.7',
    'xhigh': 'aws.claude-opus-4.7', 'max': 'aws.claude-opus-4.7',
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


def _calc_msg_cost_cny(usage, model_or_preset='', provider_id='', at=None):
    """Calculate cost in CNY for a single message's usage dict.

    This is a faithful Python port of the frontend ``calcCostCny()``
    in ``core.js``, using the same pricing logic.

    Args:
        usage: Token usage dict (prompt_tokens, completion_tokens, etc.).
        model_or_preset: Model ID or legacy preset key.
        provider_id: Optional provider that served the call. When set, a
            provider-scoped override in ``PROVIDER_PRICING`` (registered
            from the provider template's per-model ``pricing`` field) is
            preferred over the global ``MODEL_PRICING`` table.
        at: UTC epoch seconds of the MESSAGE — peak schedules are evaluated
            at the message's own time so a historical rescan never bills a
            past day at today's peak status. Defaults to now.

    Returns:
        Cost in CNY (float), or 0.0 if no tokens.
    """
    if not usage:
        return 0.0

    from lib import DEFAULT_USD_CNY_RATE
    from lib.pricing import get_pricing_data, lookup_pricing

    # Resolve legacy preset
    model_id = model_or_preset or ''
    model_id = _LEGACY_PRESET_TO_MODEL.get(model_id, model_id)

    _u = normalize_usage(usage)
    inp = _u['input']
    out = _u['output']
    cache_write = _u['cache_write']
    cache_read = _u['cache_read']
    think_tok = _u['thinking']
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

    mp = lookup_pricing(model_id, provider_id=provider_id, at=at)
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






def _usage_rows_from_snapshots(snapshots):
    """Build billing projections from authority-aware repository snapshots."""
    projected = []
    for snapshot in snapshots:
        raw_settings = snapshot.get('settings') or {}
        settings = (safe_json(raw_settings, default={}, label='cost-settings')
                    if isinstance(raw_settings, str) else raw_settings)
        settings = settings if isinstance(settings, dict) else {}
        messages = snapshot.messages if isinstance(snapshot.messages, list) else []
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        first_content = first.get('content', '')
        first_content = first_content[:30] if isinstance(first_content, str) else ''
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or not isinstance(
                    message.get('usage'), dict):
                continue
            projected.append({
                'id': snapshot['id'],
                'title': snapshot.get('title') or '',
                'created_at': snapshot.get('created_at') or 0,
                'updated_at': snapshot.get('updated_at') or 0,
                'conv_model': (settings.get('model') or settings.get('preset')
                               or settings.get('effort') or ''),
                'first_content': first_content,
                'msg_index': index,
                'total_msgs': len(messages),
                'usage': message['usage'],
                'msg_timestamp': message.get('timestamp', 0),
                'msg_model': (message.get('model') or message.get('preset')
                              or message.get('effort') or ''),
                'msg_provider': (message.get('provider_id')
                                 or message.get('providerId') or ''),
            })
    return projected






def _scan_costs_in_range(
        ms_start, ms_end, year=None, month=None, *, owner_user_id):
    """Scan conversation snapshots and build per-day costs for a range.

    Args:
        ms_start: Inclusive lower bound (epoch-ms).
        ms_end:   Exclusive upper bound (epoch-ms).
        year, month: Optional extra filter — only keep days whose date falls
            in this year/month (when aggregating a full month).  If None,
            all days in the range are kept.

    Returns:
        dict mapping day-of-month (int) → {'cost': float,
            'conversations': {conv_id: {'name', 'cost', 'tokens'}}}.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cost scan')
    try:
        from lib.conversations.repository import scan_conversations_bounded
        _candidate_count, snapshots = scan_conversations_bounded(
            user_id=owner_id,
            updated_at_gte=ms_start,
            created_at_lt=ms_end,
            limit=10_000,
            settings_keys=['model', 'preset', 'effort'],
        )
        rows = _usage_rows_from_snapshots(snapshots)
    except Exception as e:
        logger.error('[DailyReport] Cost authority read failed range=[%d,%d): %s',
                     ms_start, ms_end, e, exc_info=True)
        return {}

    days = {}   # day_num → {cost, conversations}

    for r in rows:
        # psycopg decodes JSONB to dict; SQLite's json_extract returns text.
        # Accept both without feeding an already-decoded dict back through
        # json.loads (which logs a false "corrupt JSON" warning).
        usage_raw = r['usage']
        usage = (usage_raw if isinstance(usage_raw, dict) else
                 safe_json(usage_raw, default={}, label='cost-usage'))
        if not isinstance(usage, dict) or not usage:
            continue

        conv_start = _safe_int_ts(r['created_at'] or r['updated_at'] or 0)
        conv_end = _safe_int_ts(r['updated_at'] or r['created_at'] or 0)
        total_msgs = int(r.get('total_msgs') or 0)
        msg_index = int(r.get('msg_index') or 0)
        conv_title = r['title'] or r.get('first_content') or ''
        conv_title = conv_title or 'Untitled'
        conv_id = r['id']

        ts = _safe_int_ts(r.get('msg_timestamp', 0))
        if not ts:
            if (conv_start and conv_end and conv_start != conv_end
                    and total_msgs > 1):
                ts = conv_start + int(
                    (conv_end - conv_start) * msg_index / (total_msgs - 1))
            else:
                ts = conv_start
        if not ts or ts < ms_start or ts >= ms_end:
            continue

        d = _dt.datetime.fromtimestamp(ts / 1000)
        if year is not None and month is not None:
            if d.year != year or d.month != month:
                continue
        day_num = d.day

        msg_model = r.get('msg_model') or r.get('conv_model') or ''
        msg_provider = r.get('msg_provider') or ''

        cost_cny = _calc_msg_cost_cny(usage, msg_model, msg_provider,
                                      at=ts / 1000 if ts else None)
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
        _tok = normalize_usage(usage)
        entry['tokens'] += _tok['input'] + _tok['output']

    for day_data in days.values():
        day_data['cost'] = round(day_data['cost'], 4)
        for conv_entry in day_data['conversations'].values():
            conv_entry['cost'] = round(conv_entry['cost'], 4)

    return days


def _load_cached_day_costs(year, month, *, owner_user_id):
    """Load persisted per-day costs for a given month from daily_cost_cache.

    Returns:
        dict mapping day-of-month (int) → {'cost': float, 'conversations': {...}}
        for days that have cached entries.  Days without entries are absent.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cached costs')
    try:
        from lib.storage import get_storage_client
        rows = get_storage_client().query('daily_cost.month', {
            'user_id': owner_id, 'year': year, 'month': month,
        })
    except Exception as e:
        logger.warning('[DailyReport] Load cached costs %d-%02d failed: %s',
                       year, month, e)
        return {}

    out = {}
    for r in rows:
        date_str = r['date']
        try:
            day_num = int(date_str.split('-')[2])
        except (ValueError, IndexError, AttributeError) as e:
            logger.debug('[DailyReport] Skipping invalid cached date %r: %s',
                         date_str, e)
            continue
        cost_val = float(r['cost'])
        convs = r.get('conversations') or {}
        if not isinstance(convs, dict):
            convs = {}
        out[day_num] = {'cost': round(cost_val, 4), 'conversations': convs}
    return out


def _persist_day_cost(date_str, day_data, *, owner_user_id):
    """Write a single day's cost aggregate to daily_cost_cache.

    Args:
        date_str: 'YYYY-MM-DD'.
        day_data: {'cost': float, 'conversations': {conv_id: {...}}}.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cost persistence')
    try:
        from lib.storage import get_storage_client
        get_storage_client(write=True).command('daily_cost.upsert', {
            'user_id': owner_id,
            'date': date_str,
            'cost': float(day_data.get('cost', 0.0)),
            'conversations': day_data.get('conversations', {}),
            'computed_at': int(time.time() * 1000),
        }, f'daily-cost-upsert:{uuid.uuid4().hex}')
    except Exception as e:
        logger.warning('[DailyReport] Persist day cost %s failed: %s',
                       date_str, e)


def invalidate_day_cost_cache(date_str=None, *, owner_user_id):
    """Invalidate persisted per-day cost cache entries.

    Args:
        date_str: If given, remove only that day ('YYYY-MM-DD').
                  If None, clear all entries (e.g. on bulk delete).
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report cost invalidation')
    try:
        from lib.storage import get_storage_client
        payload = {'user_id': owner_id}
        if date_str is not None:
            payload['date'] = date_str
        get_storage_client(write=True).command(
            'daily_cost.delete', payload,
            f'daily-cost-delete:{uuid.uuid4().hex}')
        if date_str:
            logger.debug('[DailyReport] Invalidated day-cost cache for %s', date_str)
        else:
            logger.info('[DailyReport] Invalidated ALL day-cost cache entries')
        # Also drop the in-process calendar TTL cache so the next request
        # picks up the change.
        for key in list(_calendar_cache):
            if key[0] == owner_id:
                _calendar_cache.pop(key, None)
    except Exception as e:
        logger.warning('[DailyReport] invalidate_day_cost_cache(%s) failed: %s',
                       date_str, e)


def _cost_days_for_messages(messages, conv_start=0, conv_end=0):
    """Return the set of ``'YYYY-MM-DD'`` days a message list contributes cost to.

    Only messages carrying a ``usage`` dict affect the per-day aggregate, so
    those are the only days whose cache is stale after an edit/delete. The
    timestamp resolution mirrors :func:`_scan_costs_in_range` (message
    timestamp, else conversation-span fallback) so the invalidated days line
    up with the persisted aggregates.

    Args:
        messages: List of message dicts (or anything non-list → empty set).
        conv_start: Conversation created_at (epoch-ms) fallback for
            timestamp-less messages.
        conv_end: Conversation updated_at (epoch-ms) fallback.

    Returns:
        set[str] of day strings.
    """
    from .conversations import _safe_int_ts

    if not isinstance(messages, list):
        return set()
    cs = _safe_int_ts(conv_start or conv_end or 0)
    days: set = set()
    for msg in messages:
        if not isinstance(msg, dict) or not msg.get('usage'):
            continue
        ts = _safe_int_ts(msg.get('timestamp', 0))
        if not ts:
            ts = cs
        if not ts:
            continue
        d = _dt.datetime.fromtimestamp(ts / 1000)
        days.add(f'{d.year:04d}-{d.month:02d}-{d.day:02d}')
    return days


def _persisted_cost_dates(date_strs, *, owner_user_id):
    """Return the subset of ``date_strs`` that already have a persisted row.

    A ``daily_cost_cache`` row for a past day is a *settled snapshot* — the
    day is over, its messages are immutable, so its aggregate never needs to
    change again. This lookup lets :func:`invalidate_cost_cache_for_messages`
    recognise such days and refuse to drop them.

    Args:
        date_strs: Iterable of ``'YYYY-MM-DD'`` day strings.

    Returns:
        set[str] of the day strings that have a persisted cache row.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report persisted costs')
    dates = [d for d in date_strs if d]
    if not dates:
        return set()
    try:
        from lib.storage import get_storage_client
        result = get_storage_client().query('daily_cost.persisted_dates', {
            'user_id': owner_id, 'dates': dates,
        })
    except Exception as e:
        logger.warning('[DailyReport] Persisted-date lookup failed: %s', e)
        return set()
    return set(result['dates'])


def _should_pin_day(date_str, today_str, persisted_dates):
    """Whether a day is a settled, immutable snapshot that must NOT be dropped.

    A day is pinned iff it is strictly BEFORE today AND already persisted in
    ``daily_cost_cache``. Once a day is over its cost aggregate is final, so a
    later edit/delete of a *cross-midnight* conversation (whose messages span
    that past day) must leave the past day's cache untouched — otherwise a
    single such edit forces a full live rescan of that day on the next
    calendar open, permanently defeating the persistent cost cache.

    This predicate is load-bearing: neutering it to ``return False`` makes
    every touched day invalidate again (the old behaviour) and turns the
    regression guard red.
    """
    return bool(date_str) and date_str < today_str and date_str in persisted_dates


def invalidate_cost_cache_for_messages(
        messages, conv_start=0, conv_end=0, *, owner_user_id):
    """Scoped cost-cache invalidation for a delete/edit of specific messages.

    Removes ONLY the persisted per-day entries the given messages touch,
    instead of nuking the whole table. This is what conversation/message
    deletes must call: wiping all days forces the next calendar open to
    live-rescan the entire month (~10s), so a single delete would otherwise
    permanently defeat the persistent cost cache.

    Settled-day pinning: a touched day that is strictly BEFORE today AND
    already persisted is treated as an immutable snapshot and is NOT
    invalidated (see :func:`_should_pin_day`). This is what makes historical
    balances stay stable + instant: a cross-midnight edit today can only ever
    drop *today's* (unsettled) entry, never yesterday's. An explicit recompute
    (a direct :func:`invalidate_day_cost_cache` call or a forced report
    regeneration) is unaffected — it bypasses this path entirely.

    Args:
        messages: The messages being removed (or the whole conversation's
            messages when a conversation is deleted).
        conv_start: Conversation created_at (epoch-ms) — timestamp fallback.
        conv_end: Conversation updated_at (epoch-ms) — timestamp fallback.

    Returns:
        set[str] of the day strings that were actually invalidated (pinned
        settled days are excluded).
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report scoped cost invalidation')
    day_strs = _cost_days_for_messages(messages, conv_start, conv_end)
    if not day_strs:
        return set()

    today_str = _dt.date.today().isoformat()
    # Only past days can possibly be pinned; look those up in one query.
    past_days = {d for d in day_strs if d < today_str}
    persisted = (
        _persisted_cost_dates(past_days, owner_user_id=owner_id)
        if past_days else set()
    )
    pinned = {d for d in day_strs if _should_pin_day(d, today_str, persisted)}

    to_invalidate = day_strs - pinned
    for date_str in to_invalidate:
        invalidate_day_cost_cache(date_str, owner_user_id=owner_id)
    if pinned:
        logger.debug('[DailyReport] Pinned %d settled day(s) (not invalidated): %s',
                     len(pinned), sorted(pinned))
    if to_invalidate:
        logger.debug('[DailyReport] Scoped cost invalidation for %d day(s): %s',
                     len(to_invalidate), sorted(to_invalidate))
    return to_invalidate


def _get_monthly_costs(year, month, *, owner_user_id):
    """Return per-day cost breakdown for a month, using persistent cache.

    Strategy:
      - For past days (date < today): read from daily_cost_cache.  On miss,
        compute that day and persist (messages on past days are immutable).
      - For today: always compute fresh (conversations are still being
        written).  Do NOT persist today (it will be persisted once "today"
        rolls over — handled by the scheduled backfill / on-demand fill for
        any past day that still has no cache entry).
      - Future days: skipped entirely.

    Args:
        year: Calendar year (int).
        month: Calendar month 1-12 (int).

    Returns:
        dict mapping day-of-month (int) → {'cost': float,
            'conversations': {conv_id: {'name': str, 'cost': float, 'tokens': int}}}.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report monthly costs')
    t0 = time.monotonic()
    today = _dt.date.today()

    # Determine which past days need an on-demand compute+persist pass.
    if month < 12:
        next_month_start = _dt.date(year, month + 1, 1)
    else:
        next_month_start = _dt.date(year + 1, 1, 1)
    month_start = _dt.date(year, month, 1)

    # Past-day range for this month (days strictly before today):
    if month_start >= today:
        past_end = month_start  # no past days in this month
    elif next_month_start <= today:
        past_end = next_month_start  # whole month is past
    else:
        past_end = today  # part of month is past

    # 1) Load already-persisted day rows.  This returns ALL cached rows
    #    including zero-cost days — we need those to know they've been
    #    scanned already (so we don't rescan them), but we filter them out
    #    of the final response below to match legacy behavior.
    cached_days = _load_cached_day_costs(
        year, month, owner_user_id=owner_id)
    cached_hits = len(cached_days)
    days = {}  # response payload — only non-zero days

    # 2) Back-fill any past day that's missing from the cache by scanning
    #    just those days' range and persisting the result (zeros included,
    #    so they're not scanned again next time).
    missing_past_days = []
    if past_end > month_start:
        d = month_start
        while d < past_end:
            if d.day not in cached_days:
                missing_past_days.append(d)
            d += _dt.timedelta(days=1)

    if missing_past_days:
        # Scan the tight range covering only the missing past days.
        # In the common case (modal opened on a settled month with zero cache)
        # this is still just one scan of the month — but once filled, it's
        # free forever.
        range_start = missing_past_days[0]
        range_end = missing_past_days[-1] + _dt.timedelta(days=1)
        ms_range_start = int(_dt.datetime.combine(range_start, _dt.time.min).timestamp() * 1000)
        ms_range_end = int(_dt.datetime.combine(range_end, _dt.time.min).timestamp() * 1000)
        scanned = _scan_costs_in_range(
            ms_range_start, ms_range_end, year, month,
            owner_user_id=owner_id)

        for d_obj in missing_past_days:
            day_num = d_obj.day
            day_data = scanned.get(day_num, {'cost': 0.0, 'conversations': {}})
            date_str = f'{year:04d}-{month:02d}-{day_num:02d}'
            # Persist EVERY past day we've checked (including zero-cost) so
            # future calendar renders skip the scan entirely.
            _persist_day_cost(date_str, day_data, owner_user_id=owner_id)
            cached_days[day_num] = day_data

    # Copy non-zero cached/backfilled days into the response.
    for day_num, day_data in cached_days.items():
        if day_data.get('cost', 0) > 0:
            days[day_num] = day_data

    # 3) Compute today live (no persist — value isn't final yet).
    today_cost = None
    if year == today.year and month == today.month:
        day_start = _dt.datetime.combine(today, _dt.time.min)
        day_end = day_start + _dt.timedelta(days=1)
        ms_today_start = int(day_start.timestamp() * 1000)
        ms_today_end = int(day_end.timestamp() * 1000)
        scanned_today = _scan_costs_in_range(
            ms_today_start, ms_today_end, year, month,
            owner_user_id=owner_id)
        today_day_data = scanned_today.get(today.day,
                                           {'cost': 0.0, 'conversations': {}})
        if today_day_data['cost'] > 0:
            days[today.day] = today_day_data
            today_cost = today_day_data['cost']
        else:
            # Drop any stale persisted value for today (e.g. from a previous
            # day-boundary rollover where we cached yesterday's partial).
            days.pop(today.day, None)

    elapsed = time.monotonic() - t0
    total_cost = sum(d['cost'] for d in days.values())
    logger.info('[DailyReport] Monthly costs %d-%02d: %d days with costs, '
                '¥%.2f total (%d cache hits, %d live-computed past days, '
                'today=%s) in %.2fs',
                year, month, len(days), total_cost, cached_hits,
                len(missing_past_days),
                f'¥{today_cost:.2f}' if today_cost is not None else 'n/a',
                elapsed)
    return days
