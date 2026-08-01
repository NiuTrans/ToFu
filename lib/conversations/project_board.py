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

# Structured human question on a [human-gated] block (Pillar #3). Capped so a
# pathological payload can't bloat the row; mirrors ask_human's option shape.
_QUESTION_MAX_CHARS = 600
_OPTION_LABEL_MAX = 120
_OPTION_DESC_MAX = 300
_OPTION_MAX = 6

# Prose patterns that ASSERT a structured question card already exists. A
# ``block_reason`` matching one of these while ``question`` is empty is the
# measured 2026-07-31 defect (epic pt_d689f2016ecf4311): two epics sat parked
# ~19 h behind a "4-option question card" that was never created, because the
# author wrote the question into the free-text reason instead of passing
# ``question=``. That state is invisible BOTH ways — ``project_attention``
# builds its "Needs you" card from the ``block_question`` column, and
# ``select_dispatchable`` only honours that same column — so the epic neither
# reached the human nor genuinely stopped.
#
# Matched against a CASEFOLDED reason. Deliberately narrow: each phrase names
# an *interactive control the owner is expected to operate*, which is exactly
# the claim only ``question=`` can make true. A plain "[human-gated] needs infra
# sign-off" asserts no such control and stays legal without a question.
#
# NOT included, on measured evidence: a bare "awaiting owner". The over-firing
# complement in tests caught it firing on the legitimate
# ``[sibling] path=lib/x.py awaiting the owner of that file`` — there "owner"
# means the owner of a FILE, not a human decision-maker. A phrase that also
# describes ordinary sibling coordination cannot carry this refusal.
_QUESTION_CLAIM_PHRASES = (
    'question card',
    'one-click',
    'one click',
    'awaiting your answer',
    'answer the question',
)


def _claims_a_question_card(reason: str) -> bool:
    """True iff ``reason`` asserts that a structured human question is pending.

    Used to refuse a block whose prose promises a card the caller never
    registered. Pure + side-effect-free so the refusal can be evaluated BEFORE
    any mutation.
    """
    low = (reason or '').casefold()
    # A [sibling] block auto-resolves on a peer's commit — it has no human
    # question by construction, so it can never be making this claim.
    if _SIBLING_TAG in low:
        return False
    return any(p in low for p in _QUESTION_CLAIM_PHRASES)


def _clean_block_question(question: str, options) -> str:
    """Sanitize the optional structured human question → canonical JSON (''
    when no question was given). Shape::

        {"q": str, "options": [{"label": str, "description"?: str}]}

    An empty options list means the human answers with free text. Never
    raises; malformed entries are dropped, not repaired.
    """
    q = (question or '').strip()[:_QUESTION_MAX_CHARS]
    if not q:
        return ''
    clean_opts = []
    raw_opts = options if isinstance(options, (list, tuple)) else []
    for o in raw_opts:
        if len(clean_opts) >= _OPTION_MAX:
            break  # cap VALID options — malformed entries never consume a slot
        if isinstance(o, str):
            label, desc = o.strip()[:_OPTION_LABEL_MAX], ''
        elif isinstance(o, dict):
            label = str(o.get('label') or '').strip()[:_OPTION_LABEL_MAX]
            desc = str(o.get('description') or '').strip()[:_OPTION_DESC_MAX]
        else:
            continue
        if not label:
            continue
        item = {'label': label}
        if desc:
            item['description'] = desc
        clean_opts.append(item)
    return json.dumps({'q': q, 'options': clean_opts}, ensure_ascii=False)


_TITLE_MAX_CHARS = 2000  # epics carry multi-sentence design descriptions; a
                         # tight cap silently clipped titles mid-word (both in
                         # the board panel and the injected prompt block)
# Admission guard against runaway posting. This caps only the ACTIVE epics
# (stored status != 'done') — the working set a reader actually has to reason
# about. Completed epics are history: they must NEVER count toward admission
# (otherwise a long-lived project accretes 200 finished epics and the board is
# PERMANENTLY "full", unable to accept a single new epic — the reported bug).
_MAX_ACTIVE_TASKS = 200
# Completed epics are retained for the "Recently done" lane, but capped so the
# table can't grow without bound over a project's life. When a post pushes the
# done-row count past this, the OLDEST done rows are pruned (best-effort, in the
# same connection). The panel/prompt only ever surface the last ~8 done epics.
_MAX_DONE_RETAINED = 100
_MAX_BOARD_TASKS = _MAX_ACTIVE_TASKS  # back-compat alias (was the total cap)


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
    # block_question / human_answer are nullable-safe: a pre-migration row (no
    # column) reads as no-question/no-answer so it is NEVER wrongly suppressed
    # from dispatch. Malformed question JSON also → None (fail-open).
    try:
        _bq_raw = r['block_question'] or ''
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] block_question field parse failed, defaulting: %s', e)
        _bq_raw = ''
    block_question = None
    if _bq_raw:
        try:
            _bq = json.loads(_bq_raw)
            if isinstance(_bq, dict) and str(_bq.get('q') or '').strip():
                _opts = _bq.get('options')
                block_question = {'q': str(_bq['q']),
                                  'options': _opts if isinstance(_opts, list) else []}
        except (TypeError, ValueError) as e:
            logger.debug('[Board] block_question JSON parse failed, defaulting: %s', e)
    try:
        human_answer = r['human_answer'] or ''
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] human_answer field parse failed, defaulting: %s', e)
        human_answer = ''
    # dispatch_target is nullable-safe: a pre-migration row (no column) reads as
    # '' -> dispatch routes to created_by_conv (unchanged).
    try:
        dispatch_target = r['dispatch_target'] or ''
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[Board] dispatch_target field parse failed, defaulting: %s', e)
        dispatch_target = ''
    # wait_paths is nullable-safe: a pre-migration row (no column) reads as
    # an empty list -> nothing waited on (never stranded). Malformed JSON
    # also -> [] (fail-open; docs/PROJECT_BRAIN_WAIT_ON_PATH.md invariant 3).
    try:
        wait_paths = json.loads(r['wait_paths'] or '[]')
        if not isinstance(wait_paths, list):
            wait_paths = []
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.debug('[Board] wait_paths parse failed, defaulting: %s', e)
        wait_paths = []
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
        # Structured human gate: the pending question (dict | None) and the
        # human's answer ('' while unanswered). See block_task / answer_task.
        'block_question': block_question,
        'human_answer': human_answer,
        # dispatch_target: mutable routing override (idle-sibling migration).
        # created_by_conv is immutable authorship; this is who runs it NEXT.
        'dispatch_target': dispatch_target,
        # wait_paths: JSON list of paths this epic waits on — held while any is
        # under a LIVE lease owned by a DIFFERENT conversation (inverse read of
        # the kind='lease' rows; consumed by _originator_stuck condition 3).
        'wait_paths': wait_paths,
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
            '       blocked_until, block_count, block_reason, block_question, '
            '       human_answer, wait_paths, '
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


def _conv_remote_token(db, conv_id: str) -> str:
    """The conv's remote-worktree binding as a write_set TOKEN.

    RWA P5 (docs/REMOTE_WORKTREE_DESIGN.md §5 P5): a conversation whose
    project is the pseudo-path ``remote:<agent>:<root>`` writes on that
    remote root, so its board epics must carry the token in write_set —
    the dispatcher's overlap check then serialises two conversations bound
    to the SAME remote root (different roots/agents never intersect: the
    ':' separator has no prefix-containment semantics). Returns '' for a
    server-local / missing / unreadable binding (fail-open, logged).
    """
    if not conv_id:
        return ''
    try:
        row = db.execute('SELECT settings FROM conversations WHERE id=?',
                         (conv_id,)).fetchone()
        if not row:
            return ''
        raw = row['settings'] if 'settings' in row.keys() else row[0]
        settings = json.loads(raw or '{}')
        path = (settings.get('projectPath') or '') \
            if isinstance(settings, dict) else ''
        return path if path.startswith('remote:') else ''
    except Exception as e:
        logger.debug('[Board] remote binding read failed conv=%s: %s',
                     (conv_id or '')[:8], e)
        return ''


def _merge_remote_token(write_set: list, token: str) -> list:
    """Append the remote token to a write_set list (idempotent dedup)."""
    out = [str(w) for w in (write_set or [])]
    if token and token not in out:
        out.append(token)
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
        # Admission counts ACTIVE epics only (status != 'done') — completed
        # epics are history and must never block a new post, else a long-lived
        # project's board is permanently "full".
        n = db.execute(
            "SELECT COUNT(*) AS c FROM project_tasks "
            "WHERE project_path=? AND status!='done'",
            (project_path,)).fetchone()
        if n and int(n['c']) >= _MAX_ACTIVE_TASKS:
            return {'ok': False,
                    'error': f'board full: {_MAX_ACTIVE_TASKS} active epics '
                             '(complete or reopen some before posting more)'}
        # Prune the oldest completed epics so the retained history stays bounded
        # over the project's life. Best-effort in the same connection; the
        # "Recently done" lane only ever shows the last ~8 anyway.
        try:
            d = db.execute(
                "SELECT COUNT(*) AS c FROM project_tasks "
                "WHERE project_path=? AND status='done'",
                (project_path,)).fetchone()
            done_n = int(d['c']) if d else 0
            if done_n > _MAX_DONE_RETAINED:
                db.execute(
                    'DELETE FROM project_tasks WHERE id IN ('
                    "  SELECT id FROM project_tasks "
                    "  WHERE project_path=? AND status='done' "
                    '  ORDER BY updated_at ASC LIMIT ?)',
                    (project_path, done_n - _MAX_DONE_RETAINED))
        except Exception as e:
            logger.debug('[Board] done-row prune skipped proj=%.40r: %s',
                         project_path, e)
        task_id = short_id('pt_', 16)
        ts = _now_ms()
        deps = json.dumps([str(d) for d in (depends_on or [])], ensure_ascii=False)
        # RWA P5:远程绑定的会话发的 epic 自动携带远程根 token。
        merged_ws = _merge_remote_token(
            write_set, _conv_remote_token(db, conv_id))
        wset = json.dumps(merged_ws, ensure_ascii=False)
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
    # ── Brain dispatch trigger (event channel): an epic that can start RIGHT
    #    NOW (deps met, routing target EXISTS and is IDLE) starts NOW — no 30 s
    #    heartbeat wait. Every other shape (busy poster, unmet deps, dead
    #    target) falls back to the completion nudge / sweep unchanged.
    #    Best-effort: never raises into the post path. ──
    try:
        from lib.conversations.project_dispatch import on_epic_posted
        on_epic_posted(project_path, task_id)
    except Exception as e:
        logger.debug('[Board] post-time dispatch trigger skipped: %s', e)
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
            'SELECT status, owner_conv_id, lease_expires_at, write_set '
            'FROM project_tasks '
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
        # ── CAS claim (TOCTOU guard) ──
        # The eligibility read above and this write are two statements; two
        # conversations racing an OPEN epic would both read 'open' and, with an
        # unconditional UPDATE, both write their own owner (last-writer-wins) —
        # each getting ok=True while only one truly holds the advisory lease.
        # Make the write CONDITIONAL on the exact (owner, lease) pre-state we
        # decided on, so the DB serializes the two writers: the loser's UPDATE
        # matches 0 rows (the winner already changed owner_conv_id /
        # lease_expires_at) and is reported as an advisory refusal, never a
        # silent steal. The precondition admits all three eligible cases —
        # open ('' owner), self-refresh (owner==conv), and expired-lease reclaim
        # (lease<=now) — via the OR below.
        prev_owner = owner
        prev_lease = int(row['lease_expires_at'] or 0)
        # RWA P5:认领会话的远程绑定并入 write_set(与认领同事务)——
        # claimed write_set 是 select_dispatchable 降级排序的输入。
        try:
            cur_ws = json.loads(row['write_set'] or '[]')
        except Exception as _e:
            logger.debug('claim task: failed (%s)', _e)
            cur_ws = []
        merged_ws = _merge_remote_token(
            cur_ws if isinstance(cur_ws, list) else [],
            _conv_remote_token(db, conv_id))
        res = db.execute(
            "UPDATE project_tasks SET status='claimed', owner_conv_id=?, "
            'lease_expires_at=?, dispatched=?, updated_at=?, write_set=? '
            'WHERE id=? AND project_path=? '
            '  AND COALESCE(owner_conv_id,?)=? '
            '  AND COALESCE(lease_expires_at,0)=?',
            (conv_id or '', lease, 1 if dispatched else 0, now,
             json.dumps(merged_ws, ensure_ascii=False),
             task_id, project_path, prev_owner, prev_owner, prev_lease))
        db.commit()
        if getattr(res, 'rowcount', 1) == 0:
            # Lost the race: another writer claimed/refreshed between our read
            # and write. Re-read to report the current owner (advisory refusal).
            cur = db.execute(
                'SELECT owner_conv_id FROM project_tasks '
                'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
            cur_owner = (cur['owner_conv_id'] if cur else '') or ''
            if cur_owner and cur_owner != (conv_id or ''):
                logger.info('[Board] claim lost race proj=%.40r task=%s → owner=%s',
                            project_path, task_id, cur_owner)
                return {'ok': False, 'error': 'already_claimed', 'owner': cur_owner}
            # Rare: state changed but not into a foreign claim (e.g. concurrent
            # complete). Report generically rather than a false success.
            return {'ok': False, 'error': 'claim_conflict'}
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
            "wait_paths='[]', dispatch_target='', block_question='', "
            "human_answer='', updated_at=? "
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


def block_task(project_path: str, conv_id: str, task_id: str, reason: str,
               *, question: str = '', options=None) -> dict:
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

    CONSISTENCY GATE: if ``reason`` asserts that a structured question card is
    awaiting the owner (see ``_claims_a_question_card``) but no ``question`` is
    given, the call is REFUSED with ``error='question_required'`` and NOTHING is
    mutated. Silently accepting it produced the measured 19 h silent park: the
    prose promised a card, the ``block_question`` column stayed empty, so the
    epic was absent from "Needs you" *and* still dispatchable. Refusing loudly
    is strictly better than parking work behind a control that does not exist
    (the same argument as ``update_decision``'s ``summary_required``).
    Returns ``{'ok', 'blocked_until'?, 'block_count'?, 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    reason = (reason or '').strip()
    # Refuse BEFORE any mutation — a rejected block must not half-apply.
    if _claims_a_question_card(reason) and not (question or '').strip():
        logger.warning(
            '[Board] block refused proj=%.40r task=%s: reason claims a pending '
            'question card but no question= was given (would park the epic '
            'behind a card that does not exist)', project_path, task_id)
        return {'ok': False, 'error': 'question_required'}
    if len(reason) > _TITLE_MAX_CHARS:
        # LOUD truncation: both measured reasons were cut mid-word with the
        # author's enumerated options in the discarded tail, and nothing
        # recorded it — turning "I wrote the options" into "the options do not
        # exist" with no trace to diagnose from.
        logger.warning(
            '[Board] block reason truncated proj=%.40r task=%s: %d → %d chars; '
            'the discarded tail is NOT stored (pass structured question=/options= '
            'rather than enumerating them in prose)',
            project_path, task_id, len(reason), _TITLE_MAX_CHARS)
        reason = reason[:_TITLE_MAX_CHARS]
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
        # A structured question makes this a WAIT-FOR-ANSWER block (see
        # answer_task): a fresh block also supersedes (clears) any stale
        # human_answer left from an earlier round.
        question_json = _clean_block_question(question, options)
        db.execute(
            'UPDATE project_tasks SET blocked_until=?, block_count=?, '
            'block_reason=?, block_question=?, human_answer=?, updated_at=? '
            'WHERE id=? AND project_path=?',
            (blocked_until, new_count, reason, question_json, '', now,
             task_id, project_path))
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
                   'blockedUntil': blocked_until, 'blockCount': new_count,
                   'question': (json.loads(question_json) if question_json else None)})
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
            "block_reason='', wait_paths='[]', dispatch_target='', "
            "block_question='', human_answer='', updated_at=? "
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
    # ── Brain dispatch trigger (event channel): the human's revive lever is
    #    also immediate — a reopened epic whose routing target EXISTS and is
    #    IDLE starts NOW, not at the next 30 s sweep. Same seam, same guards
    #    as post_task (busy/dead targets fall back to the nudge/sweep).
    #    Best-effort: never raises into the reopen path. ──
    try:
        from lib.conversations.project_dispatch import on_epic_posted
        on_epic_posted(project_path, task_id)
    except Exception as e:
        logger.debug('[Board] reopen-time dispatch trigger skipped: %s', e)
    return {'ok': True, 'from': prev_status}


def answer_task(project_path: str, conv_id: str, task_id: str, answer: str) -> dict:
    """Record the HUMAN's answer to a pending block question — the close of
    the structured human gate.

    Only meaningful while a question is PENDING (block_question set); refuses
    otherwise (``no_pending_question``). On success it stamps ``human_answer``,
    CLEARS the whole block state (cooldown / count / reason / question) and
    then triggers an IMMEDIATE re-dispatch via ``on_epic_answered`` — the
    kickoff carries the answer, so the assignee proceeds on it without waiting
    for the heartbeat sweep. ``{'ok', 'error'?}``.
    """
    if not project_path or not task_id:
        return {'ok': False, 'error': 'missing project/task'}
    answer = (answer or '').strip()[:_TITLE_MAX_CHARS]
    if not answer:
        return {'ok': False, 'error': 'missing answer'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title, block_question FROM project_tasks '
            'WHERE id=? AND project_path=?', (task_id, project_path)).fetchone()
        if not row:
            return {'ok': False, 'error': 'task not found'}
        title = row['title'] or ''
        question_raw = row['block_question'] or ''
        if not question_raw.strip():
            return {'ok': False, 'error': 'no_pending_question'}
        question_text = ''
        try:
            _bq = json.loads(question_raw)
            if isinstance(_bq, dict):
                question_text = str(_bq.get('q') or '')
        except (TypeError, ValueError) as e:
            logger.debug('[Board] answer: stored question JSON unparseable: %s', e)
        db.execute(
            'UPDATE project_tasks SET human_answer=?, blocked_until=0, '
            "block_count=0, block_reason='', block_question='', updated_at=? "
            'WHERE id=? AND project_path=?',
            (answer, _now_ms(), task_id, project_path))
        db.commit()
    except Exception as e:
        logger.error('[Board] answer failed proj=%.40r task=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    _emit('answered', project_path, conv_id,
          f'Answered: {title} — {answer}',
          payload={'taskId': task_id, 'question': question_text,
                   'answer': answer})
    audit_log('board_answer', project_path=project_path, task_id=task_id,
              conv_id=conv_id, answer_len=len(answer))
    # Immediate re-dispatch (best-effort): never raises into the answer path —
    # a dispatch failure just leaves the epic for the next heartbeat sweep.
    try:
        from lib.conversations.project_dispatch import on_epic_answered
        on_epic_answered(project_path, task_id)
    except Exception as e:
        logger.debug('[Board] post-answer dispatch trigger skipped: %s', e)
    return {'ok': True}


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


# How much of an epic title the PROMPT INJECTION carries. Epics routinely hold
# a whole spec in `title` (stored uncapped up to _TITLE_MAX_CHARS), but the
# injection only has to answer "who is doing what, so I don't collide" — the
# spec is needed when PICKING UP an epic, which is a deliberate tool round.
_INJECT_TITLE_MAX_CHARS = 200


def _abridge_title(title: str) -> str:
    """First line of ``title``, bounded — for the per-turn injection only.

    Returns the title unchanged when it is already short, so the ellipsis stays
    a MEANINGFUL signal ("there is more behind this") rather than decoration on
    every row. The full text always remains reachable via ``project_board_read``
    — an abridgement the model cannot detect or undo would be the worse defect.
    """
    head = (title or '').strip().split('\n', 1)[0].strip()
    multiline = '\n' in (title or '').strip()
    if len(head) <= _INJECT_TITLE_MAX_CHARS and not multiline:
        return head
    return head[:_INJECT_TITLE_MAX_CHARS].rstrip() + ' …'


def render_board_injection_block(project_path: str,
                                 current_conv_id: str = '') -> str:
    """The board as a per-turn PROMPT INJECTION — a coordination summary.

    Same lanes and the same avoid-duplication hint as the full render, but each
    epic is reduced to id + headline + status + owner. Measured on the live
    board this cut the per-turn cost from 16,764 chars to a small fraction,
    with no loss of coordination signal.

    Use ``render_board_block`` (the full render) for the ``project_board_read``
    TOOL and for any human-facing surface — those are pull-based and must show
    the complete epic text.
    """
    return _render_board(project_path, current_conv_id, abridged=True)


def render_board_block(project_path: str, current_conv_id: str = '') -> str:
    """Render the board IN FULL — the pull-based detail channel.

    Backs the ``project_board_read`` agent tool and the panel/influence reads:
    every epic's complete stored text, never abridged. The per-turn prompt
    injection deliberately uses ``render_board_injection_block`` instead.
    """
    return _render_board(project_path, current_conv_id, abridged=False)


def _render_board(project_path: str, current_conv_id: str = '',
                  *, abridged: bool = False) -> str:
    """Shared board renderer — ONE lane-partitioning implementation.

    ``abridged`` selects the per-turn injection shape (short titles + a pointer
    to the detail tool); everything else — lane partitioning, lease expiry,
    the "(you)" stamp, the avoid-duplication hint — is identical, so the two
    consumers can never disagree about WHAT is on the board, only about how
    much of each epic they spell out.
    """
    _t = _abridge_title if abridged else (lambda s: s)
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
    # An epic blocked WITH a structured human question waits for the ANSWER,
    # not for time — its own lane REGARDLESS of cooldown state (auto-retry is
    # paused until answered; the answer re-dispatches it immediately).
    pending_q = [t for t in epics
                 if t['status'] == 'open' and t.get('block_question')
                 and not (t.get('human_answer') or '').strip()]
    pending_ids = {t['id'] for t in pending_q}
    blocked_t = [t for t in epics
                 if t['status'] == 'open' and int(t.get('blocked_until') or 0) > now
                 and t['id'] not in pending_ids]
    blocked_ids = {t['id'] for t in blocked_t}
    open_t = [t for t in epics if t['status'] == 'open'
              and t['id'] not in blocked_ids and t['id'] not in pending_ids]
    claimed_t = [t for t in epics if t['status'] == 'claimed']
    done_t = [t for t in epics if t['status'] == 'done']
    if not (open_t or claimed_t or done_t or blocked_t or pending_q):
        return ''
    lines = ['[PROJECT BOARD] — shared coordination board for this project. '
             'Before starting work, CHECK it: claim an open epic so siblings '
             'know you own it, and do NOT duplicate an epic another '
             'conversation is already advancing.'
             + (' Epics are shown ABRIDGED (headline only, marked …) — call '
                'project_board_read for an epic\'s full text before you work it.'
                if abridged else '')]
    if claimed_t:
        lines.append('')
        lines.append('In progress (claimed by a conversation — AVOID DUPLICATING):')
        for t in claimed_t:
            owner = t['owner_conv_id'] or 'another conversation'
            mine = ' (you)' if current_conv_id and owner == current_conv_id else ''
            hint = '' if mine else ' — another conversation is advancing this; ' \
                   'pick a different epic or coordinate, do not redo it'
            lines.append(f'  • [{t["id"]}] {_t(t["title"])} — claimed by {owner}{mine}{hint}')
    if open_t:
        lines.append('')
        lines.append('Open (unclaimed — claim one with project_board_claim before working it):')
        for t in open_t:
            dep = f' (depends on {", ".join(t["depends_on"])})' if t['depends_on'] else ''
            lines.append(f'  • [{t["id"]}] {_t(t["title"])}{dep}')
    if blocked_t:
        lines.append('')
        lines.append('Waiting on an external gate (auto-retries on its own after a '
                     'cooldown — no action needed):')
        for t in blocked_t:
            mins = max(0, (int(t.get('blocked_until') or 0) - now) // 60_000)
            reason = (t.get('block_reason') or '').strip()
            why = f' — {reason}' if reason else ''
            cnt = int(t.get('block_count') or 0)
            lines.append(f'  • [{t["id"]}] {_t(t["title"])}{why} '
                         f'(retry in ~{mins}m, blocked {cnt}×)')
    if pending_q:
        lines.append('')
        lines.append("Waiting for the human's answer (auto-retry paused — the "
                     'board panel shows a question; answering re-dispatches '
                     'the epic immediately with the answer in context):')
        for t in pending_q:
            q = ((t.get('block_question') or {}).get('q') or '').strip()
            qq = f' — Q: {q}' if q else ''
            lines.append(f'  • [{t["id"]}] {_t(t["title"])}{qq}')
    if done_t:
        lines.append('')
        lines.append('Recently done:')
        for t in done_t[-8:]:
            lines.append(f'  • {_t(t["title"])}')
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
                             fn_args.get('reason') or '',
                             question=fn_args.get('question') or '',
                             options=fn_args.get('options'))
            if res.get('ok') and (fn_args.get('question') or '').strip():
                return ('Reported blocked WITH a question for the human. The '
                        'board panel now shows your question with answer '
                        'controls (one-click options / free text) in its '
                        '"Needs you" surface, and the collaboration bar counts '
                        'it as work that is STOPPED. The epic '
                        'will NOT auto-retry — the moment the human answers, '
                        'it is re-dispatched with the answer in the kickoff '
                        'context. Do NOT re-block on the same gate meanwhile.\n'
                        'While you wait: this epic is parked, but YOU are not. '
                        'Pick up another open epic, or advance any part of this '
                        'one that does not depend on the answer.')
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
                        'the board.\n'
                        'If this gate is really a DECISION you could make '
                        'yourself — reversible, and a matter of engineering '
                        'judgement rather than taste, policy or credentials — '
                        'reopen it, pick the most robust long-term option, and '
                        'record the choice with project_charter_commit instead '
                        'of leaving the epic parked.')
            return f'Error reporting block: {res.get("error", "unknown")}.'
        return f"Error: Unknown board tool '{fn_name}'"
    except Exception as e:
        logger.warning('[Board] tool %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'read_board', 'post_task', 'claim_task', 'complete_task', 'block_task',
    'reopen_task', 'answer_task',
    'render_board_block', 'execute_board_tool',
    '_effective_status',
    'claims_by_conv', 'DEFAULT_LEASE_TTL_MS',
    'BLOCK_COOLDOWN_BASE_MS', 'BLOCK_COOLDOWN_MAX_MS', '_block_cooldown_ms',
    'set_write_set',
]
