"""lib.conversations.project_board — the coordination BOARD (Pillar #3).

This is the piece that turns PERCEPTION (the Activity Feed) and shared INTENT
(the Charter) into actual AUTO-COORDINATION: a per-project board of coarse,
human-meaningful epics that conversations POST, CLAIM, and COMPLETE — so two
conversations of the same project stop colliding / duplicating work.

Locked design (owner, 2026-06-30):

  • **Soft, TTL-expiring lease — advisory, never a hard lock.** ``claim_task``
    sets ``owner_conv_id`` + ``lease_expires_at = now + TTL``. The lease is
    NOT enforced by a write-lock; it's a HINT injected into every sibling's
    prompt ("X is being worked by conversation …, avoid duplicating"). A
    crashed/abandoned conversation can NEVER deadlock the board because the
    lease expiry is evaluated AT READ TIME — an expired claim reads as
    ``open`` with no background reaper, no global cleaner thread.
  • **Per ``project_path``, never a process-global.** Every call addresses its
    project explicitly (the read/write-badge thrash guard).
  • **Coarse granularity.** Epics only — fine agent sub-steps belong to the
    Activity Feed, not the board.
  • **Feed-coupled.** post→(no feed; quiet), claim→``claimed``,
    complete→``completed``, block→``blocked`` (the last dead kind finally
    gets a producer here).

``status`` is the STORED column (open/claimed/done); ``effective_status`` is
what a reader sees after the at-read-time lease check (a stored ``claimed``
whose lease has expired is reported ``open``).
"""

from __future__ import annotations

import json
import time
import uuid

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# Default soft-lease TTL (ms). A claim is advisory for this long; after it the
# epic reads as open again so no abandoned conversation can hold it forever.
DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000  # 30 minutes

_TITLE_MAX_CHARS = 2000  # epics carry multi-sentence design descriptions; a
                         # tight cap silently clipped titles mid-word (both in
                         # the board panel and the injected prompt block)
_MAX_BOARD_TASKS = 200  # coarse epics only — a guard against runaway posting


def _now_ms() -> int:
    return int(time.time() * 1000)


def _effective_status(stored_status: str, lease_expires_at: int,
                      now_ms: int) -> str:
    """The status a READER sees. A stored 'claimed' whose lease has expired is
    reported 'open' — this single function is the anti-deadlock core: it is the
    ONLY place an expired soft-lease is reclaimed (at read time, no reaper)."""
    if stored_status == 'claimed' and lease_expires_at and lease_expires_at <= now_ms:
        return 'open'
    return stored_status


def _row_to_task(r, now_ms: int) -> dict:
    try:
        depends_on = json.loads(r['depends_on']) if r['depends_on'] else []
        if not isinstance(depends_on, list):
            depends_on = []
    except (TypeError, ValueError):
        depends_on = []
    stored = r['status'] or 'open'
    lease = int(r['lease_expires_at'] or 0)
    eff = _effective_status(stored, lease, now_ms)
    try:
        dispatched = bool(r['dispatched'])
    except (KeyError, IndexError, TypeError):
        dispatched = False
    return {
        'id': r['id'], 'title': r['title'] or '', 'status': eff,
        'stored_status': stored,
        'owner_conv_id': r['owner_conv_id'] if eff == 'claimed' else '',
        'lease_expires_at': lease if eff == 'claimed' else 0,
        # dispatched badge only meaningful while the (live) claim stands.
        'dispatched': dispatched and eff == 'claimed',
        'created_by_conv': r['created_by_conv'] or '',
        'depends_on': depends_on,
        'created_at': int(r['created_at'] or 0),
        'updated_at': int(r['updated_at'] or 0),
    }


def claims_by_conv(board_tasks: list) -> dict:
    """Map ``owner_conv_id`` → claimed-epic title, for epics whose EFFECTIVE
    status is ``claimed`` (i.e. a live, unexpired lease).

    This is the SINGLE source of the "which conversation is advancing which
    epic" join. ``read_board`` already reclaimed expired leases to ``open``, so
    every entry here is a live claim — never a deadlocked one. Both
    ``build_brain_summary`` (collab bar) and ``build_peer_status`` (the peer
    introspection tool) consume it so the two views can never drift. Pure +
    side-effect-free; safe on any task list.
    """
    out = {}
    for t in (board_tasks or []):
        if not isinstance(t, dict):
            continue
        if t.get('status') == 'claimed' and t.get('owner_conv_id'):
            out[t['owner_conv_id']] = t.get('title', '')
    return out


def read_board(project_path: str) -> dict:
    """Return the board for ``project_path`` with leases evaluated at read time.

    ``{'tasks': [...], 'open': N, 'claimed': N, 'done': N}`` where each task's
    ``status`` is its EFFECTIVE status (an expired claim → open). Never raises.
    """
    out = {'tasks': [], 'open': 0, 'claimed': 0, 'done': 0}
    if not project_path:
        return out
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT id, title, status, owner_conv_id, lease_expires_at, '
            '       created_by_conv, depends_on, dispatched, created_at, updated_at '
            'FROM project_tasks WHERE project_path=? '
            'ORDER BY created_at ASC', (project_path,)).fetchall()
    except Exception as e:
        logger.warning('[Board] read failed proj=%.40r: %s', project_path, e)
        return out
    now = _now_ms()
    for r in rows:
        t = _row_to_task(r, now)
        out['tasks'].append(t)
        out[t['status']] = out.get(t['status'], 0) + 1
    return out


def post_task(project_path: str, conv_id: str, title: str, *,
              depends_on: list | None = None) -> dict:
    """Post a new OPEN epic to the board. Returns ``{'ok', 'id'?, 'error'?}``."""
    title = (title or '').strip()[:_TITLE_MAX_CHARS]
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not title:
        return {'ok': False, 'error': 'empty title'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        n = db.execute('SELECT COUNT(*) AS c FROM project_tasks WHERE project_path=?',
                       (project_path,)).fetchone()
        if n and int(n['c']) >= _MAX_BOARD_TASKS:
            return {'ok': False, 'error': 'board full (coarse epics only)'}
        task_id = 'pt_' + uuid.uuid4().hex[:16]
        ts = _now_ms()
        deps = json.dumps([str(d) for d in (depends_on or [])], ensure_ascii=False)
        db.execute(
            'INSERT INTO project_tasks '
            '(id, project_path, title, status, owner_conv_id, lease_expires_at, '
            ' created_by_conv, depends_on, created_at, updated_at) '
            "VALUES (?, ?, ?, 'open', '', 0, ?, ?, ?, ?)",
            (task_id, project_path, title, conv_id or '', deps, ts, ts))
        db.commit()
    except Exception as e:
        logger.error('[Board] post failed proj=%.40r: %s', project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('board_post', project_path=project_path, task_id=task_id, conv_id=conv_id)
    return {'ok': True, 'id': task_id}


def claim_task(project_path: str, conv_id: str, task_id: str, *,
               ttl_ms: int = DEFAULT_LEASE_TTL_MS,
               dispatched: bool = False) -> dict:
    """Claim an epic with a SOFT TTL lease (advisory). Succeeds if the epic is
    open OR its existing claim has EXPIRED (at-read-time reclaim) OR it's
    already claimed by THIS conversation (lease refresh). Fails only if a
    DIFFERENT conversation holds an UNEXPIRED lease — and even then it's
    advisory: the caller can still proceed, but the board tells it not to.

    Returns ``{'ok', 'lease_expires_at'?, 'error'?, 'owner'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT status, owner_conv_id, lease_expires_at FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        now = _now_ms()
        eff = _effective_status(row['status'] or 'open',
                                int(row['lease_expires_at'] or 0), now)
        owner = row['owner_conv_id'] or ''
        if eff == 'claimed' and owner and owner != (conv_id or ''):
            # Held by someone else, lease still valid → advisory refusal.
            return {'ok': False, 'error': 'already_claimed', 'owner': owner}
        if (row['status'] or '') == 'done':
            return {'ok': False, 'error': 'already_done'}
        lease = now + max(60_000, int(ttl_ms or DEFAULT_LEASE_TTL_MS))
        db.execute(
            "UPDATE project_tasks SET status='claimed', owner_conv_id=?, "
            'lease_expires_at=?, dispatched=?, updated_at=? '
            'WHERE id=? AND project_path=?',
            (conv_id or '', lease, 1 if dispatched else 0, now,
             task_id, project_path))
        db.commit()
        title = _task_title(db, project_path, task_id)
    except Exception as e:
        logger.error('[Board] claim failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('claimed', project_path, conv_id, f'Claimed: {title}',
          payload={'taskId': task_id})
    audit_log('board_claim', project_path=project_path, task_id=task_id, conv_id=conv_id)
    return {'ok': True, 'lease_expires_at': lease}


def complete_task(project_path: str, conv_id: str, task_id: str) -> dict:
    """Mark an epic done. Returns ``{'ok', 'error'?}``."""
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        title = _task_title(db, project_path, task_id)
        if title is None:
            return {'ok': False, 'error': 'task not found'}
        db.execute(
            "UPDATE project_tasks SET status='done', lease_expires_at=0, "
            'dispatched=0, updated_at=? WHERE id=? AND project_path=?',
            (_now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] complete failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('completed', project_path, conv_id, f'Completed: {title}',
          payload={'taskId': task_id})
    audit_log('board_complete', project_path=project_path, task_id=task_id, conv_id=conv_id)
    # ── Brain-driven dispatch trigger (Pillar #5): completing this epic may
    #    unblock dependents → autonomously kick them off. Best-effort, never
    #    raises into the completion path; no new thread (reuses the queue). ──
    try:
        from lib.conversations.project_dispatch import on_epic_completed
        on_epic_completed(project_path, completed_conv_id=conv_id)
    except Exception as e:
        logger.debug('[Board] post-complete dispatch trigger skipped: %s', e)
    return {'ok': True}


def block_task(project_path: str, conv_id: str, task_id: str, reason: str) -> dict:
    """Report an epic BLOCKED — emits the ``blocked`` feed kind (the last dead
    kind to gain a producer). Does not change board status (a block is a
    signal, not a state); the reason is surfaced in the feed. ``{'ok','error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    reason = (reason or '').strip()
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        title = _task_title(db, project_path, task_id)
        if title is None:
            return {'ok': False, 'error': 'task not found'}
    except Exception as e:
        logger.warning('[Board] block lookup failed proj=%.40r: %s', project_path, e)
        return {'ok': False, 'error': str(e)}
    _emit('blocked', project_path, conv_id,
          f'Blocked: {title}' + (f' — {reason}' if reason else ''),
          payload={'taskId': task_id, 'reason': reason})
    audit_log('board_block', project_path=project_path, task_id=task_id, conv_id=conv_id)
    return {'ok': True}


def defer_task(project_path: str, conv_id: str, task_id: str,
               reason: str = '') -> dict:
    """PARK an epic — set the terminal-ish ``deferred`` status.

    Unlike ``block_task`` (a feed SIGNAL that leaves board status untouched),
    this is a real STATUS write: ``deferred`` epics are EXCLUDED from
    ``select_dispatchable`` (they satisfy ``status != 'open'``) so the
    heartbeat sweep stops re-dispatching them, and — crucially —
    ``_effective_status`` never reclaims a ``deferred`` epic (its reclaim is
    specific to ``claimed``), so a parked epic does NOT oscillate
    ``open→claimed→lease-expires→open`` the way a human-gated epic otherwise
    would. The epic stays VISIBLE on the board (distinct from ``done``) so the
    "parked pending a human decision" state is legible.

    Sets ``status='deferred'`` and CLEARS ``owner_conv_id`` + ``lease_expires_at``
    + the dispatched flag (a parked epic holds no lease). Permitted from
    ``open`` and ``claimed``; refused for ``done`` (can't park finished work)
    and ``deferred`` (idempotent no-op → advisory error). The un-park path is
    ``reopen_task`` (``deferred → open``), the same human lever that revives a
    done/claimed epic. Emits a ``note`` feed event so the transition is
    observable. ``{'ok', 'from'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    reason = (reason or '').strip()
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title, status FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        prev_status = row['status'] or 'open'
        title = row['title'] or ''
        if prev_status == 'done':
            return {'ok': False, 'error': 'already_done'}
        if prev_status == 'deferred':
            return {'ok': False, 'error': 'already_deferred'}
        db.execute(
            "UPDATE project_tasks SET status='deferred', owner_conv_id='', "
            'lease_expires_at=0, dispatched=0, updated_at=? '
            'WHERE id=? AND project_path=?',
            (_now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] defer failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    summary = f'Parked (deferred): {title}' + (f' — {reason}' if reason else '')
    _emit('note', project_path, conv_id, summary,
          payload={'taskId': task_id, 'deferred': True, 'from': prev_status,
                   'reason': reason})
    audit_log('board_defer', project_path=project_path, task_id=task_id,
              conv_id=conv_id, from_status=prev_status)
    return {'ok': True, 'from': prev_status}



def reopen_task(project_path: str, conv_id: str, task_id: str) -> dict:
    """Reopen an epic (done|claimed → open) — a HUMAN override.

    A direct status write, NOT a lease mutation: it sets ``status='open'`` and
    CLEARS ``owner_conv_id`` + ``lease_expires_at`` (+ the dispatched flag), so
    the epic becomes claimable again. Permitted from both ``done`` (revive
    finished work) and ``claimed`` (break a wrongly-held or stuck live claim) —
    the ONE human lever to free an epic without a background reaper.

    A ``note`` feed event is emitted so the transition is OBSERVABLE (never a
    silent yank). Note the coordination consequence when reopening a live
    ``claimed`` epic: the previous owner's injected ``[PROJECT BOARD]`` block
    flips that epic from "(you)" to a plain open epic on its NEXT prompt
    assembly (the block is re-read per turn, so the owner is not interrupted
    mid-turn — it simply sees the epic as reclaimable next time, and the feed
    note records who held it). ``{'ok', 'from'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title, status, owner_conv_id FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        prev_status = row['status'] or 'open'
        prev_owner = row['owner_conv_id'] or ''
        title = row['title'] or ''
        if prev_status == 'open':
            return {'ok': False, 'error': 'already_open'}
        db.execute(
            "UPDATE project_tasks SET status='open', owner_conv_id='', "
            'lease_expires_at=0, dispatched=0, updated_at=? '
            'WHERE id=? AND project_path=?',
            (_now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] reopen failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    summary = f'Reopened: {title}'
    if prev_status == 'claimed' and prev_owner:
        summary += f' (was claimed by {prev_owner})'
    _emit('note', project_path, conv_id, summary,
          payload={'taskId': task_id, 'reopened': True, 'from': prev_status,
                   'prevOwner': prev_owner})
    audit_log('board_reopen', project_path=project_path, task_id=task_id,
              conv_id=conv_id, from_status=prev_status, prev_owner=prev_owner)
    return {'ok': True, 'from': prev_status}


def _task_title(db, project_path: str, task_id: str):
    row = db.execute('SELECT title FROM project_tasks WHERE id=? AND project_path=?',
                     (task_id, project_path)).fetchone()
    return None if not row else (row['title'] or '')


def _emit(kind: str, project_path: str, conv_id: str, summary: str,
          *, payload: dict | None = None) -> None:
    """Best-effort feed emission — never raises into the board caller."""
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(project_path, conv_id or '', kind, summary, payload=payload)
    except Exception as e:
        logger.debug('[Board] feed emit (%s) skipped: %s', kind, e)


def render_board_block(project_path: str, current_conv_id: str = '') -> str:
    """Render the board for system-context injection — the AUTO-COORDINATION
    surface. Lists open epics + a per-claimed-epic explicit "avoid duplication"
    hint when ANOTHER conversation holds an UNEXPIRED lease (this is what makes
    a reading conversation step aside instead of redoing the work). Returns ''
    when the board is empty (no prompt weight for an unused board).
    """
    board = read_board(project_path)
    tasks = board['tasks']
    if not tasks:
        return ''
    open_t = [t for t in tasks if t['status'] == 'open']
    claimed_t = [t for t in tasks if t['status'] == 'claimed']
    done_t = [t for t in tasks if t['status'] == 'done']
    deferred_t = [t for t in tasks if t['status'] == 'deferred']
    if not (open_t or claimed_t or done_t or deferred_t):
        return ''
    lines = ['[PROJECT BOARD] — shared coordination board for this project. '
             'Before starting work, CHECK it: claim an open epic so siblings '
             'know you own it, and do NOT duplicate an epic another '
             'conversation is already advancing.']
    if claimed_t:
        lines.append('')
        lines.append('In progress (claimed by a conversation — AVOID DUPLICATING):')
        for t in claimed_t:
            owner = t['owner_conv_id'] or 'another conversation'
            mine = ' (you)' if current_conv_id and owner == current_conv_id else ''
            hint = '' if mine else ' — another conversation is advancing this; ' \
                   'pick a different epic or coordinate, do not redo it'
            lines.append(f'  • [{t["id"]}] {t["title"]} — claimed by {owner}{mine}{hint}')
    if open_t:
        lines.append('')
        lines.append('Open (unclaimed — claim one with project_board_claim before working it):')
        for t in open_t:
            dep = f' (depends on {", ".join(t["depends_on"])})' if t['depends_on'] else ''
            lines.append(f'  • [{t["id"]}] {t["title"]}{dep}')
    if deferred_t:
        lines.append('')
        lines.append('Parked (deferred — NOT auto-dispatched; awaiting a human '
                     'decision. A human reopens one when it is ready to resume):')
        for t in deferred_t:
            lines.append(f'  • [{t["id"]}] {t["title"]}')
    if done_t:
        lines.append('')
        lines.append('Recently done:')
        for t in done_t[-8:]:
            lines.append(f'  • {t["title"]}')
    return '\n'.join(lines)


def execute_board_tool(fn_name: str, fn_args: dict, *,
                       current_conv_id: str = '', project_path: str = '') -> str:
    """Execute a board agent tool → human-readable string."""
    try:
        if not project_path:
            return ('Error: the project board is only available in project mode '
                    '(open a project first).')
        if fn_name == 'project_board_read':
            block = render_board_block(project_path, current_conv_id)
            return block or ('The project board is empty. If you discover a '
                             'project-level epic, post it with project_board_post '
                             'so sibling conversations can coordinate.')
        if fn_name == 'project_board_post':
            res = post_task(project_path, current_conv_id,
                            fn_args.get('title') or '',
                            depends_on=fn_args.get('depends_on'))
            return (f'Posted epic {res["id"]} to the board.' if res.get('ok')
                    else f'Error posting epic: {res.get("error", "unknown")}.')
        if fn_name == 'project_board_claim':
            res = claim_task(project_path, current_conv_id,
                             fn_args.get('task_id') or '')
            if res.get('ok'):
                return ('Claimed. Siblings now see you own this epic; complete it '
                        'with project_board_complete when done.')
            if res.get('error') == 'already_claimed':
                return (f'NOT claimed — epic is already being advanced by '
                        f'conversation {res.get("owner", "?")}. Avoid duplicating '
                        f'it; pick a different open epic or coordinate.')
            return f'Error claiming epic: {res.get("error", "unknown")}.'
        if fn_name == 'project_board_complete':
            res = complete_task(project_path, current_conv_id,
                                fn_args.get('task_id') or '')
            return ('Marked done.' if res.get('ok')
                    else f'Error completing epic: {res.get("error", "unknown")}.')
        if fn_name == 'project_board_block':
            res = block_task(project_path, current_conv_id,
                             fn_args.get('task_id') or '',
                             fn_args.get('reason') or '')
            return ('Reported blocked (visible in the project activity feed).'
                    if res.get('ok')
                    else f'Error reporting block: {res.get("error", "unknown")}.')
        if fn_name == 'project_board_defer':
            res = defer_task(project_path, current_conv_id,
                             fn_args.get('task_id') or '',
                             fn_args.get('reason') or '')
            if res.get('ok'):
                return ('Parked (deferred). This epic is no longer auto-dispatched '
                        'by the heartbeat sweep and will NOT oscillate '
                        'open/claimed; it stays visible on the board until a human '
                        'reopens it when the blocking decision lands.')
            if res.get('error') == 'already_deferred':
                return 'Already parked (deferred) — no change.'
            if res.get('error') == 'already_done':
                return 'Cannot park a completed epic.'
            return f'Error parking epic: {res.get("error", "unknown")}.'
        return f"Error: Unknown board tool '{fn_name}'"
    except Exception as e:
        logger.warning('[Board] tool %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'read_board', 'post_task', 'claim_task', 'complete_task', 'block_task',
    'defer_task', 'reopen_task', 'render_board_block', 'execute_board_tool',
    '_effective_status',
    'claims_by_conv', 'DEFAULT_LEASE_TTL_MS',
]
