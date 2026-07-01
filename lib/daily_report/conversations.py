"""Conversation extraction + LLM-driven analysis.

- ``_safe_int_ts`` — defensive timestamp coercion (str/float/None → int).
- ``_build_transcript_from_messages`` — compact transcript for LLM digest.
- ``_extract_convs_for_date`` — load DB rows touching a date, build digests.
- ``_count_convs_for_date`` — fast count variant for the conv-count endpoint.
- ``_analyse_conversations`` — orchestrator: digests → LLM → streams /
  tomorrow / unfinished, with yesterday write-back.
"""

import datetime as _dt
import random
import re
import time

from lib.log import get_logger

from .llm import _pick_persona, _run_llm_analysis
from .prompts import _QUOTES, _TODO_TOOL_DEFAULTS, _TODO_TOOL_MAP
from .storage import DEFAULT_USER_ID, _load_report, _save_report
from .todos import (
    _close_yesterday_remaining_todos,
    _fuzzy_todo_match,
    _get_yesterday_carryover,
    _get_yesterday_todo_accountability,
    _mark_yesterday_todos_done,
    _merge_manual_state,
)

logger = get_logger(__name__)


def _safe_int_ts(value, fallback=0):
    """Safely convert a timestamp value to int, handling str/float/None."""
    if value is None:
        return fallback
    try:
        return int(value)
    except (ValueError, TypeError) as e:
        logger.debug('[DailyReport] _safe_int_ts conversion failed for %r: %s', value, e)
        return fallback


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
            for r in (msg.get('toolRounds', []) or []):
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
                for sr in (msg.get('toolRounds', []) or []):
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


def _count_convs_for_date(date_str):
    """Count conversations with activity on a given date (DB query).

    Returns:
        int: Number of conversations, or 0 on error.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.utils import safe_json

    try:
        dt = _dt.date.fromisoformat(date_str)
    except ValueError as e:
        logger.debug('[DailyReport] Invalid date_str %r: %s', date_str, e)
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

    # ── Load yesterday's report ONCE for carryover / accountability / write-back.
    # Three helpers used to re-open the same JSON; now we pass this dict through
    # and save it exactly once at the end if any mutation occurred.
    try:
        _yday = (_dt.date.fromisoformat(target_date) - _dt.timedelta(days=1)).isoformat()
        _yday_report = _load_report(_yday)
    except (ValueError, TypeError) as e:
        logger.debug('[DailyReport] Yesterday date resolve failed for %s: %s',
                     target_date, e)
        _yday, _yday_report = None, None

    carryover = _get_yesterday_carryover(target_date, _prev=_yday_report)

    if not convs:
        logger.info('[DailyReport] No conversations to analyse for %s', target_date)
        # Surface yesterday's carryover as tomorrow items
        tomorrow_items = [
            {'id': f'todo-{_uuid.uuid4().hex[:8]}', 'text': t, 'done': False}
            for t in carryover[:12] if t
        ]
        empty_result = {
            'ok': True,
            'streams': [],
            'tomorrow': tomorrow_items,
            'carryover': carryover,
            'tasks': [],
            'quote': random.choice(_QUOTES),
            'persona': _pick_persona(stats),
            'stats': stats,
        }
        # Preserve manual edits on the empty-convs regen path too (a day with
        # no convs today may still carry the user's manually-added TODOs).
        try:
            _existing = _load_report(target_date)
            if _existing:
                _merge_manual_state(empty_result, _existing)
        except Exception as e:
            logger.warning('[DailyReport] Manual-state merge (empty) failed for %s: %s',
                           target_date, e)
        return empty_result

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
    todo_status = _get_yesterday_todo_accountability(target_date, _prev=_yday_report)
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
    # Collect stream titles+summaries for additional fuzzy matching
    _stream_hints = []
    for s in raw_streams:
        title = s.get('title', '')
        summary = s.get('summary', '')
        if title:
            _stream_hints.append(title)
        if summary:
            _stream_hints.append(summary)
    _yday_report, mark_changed = _mark_yesterday_todos_done(
        target_date, raw_yesterday_done, todo_status,
        stream_titles=_stream_hints,
        _prev=_yday_report, _defer_save=True,
    )

    # ── Close remaining yesterday TODOs → "unfinished" category ──
    # Once today's report is generated, yesterday's undone items are finalized
    # as "未完成" instead of lingering as "今日待办".
    unfinished, _yday_report, close_changed = _close_yesterday_remaining_todos(
        target_date, _prev=_yday_report, _defer_save=True,
    )

    # Coalesced single write for both mutations above (was 2× save previously).
    if _yday and _yday_report and (mark_changed or close_changed):
        try:
            _save_report(_yday, _yday_report)
            logger.info('[DailyReport] Coalesced yesterday writeback for %s: '
                        'mark_done=%d auto_closed=%d',
                        _yday, mark_changed, close_changed)
        except Exception as e:
            logger.warning('[DailyReport] Coalesced yesterday save failed for %s: %s',
                           _yday, e)

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

    result = {
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

    # ── Preserve the user's manual edits across regeneration ──
    # A regen is a fresh LLM analysis; without this it silently clobbers
    # manual stream-status overrides, TODO check-offs, and manually-added
    # TODOs that the edit endpoints persisted into the prior report.
    # Centralized here (single source of truth) so POST / backfill / async
    # generator all inherit it. Only meaningful on a regen (existing report
    # present); on first generation existing is None → no-op.
    try:
        _existing = _load_report(target_date)
        if _existing:
            _merge_manual_state(result, _existing)
    except Exception as e:
        logger.warning('[DailyReport] Manual-state merge failed for %s: %s',
                       target_date, e)

    return result
