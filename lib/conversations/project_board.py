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

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

# Default soft-lease TTL (ms). A claim is advisory for this long; after it the
# epic reads as open again so no abandoned conversation can hold it forever.
DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000  # 30 minutes

# ── Block cooldown (self-expiring escalating backoff) ──
# When an epic hits a genuine external gate (a sibling must commit first; a
# human §10 infra sign-off), block_task stamps blocked_until = now + an
# ESCALATING cooldown so select_dispatchable stops re-dispatching it (which
# burned a billed agent turn every ~30 min to re-discover the same unmet dep).
# The cooldown is exponential in the block count and CAPPED, so a perpetually
# human-gated epic converges to a long sleep after a FEW retries (owner: "few
# retries then long sleep") instead of churning at fixed cadence forever. It is
# NOT the removed park shelf: it self-expires at READ time (no reaper) and needs
# NO human action to release, so it can never deadlock the board.
BLOCK_COOLDOWN_BASE_MS = 60 * 60 * 1000       # 1 h after the first block
BLOCK_COOLDOWN_MAX_MS = 24 * 60 * 60 * 1000   # capped at 1 day
_BLOCK_COOLDOWN_FACTOR = 4                     # x4 per block -> cap by block #4

# A [sibling] block auto-resolves the instant a sibling commits — it is
# TRANSIENT by definition, so it must NOT ride the escalating human curve
# (ordinary collaboration churn would otherwise ratchet a perfectly-landable
# epic toward the 24 h cap). Instead a sibling block tracks the LEASE clock: a
# flat window equal to one lease TTL, no escalation: the heartbeat stops
# churning the epic every sweep but retries after the window lapses. On the
# single shared checkout a sibling's commit is visible immediately, so a plain
# cooldown retry converges (the precise wait-on-path hold was removed 2026-07-13).
SIBLING_BLOCK_COOLDOWN_MS = DEFAULT_LEASE_TTL_MS  # 30 min, flat


def _block_cooldown_ms(block_count: int, block_class: str = 'human') -> int:
    """Return the cooldown window (ms) for a row blocked ``block_count`` times.

    CLASS-AWARE (owner directive 2026-07-11): a transient ``'sibling'`` block
    must never escalate — it returns the FLAT ``SIBLING_BLOCK_COOLDOWN_MS`` (one
    lease clock) regardless of count, because a collaboration event auto-
    resolves on the sibling's commit and is woken event-driven on release. The
    escalating exponential curve is reserved for ``'human'`` (and any untagged
    reason, conservatively treated as human — a genuine unknown gate should
    escalate, not be assumed transient).

    Human curve: 0 blocks -> 0. Otherwise ``BASE * FACTOR**(count-1)`` clamped
    to ``BLOCK_COOLDOWN_MAX_MS`` — 1st block sleeps BASE (1 h), and with
    FACTOR=4 the cap (1 day) is reached by the 4th block: 1 h -> 4 h -> 16 h ->
    24 h(cap). Pure + side-effect-free."""
    n = int(block_count or 0)
    if n <= 0:
        return 0
    if block_class == 'sibling':
        return SIBLING_BLOCK_COOLDOWN_MS
    # Clamp the exponent so FACTOR**(n-1) can't build a huge int before min()
    # (n is small in practice, but stay safe against a runaway block_count).
    exp = min(n - 1, 20)
    return min(BLOCK_COOLDOWN_MAX_MS, BLOCK_COOLDOWN_BASE_MS * (_BLOCK_COOLDOWN_FACTOR ** exp))


# The block CLASS tag that means "auto-resolves when a sibling commits" — it
# rides the FLAT (non-escalating) cooldown instead of the human curve.
_SIBLING_TAG = '[sibling]'


_TITLE_MAX_CHARS = 2000  # epics carry multi-sentence design descriptions; a
                         # tight cap silently clipped titles mid-word (both in
                         # the board panel and the injected prompt block)
_MAX_BOARD_TASKS = 200  # coarse epics only — a guard against runaway posting


_now_ms = now_ms


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
    except (TypeError, ValueError) as e:
        logger.debug('[Board] depends_on parse failed, defaulting: %s', e)
        depends_on = []
    stored = r['status'] or 'open'
    lease = int(r['lease_expires_at'] or 0)
    eff = _effective_status(stored, lease, now_ms)
    try:
        dispatched = bool(r['dispatched'])
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] dispatched field parse failed, defaulting: %s', e)
        dispatched = False
    # kind is nullable-safe: a pre-migration row (no column / NULL) reads as
    # 'epic' so it is NEVER silently dropped off the dispatch board.
    try:
        kind = r['kind'] or 'epic'
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] kind field parse failed, defaulting: %s', e)
        kind = 'epic'
    # Block-cooldown fields are nullable-safe: a pre-migration row (no column)
    # reads as never-blocked (0/'') so it is NEVER wrongly cooldown-suppressed.
    try:
        blocked_until = int(r['blocked_until'] or 0)
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] blocked_until field parse failed, defaulting: %s', e)
        blocked_until = 0
    try:
        block_count = int(r['block_count'] or 0)
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] block_count field parse failed, defaulting: %s', e)
        block_count = 0
    try:
        block_reason = r['block_reason'] or ''
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] block_reason field parse failed, defaulting: %s', e)
        block_reason = ''
    # dispatch_target is nullable-safe: a pre-migration row (no column) reads as
    # '' -> dispatch routes to created_by_conv (unchanged).
    try:
        dispatch_target = r['dispatch_target'] or ''
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] dispatch_target field parse failed, defaulting: %s', e)
        dispatch_target = ''
    # write_set is nullable-safe: a pre-migration row (no column) reads as an
    # empty list -> unknown footprint -> treated as non-conflicting (never
    # stranded). Malformed JSON also -> []. See select_dispatchable's
    # disjoint-preference partitioning (worktree isolation §4).
    try:
        write_set = json.loads(r['write_set'] or '[]')
        if not isinstance(write_set, list):
            write_set = []
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.debug('[Board] write_set parse failed, defaulting: %s', e)
        write_set = []
    return {
        'id': r['id'], 'title': r['title'] or '', 'status': eff,
        'kind': kind,
        'stored_status': stored,
        'owner_conv_id': r['owner_conv_id'] if eff == 'claimed' else '',
        'lease_expires_at': lease if eff == 'claimed' else 0,
        # dispatched badge only meaningful while the (live) claim stands.
        'dispatched': dispatched and eff == 'claimed',
        'created_by_conv': r['created_by_conv'] or '',
        'depends_on': depends_on,
        # Block cooldown: blocked_until is the at-read-time-expiring retry gate;
        # a row is "on cooldown" iff blocked_until > now (evaluated by the
        # reader — select_dispatchable / render). block_count drives escalation.
        'blocked_until': blocked_until,
        'block_count': block_count,
        'block_reason': block_reason,
        # dispatch_target: mutable routing override (idle-sibling migration).
        # created_by_conv is immutable authorship; this is who runs it NEXT.
        'dispatch_target': dispatch_target,
        # write_set: JSON list of paths/globs/subsystem-tags this epic intends
        # to write; select_dispatchable prefers epics whose write_set is
        # disjoint from live-claimed epics' (dispatch-time collision avoidance).
        'write_set': write_set,
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

    ``{'tasks': [...], 'open': N, 'claimed': N, 'done': N, 'blocked': N}`` where
    each task's ``status`` is its EFFECTIVE status (an expired claim → open).
    Never raises.

    The counts use the SAME partition as ``render_board_block`` (and the
    frontend ``renderBoard`` lanes / ``select_dispatchable``) so the collab-bar
    number, the status pillar, and the panel lanes can never drift:
      • a ``kind='lease'`` row is a path RESERVATION, not an epic — it is NEVER
        counted in open/claimed/done (it has its own Held lane);
      • an epic whose block cooldown is still LIVE (effective status ``open``
        but ``blocked_until`` in the future) is counted as ``blocked``, NOT
        ``open`` — it reads as "waiting on a gate", not "claim me".
    ``out['tasks']`` is unchanged: it still carries EVERY row (leases included)
    so readers that partition the task list themselves are unaffected.
    """
    out = {'tasks': [], 'open': 0, 'claimed': 0, 'done': 0, 'blocked': 0}
    if not project_path:
        return out
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT id, title, status, owner_conv_id, lease_expires_at, '
            '       created_by_conv, depends_on, dispatched, kind, '
            '       blocked_until, block_count, block_reason, wait_paths, '
            '       dispatch_target, write_set, created_at, updated_at '
            'FROM project_tasks WHERE project_path=? '
            'ORDER BY created_at ASC', (project_path,)).fetchall()
    except Exception as e:
        logger.warning('[Board] read failed proj=%.40r: %s', project_path, e)
        return out
    now = _now_ms()
    for r in rows:
        t = _row_to_task(r, now)
        out['tasks'].append(t)
        # Leases are reservations, not epics — never in the epic counts (the
        # panel renders them in a separate Held lane).
        if t.get('kind') == 'lease':
            continue
        # A live block cooldown is counted as 'blocked', not 'open' — mirrors
        # render_board_block / renderBoard / select_dispatchable so the collab
        # bar and status pillar agree with the panel lanes.
        if t['status'] == 'open' and int(t.get('blocked_until') or 0) > now:
            out['blocked'] = out.get('blocked', 0) + 1
            continue
        out[t['status']] = out.get(t['status'], 0) + 1
    return out


def post_task(project_path: str, conv_id: str, title: str, *,
              depends_on: list | None = None,
              write_set: list | None = None) -> dict:
    """Post a new OPEN epic to the board. Returns ``{'ok', 'id'?, 'error'?}``.

    ``write_set`` (optional) declares the paths/globs/subsystem-tags this epic
    intends to WRITE, enabling dispatch-time collision avoidance
    (select_dispatchable prefers epics disjoint from live-claimed ones). An
    omitted/empty write_set means "unknown footprint" and is treated as
    non-conflicting, so declaring it is optional and never strands an epic.
    """
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
        task_id = short_id('pt_', 16)
        ts = _now_ms()
        deps = json.dumps([str(d) for d in (depends_on or [])], ensure_ascii=False)
        wset = json.dumps([str(w) for w in (write_set or [])], ensure_ascii=False)
        db.execute(
            'INSERT INTO project_tasks '
            '(id, project_path, title, status, owner_conv_id, lease_expires_at, '
            ' created_by_conv, depends_on, write_set, created_at, updated_at) '
            "VALUES (?, ?, ?, 'open', '', 0, ?, ?, ?, ?, ?)",
            (task_id, project_path, title, conv_id or '', deps, wset, ts, ts))
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
            "dispatched=0, blocked_until=0, block_count=0, block_reason='', "
            "wait_paths='[]', dispatch_target='', updated_at=? "
            'WHERE id=? AND project_path=?',
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
    """Report an epic BLOCKED — stamp a SELF-EXPIRING escalating cooldown so it
    stops being re-dispatched while its external gate is unmet, and emit the
    ``blocked`` feed kind.

    This does NOT change the board status (a block is still not a status — the
    row stays ``open``). What it DOES: increment ``block_count`` and set
    ``blocked_until = now + _block_cooldown_ms(block_count)`` + record the
    ``block_reason``. ``select_dispatchable`` skips a row whose ``blocked_until``
    is still in the future, so the ~30-min lease-expiry re-dispatch churn (a
    billed agent turn each cycle to re-discover the same unmet dep) stops. The
    cooldown escalates (exponential, capped) so a perpetually human-gated epic
    converges to a long sleep after a few retries; it expires at READ time (no
    reaper, no human un-block gate) so it can never deadlock and a resolved dep
    IS retried once the window lapses.

    The ``reason`` should record the block CLASS for HUMAN visibility, e.g.
    ``[human-gated] …`` (only a human action can satisfy it — escalate to the
    long interval fast) vs ``[sibling] …`` (will auto-resolve when a sibling
    commits — retry-after-cooldown is right). The escalation itself is
    class-agnostic; the tag is surfaced on the board card, not branched on.
    Returns ``{'ok', 'blocked_until'?, 'block_count'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    reason = (reason or '').strip()[:_TITLE_MAX_CHARS]
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title, block_count FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        title = row['title'] or ''
        new_count = int(row['block_count'] or 0) + 1
        now = _now_ms()
        # CLASS-AWARE backoff: a [sibling] block is transient (auto-resolves on
        # the sibling's commit, visible immediately on the shared checkout) so
        # it uses the flat lease-clock window; any other reason (human-gated /
        # untagged) rides the escalating human curve.
        block_class = 'sibling' if _SIBLING_TAG in reason.lower() else 'human'
        blocked_until = now + _block_cooldown_ms(new_count, block_class)
        db.execute(
            'UPDATE project_tasks SET blocked_until=?, block_count=?, '
            'block_reason=?, updated_at=? WHERE id=? AND project_path=?',
            (blocked_until, new_count, reason, now, task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] block failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    cooldown_min = _block_cooldown_ms(new_count, block_class) // 60_000
    _emit('blocked', project_path, conv_id,
          f'Blocked: {title}' + (f' — {reason}' if reason else '')
          + f' (retry in ~{cooldown_min}m, block #{new_count})',
          payload={'taskId': task_id, 'reason': reason,
                   'blockedUntil': blocked_until, 'blockCount': new_count})
    audit_log('board_block', project_path=project_path, task_id=task_id,
              conv_id=conv_id, block_count=new_count)
    return {'ok': True, 'blocked_until': blocked_until, 'block_count': new_count}


def reopen_task(project_path: str, conv_id: str, task_id: str) -> dict:
    """Reopen an epic (done|claimed → open) — a HUMAN override / revive lever.

    Note: there is deliberately NO parked/deferred state to un-park (the
    shelving mechanism was removed — the project pushes every open epic forward
    at full speed rather than holding work pending a human decision).

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
            'SELECT title, status, owner_conv_id, blocked_until FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        prev_status = row['status'] or 'open'
        prev_owner = row['owner_conv_id'] or ''
        title = row['title'] or ''
        # A blocked epic is stored status='open' (block never changes status)
        # but carries a live cooldown. Reopen must still act on it — clearing
        # the cooldown for an IMMEDIATE retry (owner constraint) — so 'open' is
        # only "already open" when it also has NO live block cooldown.
        has_live_block = int(row['blocked_until'] or 0) > _now_ms()
        if prev_status == 'open' and not has_live_block:
            return {'ok': False, 'error': 'already_open'}
        db.execute(
            "UPDATE project_tasks SET status='open', owner_conv_id='', "
            "lease_expires_at=0, dispatched=0, blocked_until=0, block_count=0, "
            "block_reason='', wait_paths='[]', dispatch_target='', updated_at=? "
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


def set_write_set(project_path: str, conv_id: str, task_id: str,
                  write_set: list) -> dict:
    """Declare (or clear) the WRITE-SET an epic intends to touch — the
    dispatch-time file-ownership footprint (worktree isolation §4).

    ``write_set`` is a list of path / glob / subsystem-tag strings; an EMPTY
    list clears it ("unknown footprint" → treated as non-conflicting). This does
    NOT change board status. ``select_dispatchable`` PREFERS an epic whose
    write_set is disjoint from every live-claimed epic's write_set, shifting
    collision detection LEFT from land-time to dispatch-time. A soft preference,
    never a hard filter — an undeclared epic is never stranded. Unlike
    ``wait_paths``, the write_set is a STABLE declared property (like
    ``depends_on``) and is NOT reset on complete/reopen. Returns
    ``{'ok', 'write_set'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    clean = []
    for p in (write_set or []):
        s = (str(p) or '').strip()[:_TITLE_MAX_CHARS]
        if s and s not in clean:
            clean.append(s)
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        title = _task_title(db, project_path, task_id)
        if title is None:
            return {'ok': False, 'error': 'task not found'}
        db.execute(
            'UPDATE project_tasks SET write_set=?, updated_at=? '
            'WHERE id=? AND project_path=?',
            (json.dumps(clean), _now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] set_write_set failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('board_write_set', project_path=project_path, task_id=task_id,
              conv_id=conv_id, write_count=len(clean))
    return {'ok': True, 'write_set': clean}


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
    # Leases (kind='lease') are path RESERVATIONS, not epics — partition them
    # out of every epic section and render them in their own "Held" block. Only
    # a LIVE lease (effective status still 'claimed') is a held reservation; an
    # expired one reads 'open' and is simply dropped (it holds nothing).
    epics = [t for t in tasks if t.get('kind') != 'lease']
    now = _now_ms()
    # An epic whose block cooldown is still LIVE (blocked_until > now) is
    # partitioned into its own "Blocked" lane — NOT the Open lane (where it
    # would read as "claim me" and get re-dispatched). Once the cooldown lapses
    # it falls back to Open automatically (at-read-time, no reaper).
    blocked_t = [t for t in epics
                 if t['status'] == 'open' and int(t.get('blocked_until') or 0) > now]
    blocked_ids = {t['id'] for t in blocked_t}
    open_t = [t for t in epics if t['status'] == 'open' and t['id'] not in blocked_ids]
    claimed_t = [t for t in epics if t['status'] == 'claimed']
    done_t = [t for t in epics if t['status'] == 'done']
    if not (open_t or claimed_t or done_t or blocked_t):
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
    if blocked_t:
        lines.append('')
        lines.append('Waiting on an external gate (auto-retries on its own after a '
                     'cooldown — no action needed):')
        for t in blocked_t:
            mins = max(0, (int(t.get('blocked_until') or 0) - now) // 60_000)
            reason = (t.get('block_reason') or '').strip()
            why = f' — {reason}' if reason else ''
            cnt = int(t.get('block_count') or 0)
            lines.append(f'  • [{t["id"]}] {t["title"]}{why} '
                         f'(retry in ~{mins}m, blocked {cnt}×)')
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
                            depends_on=fn_args.get('depends_on'),
                            write_set=fn_args.get('write_set'))
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
            if res.get('ok'):
                mins = _block_cooldown_ms(res.get('block_count', 1)) // 60_000
                return ('Reported blocked. This epic is now on a self-expiring '
                        f'cooldown (~{mins}m, block #{res.get("block_count", 1)}) '
                        'so the autonomous heartbeat will NOT re-dispatch it '
                        'until the external gate has had time to clear. The '
                        'cooldown escalates on repeated blocks and auto-expires '
                        '(no human un-block needed); a human reopen resets it '
                        'for an immediate retry. Tag the reason with the block '
                        'class ([human-gated] vs [sibling]) so it is visible on '
                        'the board.')
            return f'Error reporting block: {res.get("error", "unknown")}.'
        return f"Error: Unknown board tool '{fn_name}'"
    except Exception as e:
        logger.warning('[Board] tool %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'read_board', 'post_task', 'claim_task', 'complete_task', 'block_task',
    'reopen_task',
    'render_board_block', 'execute_board_tool',
    '_effective_status',
    'claims_by_conv', 'DEFAULT_LEASE_TTL_MS',
    'BLOCK_COOLDOWN_BASE_MS', 'BLOCK_COOLDOWN_MAX_MS', '_block_cooldown_ms',
    'set_write_set',
]
