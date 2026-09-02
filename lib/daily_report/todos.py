"""Cross-day TODO logic.

- ``_get_yesterday_carryover`` — load yesterday's unfinished items (text only).
- ``_get_today_inherited_todos`` — load yesterday's unfinished items as dicts.
- ``_get_yesterday_todo_accountability`` — (text, done_bool) tuples.
- ``_mark_yesterday_todos_done`` — fuzzy-match LLM signals to yesterday's items.
- ``_close_yesterday_remaining_todos`` — auto-close anything still undone.

Mutations on yesterday's report go through ``_save_report`` from
storage.py; callers that batch multiple mutations pass ``_defer_save=True``
and do a single coalesced save at the end.
"""

import datetime as _dt
import re

from lib.identity import require_user_id
from lib.log import get_logger

from .storage import _load_report, _save_report

logger = get_logger(__name__)


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


def _merge_manual_state(result, existing):
    """Preserve the user's manual edits across a report REGENERATION.

    A regenerated report is a fresh LLM analysis; without this merge it
    silently clobbers three kinds of user-owned state that the manual-edit
    endpoints persisted into the previous report JSON:

      1. **Manual stream status overrides** — ``update_task_status`` stamps
         ``_manual=True`` + a cycled status onto a stream. Streams get a
         brand-new ``stream-<uuid>`` id on every regen, so there is no stable
         id to match on; we fuzzy-match by title (the pragmatic identity for
         an LLM-derived work-stream) and carry the status + ``_manual`` flag.
      2. **TODO check-off state** — ``toggle_tomorrow_todo`` sets ``done`` on a
         ``tomorrow[]`` item. The fresh analysis rebuilds ``tomorrow[]`` from
         scratch, so we fuzzy-match text and carry ``done`` forward.
      3. **Manually-added TODOs** — ``add_manual_task`` appends a ``_manual``
         item to ``tomorrow[]``. If the new analysis didn't re-propose it, we
         re-append it verbatim (preserving its id / done / ``_manual``).

    Also preserves legacy ``tasks[]`` manual todos (``_todo``) — the merge the
    route handlers used to do ad-hoc, now centralized here so every caller of
    ``_analyse_conversations`` (POST / backfill / async generator) gets it.

    Mutates and returns ``result`` in place. No-op when ``existing`` is falsy.
    """
    if not existing or not isinstance(existing, dict):
        return result

    # 1) Manual stream status overrides.
    #    Identity is conv_ids overlap FIRST (the stable identity — the same
    #    conversations grouped together is the same work stream, regardless of
    #    how the LLM reworded the title on regen), with a strict normalized
    #    title match as fallback for streams that carry no conv_ids. We do NOT
    #    use the loose CJK fuzzy matcher here — it would bleed an override onto
    #    an unrelated stream (false positives on short/latin titles).
    manual_streams = [s for s in existing.get('streams', [])
                      if isinstance(s, dict) and s.get('_manual')]
    if manual_streams:
        for new_s in result.get('streams', []):
            new_ids = set(new_s.get('conv_ids') or [])
            new_title_n = _normalize_todo_text(new_s.get('title', ''))
            for old_s in manual_streams:
                old_ids = set(old_s.get('conv_ids') or [])
                old_title_n = _normalize_todo_text(old_s.get('title', ''))
                matched = False
                if new_ids and old_ids:
                    matched = bool(new_ids & old_ids)
                elif new_title_n and old_title_n:
                    matched = (new_title_n == old_title_n)
                if matched:
                    new_s['status'] = old_s.get('status', new_s.get('status'))
                    new_s['_manual'] = True
                    if new_s['status'] == 'done':
                        new_s['remaining'] = None
                    break

    old_tomorrow = [t for t in existing.get('tomorrow', []) if isinstance(t, dict)]
    new_tomorrow = result.setdefault('tomorrow', [])

    def _same_todo(a, b):
        """Strict TODO identity: normalized equality or containment (len>3).

        Deliberately stricter than ``_fuzzy_todo_match`` — for state
        PRESERVATION a false positive (marking an unrelated item done, or
        swallowing a manual TODO) is worse than a false negative (losing one
        edit), so we avoid the Jaccard/LCS signals that trip on short latin text.
        """
        na, nb = _normalize_todo_text(a), _normalize_todo_text(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        return len(na) > 3 and len(nb) > 3 and (na in nb or nb in na)

    # 2) Carry done-state onto matching new TODO items.
    done_texts = [t.get('text', '') for t in old_tomorrow if t.get('done')]
    if done_texts:
        for new_t in new_tomorrow:
            nt_text = new_t.get('text', '')
            if any(_same_todo(nt_text, dt) for dt in done_texts):
                new_t['done'] = True

    # 3) Re-append manually-added TODOs the fresh analysis didn't re-propose.
    new_texts = [t.get('text', '') for t in new_tomorrow]
    for old_t in old_tomorrow:
        if not old_t.get('_manual'):
            continue
        ot_text = old_t.get('text', '')
        if any(_same_todo(ot_text, nt) for nt in new_texts):
            continue  # LLM already re-proposed an equivalent item
        new_tomorrow.append(dict(old_t))  # preserve id + done + _manual

    # 4) Legacy manual tasks[] (``_todo``).
    manual_tasks = [t for t in existing.get('tasks', [])
                    if isinstance(t, dict) and t.get('_todo')]
    if manual_tasks:
        result.setdefault('tasks', []).extend(manual_tasks)

    return result


def _get_yesterday_carryover(target_date, *, owner_user_id, _prev=None):
    """Load yesterday's unfinished TODO items and blocked streams.

    Args:
        target_date: Today's date string 'YYYY-MM-DD'.
        _prev: Optional pre-loaded yesterday report dict — pass through
            when already loaded elsewhere to avoid a 2nd disk read.

    Returns a list of short carryover strings for LLM context.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report carryover')
    try:
        if _prev is None:
            dt = _dt.date.fromisoformat(target_date)
            yesterday = (dt - _dt.timedelta(days=1)).isoformat()
            prev = _load_report(yesterday, owner_user_id=owner_id)
        else:
            prev = _prev
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


def _get_today_inherited_todos(target_date, *, owner_user_id, _prev=None):
    """Load yesterday's unfinished TODO items as structured dicts for display.

    These are items from the previous day's ``tomorrow[]`` that haven't
    been checked off.  They appear in the current day's "今日待办" section.

    Args:
        target_date: Today's date string 'YYYY-MM-DD'.
        _prev: Optional pre-loaded yesterday report dict (avoids re-reading
            the JSON file from disk when the caller already has it).

    Returns list of dicts: [{id, text, done, _inherited, _origin_date}, ...].
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report inherited todos')
    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = (
            _prev if _prev is not None
            else _load_report(yesterday, owner_user_id=owner_id)
        )
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


def _get_yesterday_todo_accountability(
        target_date, *, owner_user_id, _prev=None):
    """Load yesterday's TODO items with completion status for LLM context.

    Args:
        target_date: Today's date string 'YYYY-MM-DD'.
        _prev: Optional pre-loaded yesterday report dict.

    Returns list of (text, done_bool) tuples for the LLM prompt.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report todo accountability')
    try:
        if _prev is None:
            dt = _dt.date.fromisoformat(target_date)
            yesterday = (dt - _dt.timedelta(days=1)).isoformat()
            prev = _load_report(yesterday, owner_user_id=owner_id)
        else:
            prev = _prev
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


def _mark_yesterday_todos_done(target_date, yesterday_done, todo_status,
                               *, owner_user_id, stream_titles=None,
                               _prev=None, _defer_save=False):
    """Write back completion status to yesterday's report.

    When the LLM identifies that yesterday's TODO items were addressed
    by today's work, this function marks those items as ``done: True``
    in yesterday's saved report JSON.

    Also checks if any of today's stream titles fuzzy-match yesterday's
    TODO items — if they do, auto-mark them as done (a stream about the
    same topic means the work was addressed).

    Args:
        target_date: Today's date string 'YYYY-MM-DD'.
        yesterday_done: List of TODO texts the LLM says were completed.
        todo_status: List of (text, done_bool) from yesterday's TODOs
                     (used to find items that were already done).
        stream_titles: Optional list of today's stream titles+summaries
                       for additional fuzzy matching.
        _prev: Optional pre-loaded yesterday report dict. When provided,
            this function mutates it in-place.
        _defer_save: If True, skip the _save_report disk write (caller is
            expected to save once at the end of the generation cycle).

    Returns:
        Tuple ``(prev_dict_or_None, changed_count)`` so the caller can
        perform a coalesced save covering multiple mutations.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report todo completion')
    all_done_texts = list(yesterday_done or [])
    # Also treat stream titles+summaries as potential done signals
    if stream_titles:
        all_done_texts.extend(stream_titles)

    if not all_done_texts or not todo_status:
        return _prev, 0

    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = (
            _prev if _prev is not None
            else _load_report(yesterday, owner_user_id=owner_id)
        )
        if not prev:
            return None, 0

        changed = 0
        for todo in prev.get('tomorrow', []):
            if todo.get('done') and not todo.get('_auto_closed'):
                continue  # genuinely done (manually or by previous analysis)
            todo_text = todo.get('text', '')
            if not todo_text:
                continue
            # Check if LLM flagged this as done, or if any stream title
            # matches (fuzzy match since LLM may slightly alter text)
            for done_text in all_done_texts:
                if not isinstance(done_text, str):
                    continue
                if _fuzzy_todo_match(todo_text, done_text):
                    todo['done'] = True
                    todo.pop('_auto_closed', None)  # promote to genuinely done
                    changed += 1
                    logger.debug('[DailyReport] Marked yesterday TODO as done: %s',
                                 todo_text)
                    break

        if changed and not _defer_save:
            _save_report(yesterday, prev, owner_user_id=owner_id)
            logger.info('[DailyReport] Wrote back %d completed TODOs to %s',
                        changed, yesterday)
        return prev, changed
    except Exception as e:
        logger.warning('[DailyReport] Failed to write back yesterday TODOs: %s', e)
        return _prev, 0


def _close_yesterday_remaining_todos(
        target_date, *, owner_user_id, _prev=None, _defer_save=False):
    """Close ALL remaining undone TODOs in yesterday's report.

    Once today's report is generated, yesterday's plan is finalized:
    items already marked done by ``_mark_yesterday_todos_done()`` stay done;
    everything else is auto-closed and returned as "unfinished".

    This ensures ``_get_today_inherited_todos()`` returns empty after
    report generation, replacing the ambiguous "今日待办" with a clear
    "未完成" (Unfinished) category.

    On force re-generation, items previously ``_auto_closed`` are
    re-included in the unfinished list (they remain closed).

    Args:
        target_date: Today's date string 'YYYY-MM-DD'.
        _prev: Optional pre-loaded yesterday report dict (mutated in place).
        _defer_save: If True, skip the _save_report write. Caller must save.

    Returns:
        Tuple ``(unfinished_list, prev_dict_or_None, changed_count)``.
    """
    owner_id = require_user_id(
        owner_user_id, context='daily report todo closure')
    try:
        dt = _dt.date.fromisoformat(target_date)
        yesterday = (dt - _dt.timedelta(days=1)).isoformat()
        prev = (
            _prev if _prev is not None
            else _load_report(yesterday, owner_user_id=owner_id)
        )
        if not prev:
            return [], None, 0

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

        if changed and not _defer_save:
            _save_report(yesterday, prev, owner_user_id=owner_id)
            logger.info('[DailyReport] Auto-closed %d remaining TODOs from %s',
                        changed, yesterday)

        return unfinished, prev, changed
    except Exception as e:
        logger.warning('[DailyReport] Failed to close yesterday remaining TODOs: %s', e)
        return [], _prev, 0
