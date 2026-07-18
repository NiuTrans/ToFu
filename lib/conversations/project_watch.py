"""lib.conversations.project_watch — the human's standing "watch list" (Pillar #7).

The status lane (``project_status.py``) lets the human ASK the project a one-shot
question and get an ephemeral answer. This module is its DURABLE counterpart: a
standing list of things the HUMAN cares about — concerns, open questions,
goals — that the brain ADDRESSES on a recurring basis, keeping an append-only
trail of its responses per item so the human can see how the answer to a
concern DRIFTS over time.

Owner-locked decisions (2026-07-08):
  1. **Human-facing-only, with ONE explicit bridge.** A watch item is authored
     by the HUMAN and is NEVER injected into sibling agent prompts (same
     source-grep guard as the status memory). The ONLY way an item reaches
     agents is ``promote_watch_item`` → a charter commit (``commit_charter``),
     because the charter is already the ambient-to-agents surface. This is a
     HUMAN action on a HUMAN-authored item (the human decides to promote their
     own watch item), distinct from an agent self-committing a decision. No
     auto-steering, no new inter-conv write, no fan-out.
  2. **Append-only response trail per item** (bounded), not latest-only — the
     drift is the signal.
  3. **Cadence = on-tab-open + event-driven now** (reuse the staleness gate so a
     quiescent project costs nothing). The closed-panel scheduler cadence is a
     deliberately-deferred follow-up.

All functions key STRICTLY on ``project_path`` / ``item_id`` — never a
process-global. Best-effort throughout; the address generator never raises.
"""

from __future__ import annotations

import json
import threading
import time

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

# Keep at most this many responses per item (bounded trail; pruned on insert).
_RESPONSES_KEEP = 100
# Soft cap on a human-authored item's text (keeps a row cheap; a watch item is
# a prompt, not a document).
_ITEM_TEXT_MAX = 2000
# Bounded response length.
_RESPONSE_MAX_CHARS = 2000

VALID_KINDS = ('concern', 'question', 'goal')
VALID_STATUSES = ('open', 'resolved')

# Serializes the read-max-then-insert of the per-item monotonic seq.
_response_lock = threading.Lock()

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
        db.execute(
            'INSERT INTO project_watch_items '
            '(item_id, project_path, kind, text, status, promoted, '
            ' response_fingerprint, created_by_conv, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (item_id, project_path, kind, text, 'open', 0, '',
             created_by_conv or '', ts, ts))
        db.commit()
    except Exception as e:
        logger.error('[Watch] add failed proj=%.40r: %s', project_path, e,
                     exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_added', project_path=project_path, item_id=item_id,
              kind=kind)
    return {'ok': True, 'item': {
        'item_id': item_id, 'kind': kind, 'text': text, 'status': 'open',
        'promoted': False, 'created_at': ts, 'updated_at': ts, 'responses': []}}


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
        db.execute(
            'UPDATE project_watch_items SET text=?, kind=?, updated_at=?'
            + (', response_fingerprint=?' if text_changed else '')
            + ' WHERE item_id=?',
            ((new_text, new_kind, _now_ms(), '', item_id) if text_changed
             else (new_text, new_kind, _now_ms(), item_id)))
        db.commit()
    except Exception as e:
        logger.error('[Watch] edit failed item=%s: %s', item_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_edited', item_id=item_id)
    return {'ok': True}


def set_watch_status(item_id: str, status: str) -> dict:
    """Mark an item open|resolved. Returns ``{'ok', 'error'?}``."""
    status = (status or '').strip().lower()
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    if status not in VALID_STATUSES:
        return {'ok': False, 'error': 'invalid status'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = _get_item_row(db, item_id)
        if not row:
            return {'ok': False, 'error': 'not found'}
        db.execute('UPDATE project_watch_items SET status=?, updated_at=? '
                   'WHERE item_id=?', (status, _now_ms(), item_id))
        db.commit()
    except Exception as e:
        logger.error('[Watch] status failed item=%s: %s', item_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('watch_item_status', item_id=item_id, status=status)
    return {'ok': True}


def delete_watch_item(item_id: str) -> dict:
    """Delete a watch item AND its response trail. Returns ``{'ok', 'error'?}``."""
    if not item_id:
        return {'ok': False, 'error': 'no item'}
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_watch_responses WHERE item_id=?', (item_id,))
        db.execute('DELETE FROM project_watch_items WHERE item_id=?', (item_id,))
        db.commit()
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


def _row_to_item(db, row, *, with_responses: bool = True,
                 resp_limit: int = 20) -> dict:
    item = {
        'item_id': row['item_id'], 'kind': row['kind'], 'text': row['text'] or '',
        'status': row['status'] or 'open', 'promoted': bool(row['promoted']),
        'created_at': int(row['created_at'] or 0),
        'updated_at': int(row['updated_at'] or 0),
    }
    if with_responses:
        item['responses'] = _response_trail(db, row['item_id'], limit=resp_limit)
    return item


def list_watch_items(project_path: str, *, include_resolved: bool = True,
                     resp_limit: int = 20) -> dict:
    """List watch items for a project (newest-updated-first) with their response
    trails. Returns ``{'items': [...]}``; empty on no project / error."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return {'items': []}
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
        return {'items': []}
    return {'items': [_row_to_item(db, r, resp_limit=resp_limit) for r in rows]}


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
                      pillar_state: dict, trigger: str) -> dict | None:
    try:
        pillar_json = json.dumps(pillar_state, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[Watch] pillar_state dump failed, defaulting: %s', e)
        pillar_json = '{}'
    ts = _now_ms()
    try:
        with _response_lock:
            row = db.execute(
                'SELECT COALESCE(MAX(seq), 0) AS m FROM project_watch_responses '
                'WHERE item_id=?', (item_id,)).fetchone()
            seq = (row['m'] if row and row['m'] is not None else 0) + 1
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
            db.commit()
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

    snap = _persist_response(db, item_id, project_path, response, pillar_state, trigger)
    if snap:
        try:
            db.execute('UPDATE project_watch_items SET response_fingerprint=?, '
                       'updated_at=? WHERE item_id=?', (fp, _now_ms(), item_id))
            db.commit()
        except Exception as e:
            logger.debug('[Watch] fingerprint update skipped item=%s: %s', item_id, e)
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
#  Promote-to-charter — the ONE explicit bridge to agent awareness
# ══════════════════════════════════════════════════════════════════════

def promote_watch_item(item_id: str, *, updated_by_conv: str = '',
                       expected_version: int | None = None) -> dict:
    """Bridge a watch item into the charter as a committed decision — the ONLY
    path by which a watch item reaches sibling agents. Routes strictly through
    ``commit_charter`` (no new write path, no fan-out) as a HUMAN action on a
    HUMAN-authored item. Marks the item ``promoted``. Returns ``{'ok',
    'version'?, 'error'?}``.
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
    label = {'goal': 'Goal', 'concern': 'Concern', 'question': 'Question'}.get(
        row['kind'], 'Watch item')
    decision = f'[{label} — promoted by owner] {row["text"]}'
    # The bridge: a charter commit invoked by a HUMAN promoting their own
    # watch item. NOTHING here writes into the agent prompt path directly — the
    # charter is the ambient-to-agents surface.
    from lib.conversations.project_charter import commit_charter
    res = commit_charter(project_path, add_decision=decision,
                         updated_by_conv=updated_by_conv or '',
                         expected_version=expected_version)
    if not res.get('ok'):
        return res
    try:
        db.execute('UPDATE project_watch_items SET promoted=1, updated_at=? '
                   'WHERE item_id=?', (_now_ms(), item_id))
        db.commit()
    except Exception as e:
        logger.debug('[Watch] promoted-flag update skipped item=%s: %s', item_id, e)
    audit_log('watch_item_promoted', project_path=project_path, item_id=item_id,
              charter_version=res.get('version'))
    return {'ok': True, 'version': res.get('version')}


__all__ = [
    'add_watch_item', 'edit_watch_item', 'set_watch_status', 'delete_watch_item',
    'list_watch_items', 'generate_item_response', 'address_watch_item',
    'address_open_items', 'promote_watch_item', 'VALID_KINDS', 'VALID_STATUSES',
    '_RESPONSES_KEEP',
]
