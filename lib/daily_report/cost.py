"""Server-side cost calculation for the daily-report calendar.

Strategy: past days come from ``daily_cost_cache`` (DB); current day is
always live-computed; the calendar endpoint wraps the whole thing in a
30-second in-process TTL cache to absorb burst-polling.
"""

import datetime as _dt
import re
import time

from lib.cost import normalize_usage
from lib.log import get_logger

from .storage import DEFAULT_USER_ID

logger = get_logger(__name__)


# ── Calendar endpoint TTL cache (avoids 5s+ repeated full-table scans) ──
# Shared with storage._save_report / invalidate_day_cost_cache /
# routes.daily_report.get_calendar_month — they all pop / clear the
# entry for a month when a report is saved or cost data invalidated.
_calendar_cache: dict = {}     # (year, month) → {'data': dict, 'ts': monotonic, ...}
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


def _calc_msg_cost_cny(usage, model_or_preset='', provider_id=''):
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

    mp = lookup_pricing(model_id, provider_id=provider_id)
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


def _scan_costs_in_range(ms_start, ms_end, year=None, month=None):
    """Scan the conversations table and build per-day cost breakdowns in a range.

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
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.utils import safe_json

    # _safe_int_ts lives in conversations.py to keep it close to its
    # transcript callers; import lazily to avoid the circular import that
    # would otherwise form (cost ↔ conversations).
    from .conversations import _safe_int_ts

    try:
        db = get_thread_db(DOMAIN_CHAT)
        # Bound on both ends so only the target window is scanned
        # (previous version omitted the upper bound, causing full-history scans
        #  for any month-open).
        rows = db.execute(
            'SELECT id, title, messages, created_at, updated_at, settings '
            'FROM conversations WHERE user_id=? AND '
            'COALESCE(updated_at, created_at, 0) >= ? AND '
            'COALESCE(created_at, updated_at, 0) < ? '
            'ORDER BY updated_at DESC',
            (DEFAULT_USER_ID, ms_start, ms_end)
        ).fetchall()
    except Exception as e:
        logger.error('[DailyReport] Cost DB query failed range=[%d,%d): %s',
                     ms_start, ms_end, e, exc_info=True)
        return {}

    days = {}   # day_num → {cost, conversations}

    for r in rows:
        msgs = safe_json(r['messages'], default=[], label='cost-messages')
        if not isinstance(msgs, list) or not msgs:
            continue

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

            if ts < ms_start or ts >= ms_end:
                continue

            d = _dt.datetime.fromtimestamp(ts / 1000)
            if year is not None and month is not None:
                if d.year != year or d.month != month:
                    continue
            day_num = d.day

            msg_model = (msg.get('model') or msg.get('preset')
                         or msg.get('effort') or conv_model)
            msg_provider = msg.get('provider_id') or msg.get('providerId') or ''

            cost_cny = _calc_msg_cost_cny(usage, msg_model, msg_provider)
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


def _load_cached_day_costs(year, month):
    """Load persisted per-day costs for a given month from daily_cost_cache.

    Returns:
        dict mapping day-of-month (int) → {'cost': float, 'conversations': {...}}
        for days that have cached entries.  Days without entries are absent.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.utils import safe_json

    try:
        db = get_thread_db(DOMAIN_CHAT)
        prefix = f'{year:04d}-{month:02d}-'
        # LIKE 'YYYY-MM-%' matches all days in the month
        rows = db.execute(
            'SELECT date, cost, conversations_json FROM daily_cost_cache '
            'WHERE user_id=? AND date LIKE ?',
            (DEFAULT_USER_ID, prefix + '%')
        ).fetchall()
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
        # conversations_json is TEXT (SQLite) or JSONB rendered as string (PG,
        # see _jsonb_as_string in _core.py).  Either way, safe_json parses it.
        convs = safe_json(r['conversations_json'], default={},
                          label='cached-day-convs')
        if not isinstance(convs, dict):
            convs = {}
        out[day_num] = {'cost': round(cost_val, 4), 'conversations': convs}
    return out


def _persist_day_cost(date_str, day_data):
    """Write a single day's cost aggregate to daily_cost_cache.

    Args:
        date_str: 'YYYY-MM-DD'.
        day_data: {'cost': float, 'conversations': {conv_id: {...}}}.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import DAILY_COST_CACHE, upsert

    try:
        db = get_thread_db(DOMAIN_CHAT)
        # Use json_dumps_pg so that PG's JSONB column accepts the payload
        # (strips \u0000 / lone surrogates that would otherwise be rejected).
        # SQLite treats this as plain TEXT, so behavior is identical.
        convs_json = json_dumps_pg(day_data.get('conversations', {}))
        # Backend-agnostic composite-PK (user_id, date) UPSERT. retry=True
        # preserves the contention/connection-loss retry of the former
        # db_execute_with_retry call (it commits internally too).
        upsert(db, DAILY_COST_CACHE, {
            'user_id': DEFAULT_USER_ID,
            'date': date_str,
            'cost': float(day_data.get('cost', 0.0)),
            'conversations_json': convs_json,
            'computed_at': int(time.time() * 1000),
        }, retry=True, commit=True)
    except Exception as e:
        logger.warning('[DailyReport] Persist day cost %s failed: %s',
                       date_str, e)


def invalidate_day_cost_cache(date_str=None):
    """Invalidate persisted per-day cost cache entries.

    Args:
        date_str: If given, remove only that day ('YYYY-MM-DD').
                  If None, clear all entries (e.g. on bulk delete).
    """
    from lib.database import DOMAIN_CHAT, get_thread_db

    try:
        db = get_thread_db(DOMAIN_CHAT)
        if date_str:
            db.execute(
                'DELETE FROM daily_cost_cache WHERE user_id=? AND date=?',
                (DEFAULT_USER_ID, date_str)
            )
            logger.debug('[DailyReport] Invalidated day-cost cache for %s', date_str)
        else:
            db.execute('DELETE FROM daily_cost_cache WHERE user_id=?',
                       (DEFAULT_USER_ID,))
            logger.info('[DailyReport] Invalidated ALL day-cost cache entries')
        db.commit()
        # Also drop the in-process calendar TTL cache so the next request
        # picks up the change.
        _calendar_cache.clear()
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


def _persisted_cost_dates(date_strs):
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
    dates = [d for d in date_strs if d]
    if not dates:
        return set()
    from lib.database import DOMAIN_CHAT, get_thread_db

    try:
        db = get_thread_db(DOMAIN_CHAT)
        placeholders = ','.join('?' for _ in dates)
        rows = db.execute(
            'SELECT date FROM daily_cost_cache '
            f'WHERE user_id=? AND date IN ({placeholders})',
            (DEFAULT_USER_ID, *dates)
        ).fetchall()
    except Exception as e:
        logger.warning('[DailyReport] Persisted-date lookup failed: %s', e)
        return set()
    return {r['date'] for r in rows}


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


def invalidate_cost_cache_for_messages(messages, conv_start=0, conv_end=0):
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
    day_strs = _cost_days_for_messages(messages, conv_start, conv_end)
    if not day_strs:
        return set()

    today_str = _dt.date.today().isoformat()
    # Only past days can possibly be pinned; look those up in one query.
    past_days = {d for d in day_strs if d < today_str}
    persisted = _persisted_cost_dates(past_days) if past_days else set()
    pinned = {d for d in day_strs if _should_pin_day(d, today_str, persisted)}

    to_invalidate = day_strs - pinned
    for date_str in to_invalidate:
        invalidate_day_cost_cache(date_str)
    if pinned:
        logger.debug('[DailyReport] Pinned %d settled day(s) (not invalidated): %s',
                     len(pinned), sorted(pinned))
    if to_invalidate:
        logger.debug('[DailyReport] Scoped cost invalidation for %d day(s): %s',
                     len(to_invalidate), sorted(to_invalidate))
    return to_invalidate


def _get_monthly_costs(year, month):
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
    cached_days = _load_cached_day_costs(year, month)
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
        scanned = _scan_costs_in_range(ms_range_start, ms_range_end, year, month)

        for d_obj in missing_past_days:
            day_num = d_obj.day
            day_data = scanned.get(day_num, {'cost': 0.0, 'conversations': {}})
            date_str = f'{year:04d}-{month:02d}-{day_num:02d}'
            # Persist EVERY past day we've checked (including zero-cost) so
            # future calendar renders skip the scan entirely.
            _persist_day_cost(date_str, day_data)
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
        scanned_today = _scan_costs_in_range(ms_today_start, ms_today_end,
                                             year, month)
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
