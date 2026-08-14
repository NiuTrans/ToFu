"""lib.conversations.project_watch — the human's standing "watch list" (Pillar #7).

The status lane (``project_status.py``) lets the human ASK the project a one-shot
question and get an ephemeral answer. This module is its DURABLE counterpart: a
standing list of things the HUMAN cares about — concerns, open questions,
goals — that the brain ADDRESSES on a recurring basis, keeping an append-only
trail of its responses per item so the human can see how the answer to a
concern DRIFTS over time.

Owner-locked decisions (2026-07-08; goals-inject-directly 2026-07-30):
  1. **A GOAL is live because it exists — it never travels through the
     charter.** ``kind=goal`` items render as their own ``[PROJECT GOALS]``
     prompt block (see :func:`render_goals_injection_block`), read straight
     from this lane. ``concern`` / ``question`` stay human-facing-only and are
     NEVER injected; their one bridge to agents is still
     ``promote_watch_item`` → ``commit_charter(add_decision=…)``.

     This replaces the 2026-07-30-morning design in which a goal was COPIED
     into the charter's north-star ``content`` column. That copy is what forced
     every mechanism now deleted: two copies of one sentence need "which side
     moved?" detection, a three-state badge, a replacement-preview card, and a
     version gate. One copy needs none of them. The owner's framing: *goals are
     goals; they should work without being in the charter.* A goal's state
     model is therefore the whole of: it exists, and it is not resolved.

     **Single-goal-ness is NOT enforced and deliberately so.** Several goals is
     a legitimate thing for a project to have; they all inject. The charter's
     ``content`` column remains the human's separate north-star statement.
  2. **Append-only response trail per item** (bounded), not latest-only — the
     drift is the signal.
  3. **Cadence = on-tab-open + event-driven now** (reuse the staleness gate so a
     quiescent project costs nothing). The closed-panel scheduler cadence is a
     deliberately-deferred follow-up.
  4. **The promotion verdict is COMPUTED, never stored** — and applies ONLY to
     concern/question now. The ``promoted`` column is a one-shot audit marker
     that cannot answer "is this reaching agents right now": measured on the
     live project, ``promoted=1`` while ``read_charter()`` reported
     ``exists=False``, i.e. the badge asserted something already untrue.
     :func:`promotion_state` recomputes it against the LIVE charter per read.
     A goal has no promotion state at all — it is injected or it is resolved.

All functions key STRICTLY on ``project_path`` / ``item_id`` — never a
process-global. Best-effort throughout; the address generator never raises.
"""

from __future__ import annotations

import json
import threading
import time

from lib.database import (
    DOMAIN_CHAT,
    allocate_scoped_sequence,
    db_execute_with_retry,
    get_thread_db,
    write_transaction,
)
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

# Keep at most this many responses per item (bounded trail; pruned on insert).
_RESPONSES_KEEP = 100
# Soft cap on a human-authored item's text. A goal is injected into every
# sibling's prompt verbatim, so this is also the per-goal prompt-weight ceiling.
# NOT tied to project_charter._CONTENT_MAX_CHARS any more: a goal is no longer
# copied into that column, so there is no "adopt the other side" direction that
# could truncate, and the two texts are now genuinely independent settings.
_ITEM_TEXT_MAX = 8000
# Bounded response length.
_RESPONSE_MAX_CHARS = 2000

# Total budget for the [PROJECT GOALS] block. Goals ride EVERY turn of EVERY
# sibling conversation, so an unbounded lane would let one long paste tax the
# whole project forever. Oldest-first truncation is deliberate: the block states
# plainly when it elided goals rather than silently shipping a subset.
_GOALS_BLOCK_MAX_CHARS = 4000

VALID_KINDS = ('concern', 'question', 'goal')
VALID_STATUSES = ('open', 'resolved')

# The COMPUTED promotion states for concern/question (decision 4). Never
# persisted. A GOAL never has one of these — it is injected, or it is resolved.
PROMOTION_NONE = 'none'      # no promotion on record → offer "add to charter"
PROMOTION_ACTIVE = 'active'  # item text IS a live committed decision

_SYSTEM_PROMPTS = {
    'question': (
        'You are the project brain answering a specific QUESTION the human '
        'owner is tracking about their project. Answer it directly and '
        'concretely using ONLY the project state provided. If the state does '
        'not contain the answer, say so plainly — do NOT invent facts.'),
    'concern': (
        'You are the project brain addressing a CONCERN the human owner is '
        'tracking. Using ONLY the project state provided, assess whether the '
        'concern is being addressed, is at risk, or is currently a non-issue, '
        'and say why. Be concrete; if the state does not speak to it, say so.'),
    'goal': (
        'You are the project brain reporting on a GOAL the human owner is '
        'tracking. Using ONLY the project state provided, report concrete '
        'progress toward the goal and whether current in-flight work is '
        'aligned with it or drifting. If the state does not speak to it, say '
        'so plainly.'),
}
_COMMON_SUFFIX = (
    '\nBe concise and dense (2-4 sentences). No greetings or filler. Use the '
    'same language as the item text.')


_now_ms = now_ms


# ══════════════════════════════════════════════════════════════════════
#  Human CRUD — the human authors / edits / resolves / deletes items
# ══════════════════════════════════════════════════════════════════════

def add_watch_item(project_path: str, kind: str, text: str, *,
                   created_by_conv: str = '') -> dict:
    """Add a human-authored watch item. Returns ``{'ok', 'item'?, 'error'?}``."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    text = (text or '').strip()[:_ITEM_TEXT_MAX]
    kind = (kind or 'concern').strip().lower()
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if kind not in VALID_KINDS:
        return {'ok': False, 'error': 'invalid kind'}
    if not text:
        return {'ok': False, 'error': 'empty text'}
    item_id = short_id('watch_', 16)
    ts = _now_ms()
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(
            db,
            'INSERT INTO project_watch_items '
            '(item_id, project_path, kind, text, status, promoted, '
            ' response_fingerprint, created_by_conv, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (item_id, project_path, kind, text, 'open', 0, '',
             created_by_conv or '', ts, ts))
    except Exception as e:
        logger.error('[Watch] add failed proj=%.40r: %s', project_path, e,
                     exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_added', project_path=project_path, item_id=item_id,
              kind=kind)
    return {'ok': True, 'item': {
        'item_id': item_id, 'kind': kind, 'text': text, 'status': 'open',
        'promotedAudit': False, 'promotionState': PROMOTION_NONE,
        'divergedSide': '', 'injected': kind == 'goal',
        'created_at': ts, 'updated_at': ts, 'responses': []}}


def _get_item_row(db, item_id: str):
    return db.execute(
        'SELECT item_id, project_path, kind, text, status, promoted, '
        '       response_fingerprint, created_by_conv, created_at, updated_at '
        'FROM project_watch_items WHERE item_id=?', (item_id,)).fetchone()


def edit_watch_item(item_id: str, *, text: str | None = None,
                    kind: str | None = None) -> dict:
    """Edit a watch item's text and/or kind. Editing the text CLEARS the
    response fingerprint so the next address re-synthesizes (the question
    changed). Returns ``{'ok', 'error'?}``."""
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = _get_item_row(db, item_id)
        if not row:
            return {'ok': False, 'error': 'not found'}
        new_text = row['text'] if text is None else (text or '').strip()[:_ITEM_TEXT_MAX]
        new_kind = row['kind'] if kind is None else (kind or '').strip().lower()
        if not new_text:
            return {'ok': False, 'error': 'empty text'}
        if new_kind not in VALID_KINDS:
            return {'ok': False, 'error': 'invalid kind'}
        text_changed = new_text != row['text']
        updated_at = max(_now_ms(), int(row['updated_at'] or 0) + 1)
        cursor = db_execute_with_retry(
            db,
            'UPDATE project_watch_items SET text=?, kind=?, updated_at=?'
            + (', response_fingerprint=?' if text_changed else '')
            + ' WHERE item_id=? AND text=? AND kind=? AND updated_at=?',
            ((new_text, new_kind, updated_at, '', item_id,
              row['text'], row['kind'], row['updated_at']) if text_changed
             else (new_text, new_kind, updated_at, item_id,
                   row['text'], row['kind'], row['updated_at'])),
            return_cursor=True)
        if cursor.rowcount != 1:
            return {'ok': False, 'error': 'edit_conflict'}
    except Exception as e:
        logger.error('[Watch] edit failed item=%s: %s', item_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_edited', item_id=item_id)
    return {'ok': True}


def set_watch_status(item_id: str, status: str) -> dict:
    """Mark an item open|resolved. Returns ``{'ok', 'error'?}``.

    For a GOAL this is the lever that withdraws it from every sibling's prompt:
    :func:`render_goals_injection_block` ships open goals only. That is the
    whole of a goal's lifecycle — no separate "un-promote" step to forget.

    Deliberately does NOT touch the charter. The charter's own north-star
    ``content`` is a human-owned statement edited in the Charter panel; a
    bookkeeping action on a watch card must never rewrite shared intent as a
    side effect."""
    status = (status or '').strip().lower()
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    if status not in VALID_STATUSES:
        return {'ok': False, 'error': 'invalid status'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        cursor = db_execute_with_retry(
            db,
            'UPDATE project_watch_items SET status=?, updated_at=? '
            'WHERE item_id=?', (status, _now_ms(), item_id),
            return_cursor=True)
        if cursor.rowcount != 1:
            return {'ok': False, 'error': 'not found'}
    except Exception as e:
        logger.error('[Watch] status failed item=%s: %s', item_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_status', item_id=item_id, status=status)
    return {'ok': True}


def delete_watch_item(item_id: str) -> dict:
    """Delete a watch item AND its response trail. Returns ``{'ok', 'error'?}``.

    Deleting a GOAL card removes it from every sibling's prompt on the next
    turn — the card IS the goal, so there is no orphaned copy left behind
    anywhere. Deliberately does NOT touch the charter: the charter's north-star
    ``content`` is separately human-owned and is edited in the Charter panel."""
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        with write_transaction(db, label='project-watch-delete'):
            db.execute('DELETE FROM project_watch_responses WHERE item_id=?',
                       (item_id,))
            db.execute('DELETE FROM project_watch_items WHERE item_id=?',
                       (item_id,))
    except Exception as e:
        logger.error('[Watch] delete failed item=%s: %s', item_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_deleted', item_id=item_id)
    return {'ok': True}


# ══════════════════════════════════════════════════════════════════════
#  Read — items + their append-only response trails
# ══════════════════════════════════════════════════════════════════════

def _response_trail(db, item_id: str, limit: int = 20) -> list[dict]:
    rows = db.execute(
        'SELECT seq, response, pillar_state, trigger, ts '
        'FROM project_watch_responses WHERE item_id=? '
        'ORDER BY seq DESC LIMIT ?', (item_id, max(1, min(limit, _RESPONSES_KEEP)))
    ).fetchall()
    out = []
    for r in rows:
        try:
            ps = json.loads(r['pillar_state']) if r['pillar_state'] else {}
        except (TypeError, ValueError) as e:
            logger.debug('[Watch] response pillar_state parse failed, defaulting: %s', e)
            ps = {}
        out.append({'seq': int(r['seq']), 'response': r['response'] or '',
                    'pillar_state': ps, 'trigger': r['trigger'] or '',
                    'ts': int(r['ts'] or 0)})
    return out


# ══════════════════════════════════════════════════════════════════════
#  The COMPUTED promotion verdict — concern/question ONLY (decision 4)
# ══════════════════════════════════════════════════════════════════════

def _norm(text: str) -> str:
    """Normalize text for promotion-equality comparison.

    Strips outer whitespace and collapses every internal whitespace run
    (including newlines) to one space, so a reflowed paragraph still counts as
    the same item. Case is deliberately PRESERVED — capitalization carries
    meaning, and folding it would call two genuinely different texts equal.
    """
    return ' '.join((text or '').split())


def promotion_state(item: dict, charter: dict) -> dict:
    """Compute — never read — whether a concern/question is in the charter.

    The stored ``promoted`` boolean cannot answer this: it records that a
    promotion once happened, not that its effect survives. The charter can be
    deleted or the decision FIFO-evicted, and the boolean still reads 1 while
    nothing reaches the model. (Measured on the live project: promoted=1,
    read_charter() exists=False, injection block 0 bytes.)

    A ``goal`` ALWAYS returns ``none``: goals do not go through the charter at
    all (decision 1), so "is it promoted" is not a question about them. Their
    prompt presence is decided by :func:`render_goals_injection_block`, and the
    UI renders that as an injected/not-injected fact rather than a promotion.

    Returns ``{state, divergedSide}``. ``divergedSide`` is retained as an
    always-empty key so a stale frontend reading it gets a falsy value instead
    of ``undefined``. Pure; never raises.
    """
    kind = (item or {}).get('kind') or 'concern'
    if kind == 'goal':
        return {'state': PROMOTION_NONE, 'divergedSide': ''}
    item_text = (item or {}).get('text') or ''
    charter = charter or {}
    live_norms = []
    for d in (charter.get('decisions') or []):
        txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
        if _norm(txt):
            live_norms.append(_norm(txt))
    norm_item = _norm(item_text)
    # The bridge prefixes the committed text ("[Concern — promoted by owner] …"),
    # so containment — not equality — is the right test here.
    if norm_item and any(norm_item in live for live in live_norms):
        return {'state': PROMOTION_ACTIVE, 'divergedSide': ''}
    return {'state': PROMOTION_NONE, 'divergedSide': ''}


# ══════════════════════════════════════════════════════════════════════
#  The [PROJECT GOALS] prompt block — the ONE way a goal reaches agents
# ══════════════════════════════════════════════════════════════════════

def render_goals_injection_block(project_path: str) -> str:
    """Render this project's OPEN goals for per-turn prompt injection.

    This is the whole of "a goal takes effect": the human writes a goal in
    Status & Focus and it ships in every sibling conversation's prompt. No
    promotion step, no charter copy, no version gate — decision 1.

    Scope rules, each of which is a behaviour the human can predict:
      * ``kind='goal'`` ONLY. Concerns and questions are things the human is
        TRACKING, not intent they are declaring; injecting a worry as if it were
        direction would steer the project on an unresolved question.
      * ``status='open'`` ONLY, so resolving a goal withdraws it from prompts —
        that is the one lever that stops it being injected, and it is the same
        lever the human already uses for the recurring cadence.
      * The brain's synthesized RESPONSES are deliberately excluded. They are
        the brain talking to the human about progress; feeding them back to
        agents would turn one summarizer's opinion into project direction.

    Returns '' when the project has no open goals, so an empty lane adds ZERO
    prompt weight (same contract as the charter block). Never raises.
    """
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return ''
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT text FROM project_watch_items "
            "WHERE project_path=? AND kind='goal' AND status='open' "
            "ORDER BY created_at ASC", (project_path,)).fetchall()
    except Exception as e:
        logger.warning('[Watch] goals block read failed proj=%.40r: %s',
                       project_path, e)
        return ''
    texts = [(r['text'] or '').strip() for r in rows]
    texts = [t for t in texts if t]
    if not texts:
        return ''
    header = (
        '[PROJECT GOALS] — what the human owner wants this project to achieve. '
        'They set these directly; treat them as standing intent that outranks '
        'local convenience, and say so when a request conflicts with one.')
    body, elided = [], 0
    used = 0
    for t in texts:
        entry = f'  • {t}'
        if used + len(entry) > _GOALS_BLOCK_MAX_CHARS and body:
            elided = len(texts) - len(body)
            break
        body.append(entry)
        used += len(entry)
    lines = [header, ''] + body
    if elided:
        lines.append(f'  … and {elided} more goal(s) not shown (block is '
                     f'capped at {_GOALS_BLOCK_MAX_CHARS} chars).')
    return '\n'.join(lines)


def _row_to_item(db, row, *, with_responses: bool = True,
                 resp_limit: int = 20, charter: dict | None = None) -> dict:
    kind = row['kind']
    item = {
        'item_id': row['item_id'], 'kind': kind, 'text': row['text'] or '',
        'status': row['status'] or 'open',
        # AUDIT ONLY — never render this. `promotionState` below is the live
        # verdict; see promotion_state. Kept under a renamed key so a stale
        # consumer that blindly renders a truthy flag fails loudly on a missing
        # key instead of silently resurrecting the lying badge.
        'promotedAudit': bool(row['promoted']),
        'created_at': int(row['created_at'] or 0),
        'updated_at': int(row['updated_at'] or 0),
    }
    # A goal's prompt presence is a FACT about this lane, not a promotion:
    # open ⇒ it is in every sibling's prompt (render_goals_injection_block).
    # Computed here so the badge cannot drift from the injector's own rule.
    item['injected'] = bool(kind == 'goal' and item['status'] == 'open')
    verdict = promotion_state(item, charter or {})
    item['promotionState'] = verdict['state']
    item['divergedSide'] = verdict['divergedSide']
    if with_responses:
        item['responses'] = _response_trail(db, row['item_id'], limit=resp_limit)
    return item


def list_watch_items(project_path: str, *, include_resolved: bool = True,
                     resp_limit: int = 20) -> dict:
    """List watch items for a project (newest-updated-first) with their response
    trails, each goal's ``injected`` fact and each concern/question's LIVE
    promotion verdict.

    The charter is read ONCE here and threaded into every row so the whole list
    is judged against one snapshot. Returns ``{'items': [...],
    'charterVersion': int}``. ``charterContent`` is deliberately NO LONGER
    returned: it existed only to render the goal replacement preview, and goals
    no longer touch the charter — shipping it would invite a new consumer to
    rebuild exactly the coupling this design removes. Empty on no project /
    error."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return {'items': [], 'charterVersion': 0}
    charter = {}
    try:
        from lib.conversations.project_charter import read_charter
        charter = read_charter(project_path)
    except Exception as e:
        # Degrade to "no charter": concern/question then read as not-promoted
        # rather than falsely claiming to be in it. Goals are unaffected —
        # their injected fact does not consult the charter at all.
        logger.warning('[Watch] charter read failed proj=%.40r: %s',
                       project_path, e)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        sql = ('SELECT item_id, project_path, kind, text, status, promoted, '
               '       response_fingerprint, created_by_conv, created_at, updated_at '
               'FROM project_watch_items WHERE project_path=?')
        params = [project_path]
        if not include_resolved:
            sql += " AND status='open'"
        sql += ' ORDER BY updated_at DESC'
        rows = db.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        logger.warning('[Watch] list failed proj=%.40r: %s', project_path, e)
        return {'items': [], 'charterVersion': 0}
    return {'items': [_row_to_item(db, r, resp_limit=resp_limit, charter=charter)
                      for r in rows],
            'charterVersion': int(charter.get('version') or 0)}


# ══════════════════════════════════════════════════════════════════════
#  Address — the brain synthesizes a recurring response per item
# ══════════════════════════════════════════════════════════════════════

def _item_fingerprint(item_text: str, pillar_state: dict) -> str:
    """Change key gating whether an item needs a fresh response: the item text
    (so an edit re-addresses) + the SAME coarse pillar fingerprint the status
    lane uses (so sibling progress re-addresses)."""
    from lib.conversations.project_status import _fingerprint as _pfp
    return f'{hash(item_text)}::{_pfp(pillar_state)}'


def generate_item_response(kind: str, item_text: str, pillar_state: dict) -> str:
    """Synthesize ONE response to a watch item from live pillar state via the
    cheap model. Returns '' on failure (caller keeps prior response)."""
    from lib.conversations.project_status import _build_synthesis_source
    source = _build_synthesis_source(pillar_state)
    if not (item_text or '').strip():
        return ''
    system = _SYSTEM_PROMPTS.get(kind, _SYSTEM_PROMPTS['concern']) + _COMMON_SUFFIX
    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': system},
                {'role': 'user',
                 'content': f'Project state:\n\n{source}\n\n'
                            f'The {kind} I am tracking: {item_text}\n\nResponse:'},
            ],
            max_tokens=700, temperature=0.3, capability='cheap',
            log_prefix='[Watch]',
        )
    except Exception as e:
        logger.warning('[Watch] synthesis failed after %.1fs: %s',
                       time.time() - started, e)
        return ''
    text = (content or '').strip()
    if len(text) > _RESPONSE_MAX_CHARS:
        text = text[:_RESPONSE_MAX_CHARS].rstrip() + '…'
    return text


def _persist_response(db, item_id: str, project_path: str, response: str,
                      pillar_state: dict, trigger: str, *,
                      fingerprint_guard: tuple[str, int, str] | None = None
                      ) -> dict | None:
    try:
        pillar_json = json.dumps(pillar_state, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[Watch] pillar_state dump failed, defaulting: %s', e)
        pillar_json = '{}'
    ts = _now_ms()
    try:
        with write_transaction(db, label='project-watch-response-append'):
            if fingerprint_guard is not None:
                expected_fp, expected_updated_at, new_fp = fingerprint_guard
                updated_at = max(ts, int(expected_updated_at or 0) + 1)
                cursor = db.execute(
                    'UPDATE project_watch_items '
                    'SET response_fingerprint=?, updated_at=? '
                    'WHERE item_id=? AND response_fingerprint=? '
                    'AND updated_at=?',
                    (new_fp, updated_at, item_id, expected_fp,
                     expected_updated_at))
                if cursor.rowcount != 1:
                    return {'conflict': True}
            seq = allocate_scoped_sequence(
                db, 'project_watch_responses', item_id)
            db.execute(
                'INSERT INTO project_watch_responses '
                '(item_id, seq, project_path, response, pillar_state, trigger, ts) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (item_id, seq, project_path, response, pillar_json,
                 trigger or 'manual', ts))
            if seq > _RESPONSES_KEEP:
                db.execute('DELETE FROM project_watch_responses '
                           'WHERE item_id=? AND seq <= ?',
                           (item_id, seq - _RESPONSES_KEEP))
    except Exception as e:
        logger.warning('[Watch] persist response failed item=%s: %s', item_id, e)
        return None
    return {'seq': seq, 'response': response, 'pillar_state': pillar_state,
            'trigger': trigger or 'manual', 'ts': ts}


def address_watch_item(item_id: str, *, trigger: str = 'manual',
                       force: bool = False) -> dict | None:
    """Ensure ONE open item has a fresh response; return the latest response.

    Reads live pillar state; if the item's fingerprint (text + pillar
    fingerprint) changed since the last response (or ``force``), synthesizes a
    fresh response and appends it. Otherwise returns the latest WITHOUT an LLM
    call (staleness gate). Never raises. Returns the latest response dict, or
    None when the item is missing / resolved / no narrative could be produced.
    """
    if not item_id:
        return None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = _get_item_row(db, item_id)
    except Exception as e:
        logger.warning('[Watch] address read failed item=%s: %s', item_id, e)
        return None
    if not row:
        return None
    if (row['status'] or 'open') != 'open' and not force:
        # A resolved item isn't re-addressed on the recurring path.
        trail = _response_trail(db, item_id, limit=1)
        return trail[0] if trail else None

    from lib.conversations.project_status import collect_pillar_state
    project_path = row['project_path']
    pillar_state = collect_pillar_state(project_path)
    fp = _item_fingerprint(row['text'] or '', pillar_state)
    if not force and row['response_fingerprint'] == fp:
        trail = _response_trail(db, item_id, limit=1)
        if trail:
            return trail[0]

    response = generate_item_response(row['kind'], row['text'] or '', pillar_state)
    if not response:
        trail = _response_trail(db, item_id, limit=1)
        return trail[0] if trail else None

    snap = _persist_response(
        db, item_id, project_path, response, pillar_state, trigger,
        fingerprint_guard=(row['response_fingerprint'] or '',
                           int(row['updated_at'] or 0), fp))
    if snap and snap.get('conflict'):
        # Another worker edited/addressed the item while synthesis ran. Never
        # append stale evidence; return the concurrent winner if one exists.
        trail = _response_trail(db, item_id, limit=1)
        return trail[0] if trail else None
    return snap


def address_open_items(project_path: str, *, trigger: str = 'manual',
                       blocking: bool = True) -> None:
    """Re-address every OPEN item for a project (the recurring cadence entry).

    When ``blocking`` is False, runs in a daemon thread so an event trigger
    never blocks. Each item's own staleness gate elides the LLM when unchanged,
    so a quiescent project costs nothing. Never raises.
    """
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return
    if not blocking:
        threading.Thread(target=_address_open_items_blocking,
                         args=(project_path, trigger),
                         name=f'watch-{project_path[-8:]}', daemon=True).start()
        return
    _address_open_items_blocking(project_path, trigger)


def _address_open_items_blocking(project_path: str, trigger: str) -> None:
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT item_id FROM project_watch_items "
            "WHERE project_path=? AND status='open'", (project_path,)).fetchall()
    except Exception as e:
        logger.warning('[Watch] address_open list failed proj=%.40r: %s',
                       project_path, e)
        return
    for r in rows:
        try:
            address_watch_item(r['item_id'], trigger=trigger)
        except Exception as e:
            logger.debug('[Watch] address item=%s skipped: %s', r['item_id'], e)


# ══════════════════════════════════════════════════════════════════════
#  Follow-up Q&A — the human's thread ON one response (Increment 2 slice)
# ══════════════════════════════════════════════════════════════════════

_FOLLOW_UP_SYSTEM = (
    'You are the project brain. The human owner is asking a FOLLOW-UP about '
    'ONE earlier response you gave on a {kind} they are tracking. Answer the '
    'follow-up directly and concretely using ONLY the project state provided. '
    'Stay consistent with the earlier response unless the state has moved — '
    'if it moved, say what changed. If the state does not contain the answer, '
    'say so plainly — do NOT invent facts.')


def answer_follow_up(item_id: str, question: str, *,
                     response_seq: int | None = None) -> dict:
    """Answer the human's follow-up anchored to ONE trail response.

    The one interaction the watch lane was missing: a response the human wants
    to dig into becomes a THREAD. The answer is grounded in LIVE pillar state
    + the item text + the anchor response, and is persisted into the SAME
    append-only trail with ``trigger='follow_up'``; the question rides the
    evidence JSON (``followUpQuestion`` / ``anchorSeq``) so the trail shows
    what was asked. Stays strictly inside the human↔brain lane — synthesized
    responses never reach the prompt-injection path (the goals renderer ships
    item TEXT only, guarded by tests).

    Deliberately does NOT touch the item's ``response_fingerprint``: a Q&A
    turn is not a fresh assessment and must not mark the recurring cadence
    as fresh. Resolved items may still be followed up (the human asked
    explicitly). Returns ``{'ok', 'response'?}`` / ``{'ok': False, 'error'}``;
    never raises.
    """
    question = (question or '').strip()
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    if not question:
        return {'ok': False, 'error': 'empty question'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = _get_item_row(db, item_id)
        if not row:
            return {'ok': False, 'error': 'not found'}
        if response_seq:
            anchor = db.execute(
                'SELECT seq, response FROM project_watch_responses '
                'WHERE item_id=? AND seq=?', (item_id, int(response_seq))).fetchone()
        else:
            anchor = db.execute(
                'SELECT seq, response FROM project_watch_responses '
                'WHERE item_id=? ORDER BY seq DESC LIMIT 1', (item_id,)).fetchone()
    except Exception as e:
        logger.warning('[Watch] follow-up read failed item=%s: %s', item_id, e)
        return {'ok': False, 'error': str(e)}

    kind = row['kind'] or 'concern'
    anchor_text = (anchor['response'] or '') if anchor else ''
    anchor_seq = int(anchor['seq']) if anchor else 0

    from lib.conversations.project_status import (
        _build_synthesis_source, collect_pillar_state)
    project_path = row['project_path']
    pillar_state = collect_pillar_state(project_path)
    source = _build_synthesis_source(pillar_state)
    system = _FOLLOW_UP_SYSTEM.format(kind=kind) + _COMMON_SUFFIX
    user = (
        f'Project state:\n\n{source}\n\n'
        f'The {kind} I am tracking: {row["text"] or ""}\n\n'
        f'Your earlier response (the one I am following up on): '
        f'{anchor_text or "(none yet)"}\n\n'
        f'My follow-up: {question}\n\nAnswer:')
    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [{'role': 'system', 'content': system},
             {'role': 'user', 'content': user}],
            max_tokens=700, temperature=0.3, capability='cheap',
            log_prefix='[Watch]',
        )
    except Exception as e:
        logger.warning('[Watch] follow-up synthesis failed after %.1fs: %s',
                       time.time() - started, e)
        return {'ok': False, 'error': str(e)}
    text = (content or '').strip()
    if not text:
        return {'ok': False, 'error': 'empty response'}
    if len(text) > _RESPONSE_MAX_CHARS:
        text = text[:_RESPONSE_MAX_CHARS].rstrip() + '…'

    evidence = dict(pillar_state) if isinstance(pillar_state, dict) else {}
    evidence['followUpQuestion'] = question
    evidence['anchorSeq'] = anchor_seq
    snap = _persist_response(db, item_id, project_path, text, evidence,
                             'follow_up')
    if not snap:
        return {'ok': False, 'error': 'persist failed'}
    return {'ok': True, 'response': snap}


# ══════════════════════════════════════════════════════════════════════
#  Promote-to-charter — concern/question ONLY (a goal never travels here)
# ══════════════════════════════════════════════════════════════════════

def _goal_summary(text: str) -> str:
    """One-line summary for a committed decision (its first line, bounded).

    ``commit_charter`` renders ONLY this line in the per-turn injection block
    (via ``_decision_headline``), so omitting it — as this bridge used to — left
    every promoted concern/question showing as a first line clipped mid-sentence
    by the generic fallback. The charter owns the ceiling; we import it rather
    than re-hardcoding 240.
    """
    from lib.conversations.project_charter import _SUMMARY_MAX_CHARS
    first = (text or '').strip().split('\n', 1)[0].strip()
    if len(first) > _SUMMARY_MAX_CHARS:
        first = first[:_SUMMARY_MAX_CHARS].rstrip() + '…'
    return first


def promote_watch_item(item_id: str, *, updated_by_conv: str = '',
                       expected_version: int | None = None) -> dict:
    """Bridge a CONCERN or QUESTION into the charter as a committed decision —
    the only path by which one of those reaches sibling agents. Routes strictly
    through ``commit_charter(add_decision=…)`` (no new write path, no fan-out)
    as a HUMAN action on a HUMAN-authored item. Appends commute, so
    ``expected_version`` is advisory here.

    **A ``goal`` is REFUSED** (``error='goal_not_promotable'``). Goals reach
    agents by existing — :func:`render_goals_injection_block` injects every open
    one — so there is nothing to promote, and copying a goal into the charter is
    exactly the duplication this design removed (decision 1). Refusing loudly
    rather than silently succeeding matters: a caller that still asks for this
    is running against the old contract and must be told, not quietly no-op'd
    into thinking it set the north star. Editing the charter's own north-star
    ``content`` is a HUMAN action in the Charter panel.

    Returns ``{'ok', 'version'?, 'error'?}``.
    """
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = _get_item_row(db, item_id)
    except Exception as e:
        logger.error('[Watch] promote read failed item=%s: %s', item_id, e,
                     exc_info=True)
        return {'ok': False, 'error': str(e)}
    if not row:
        return {'ok': False, 'error': 'not found'}
    project_path = row['project_path']
    kind = row['kind'] or 'concern'
    item_text = row['text'] or ''

    if kind == 'goal':
        logger.info('[Watch] promote refused for goal item=%s — goals inject '
                    'directly and never touch the charter', item_id)
        return {'ok': False, 'error': 'goal_not_promotable'}

    from lib.conversations.project_charter import commit_charter
    label = {'concern': 'Concern', 'question': 'Question'}.get(
        kind, 'Watch item')
    promoted_text = f'[{label} — promoted by owner] {item_text}'
    res = commit_charter(project_path, add_decision=promoted_text,
                         decision_kind='invariant',
                         summary=_goal_summary(item_text),
                         updated_by_conv=updated_by_conv or '',
                         expected_version=expected_version)
    if not res.get('ok'):
        return res
    try:
        db_execute_with_retry(
            db,
            'UPDATE project_watch_items SET promoted=1, updated_at=? '
            'WHERE item_id=?', (_now_ms(), item_id))
    except Exception as e:
        logger.debug('[Watch] promoted-flag update skipped item=%s: %s', item_id, e)
    audit_log('watch_item_promoted', project_path=project_path, item_id=item_id,
              kind=kind, charter_version=res.get('version'))
    return {'ok': True, 'version': res.get('version')}


__all__ = [
    'add_watch_item', 'edit_watch_item', 'set_watch_status', 'delete_watch_item',
    'list_watch_items', 'generate_item_response', 'address_watch_item',
    'address_open_items', 'promote_watch_item', 'promotion_state',
    'answer_follow_up', 'render_goals_injection_block',
    'VALID_KINDS', 'VALID_STATUSES', 'PROMOTION_NONE', 'PROMOTION_ACTIVE',
    '_RESPONSES_KEEP',
]
