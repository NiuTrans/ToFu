"""Human-facing project status synthesis and snapshot history.

Entry points collect live project state, synthesize a bounded narrative, and
append/read snapshots through the Sidecar ``project.status`` operations.
Snapshots and background jobs are keyed by explicit owner plus normalized
project path. They are never injected into agent prompts.
"""

from __future__ import annotations

import json
import time
import uuid

from lib.conversations._bounded_lane import BoundedCoalescingLane
from lib.log import audit_log, get_logger
from lib.storage import get_storage_client
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

# Retention: keep at most this many most-recent snapshots per project (pruned
# on insert). A bounded trail, not an unbounded archive.
_SNAPSHOTS_KEEP = 200

# Bounded narrative so a snapshot row stays cheap to store + render.
_NARRATIVE_MAX_CHARS = 2400

_SYSTEM_PROMPT = (
    'You are the status synthesizer for a software project that multiple AI '
    'conversations ("the project brain") are working on in parallel. Your job '
    'is to tell the human OWNER, at a glance, WHERE THE PROJECT IS and whether '
    'it is DRIFTING from the stated goal.\n'
    'You are given the project north-star + committed decisions (the charter), '
    "the in-flight and finished work (the board's epics), recent blocks, and a "
    'digest of the sibling conversations. Write a concise status:\n'
    '- Lead with the current state: what has shipped, what is in flight (name '
    'the epics + who is advancing them), what is blocked or awaiting the human.\n'
    '- Then an explicit ALIGNMENT read: is the current work tracking the '
    "charter's north-star and committed decisions, or drifting? If drifting, "
    'name the drift concretely. If there is no charter yet, say so.\n'
    '- Be specific and dense. No greetings, no filler, no markdown headings.\n'
    '- Use the SAME language as the charter/goal text (Chinese if it is '
    'Chinese, else English).\n'
    '- 2 to 5 sentences.'
)


# Event-driven status refreshes are intentionally asynchronous, but a raw
# ``Thread(...).start()`` per charter commit turns a burst into an unbounded
# number of simultaneous LLM calls.  Keep a small process-wide worker lane and
# coalesce by project: one project has at most one active synthesis, and a
# change that arrives during it causes one follow-up pass over the newest
# state.  Different projects still make bounded progress in parallel.
_BACKGROUND_WORKERS = 2
_BACKGROUND_CAPACITY = resolve_resource_budget(
    'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY', maximum=4096)


def _merge_background_request(
    current: tuple[str, bool], newest: tuple[str, bool]
) -> tuple[str, bool]:
    return newest[0], bool(current[1] or newest[1])


def _consume_background_request(
    scope: tuple[int, str], request: tuple[str, bool]
) -> None:
    user_id, project_path = scope
    trigger, force = request
    _build_status_snapshot_blocking(
        project_path,
        user_id=user_id,
        trigger=trigger,
        force=force,
    )


def _report_background_failure(
    scope: tuple[int, str], error: Exception
) -> None:
    logger.warning(
        '[ProjStatus] background worker failed proj=%.40r: %s',
        scope[1], error, exc_info=True)


_background_lane = BoundedCoalescingLane[
    tuple[int, str], tuple[str, bool]
](
    name='project-status',
    workers=_BACKGROUND_WORKERS,
    capacity=_BACKGROUND_CAPACITY,
    merge=_merge_background_request,
    consume=_consume_background_request,
    on_error=_report_background_failure,
)


def _schedule_background_snapshot(
    project_path: str,
    *,
    user_id: int,
    trigger: str,
    force: bool,
) -> bool:
    """Coalesce one non-blocking refresh into the bounded worker lane."""
    scope = (int(user_id), project_path)
    return _background_lane.submit(scope, (trigger, bool(force)))


def _wait_for_background_status(timeout: float = 5.0) -> bool:
    """Wait until the status lane is idle; lifecycle/test diagnostic seam."""
    return _background_lane.wait_idle(timeout)


def background_status_lane_snapshot() -> dict[str, float | int | str]:
    """Operational counters for capacity, saturation, and coalescing."""
    return _background_lane.snapshot()


def collect_pillar_state(project_path: str, *, user_id: int) -> dict:
    """Read LIVE state across the six pillars into one evidence dict.

    This is the SAME cross-pillar join ``build_brain_summary`` performs (board
    counts + the ``claims_by_conv`` peer→epic join + charter + pending
    proposals + presence + sibling digest); it is the authoritative "what the
    brain sees" evidence a narrative is generated from and stored alongside it.

    Best-effort: every sub-read degrades to a safe default; never raises.
    Returns a dict with: ``epicsOpen/Claimed/Done/Blocked``, ``epicsInFlight``
    (list of {title, owner}), ``pendingDecisions``, ``charterExists``,
    ``charterVersion``, ``northStar``, ``decisions`` (list of str),
    ``activePeers``, ``recentBlocks`` (list of str), ``siblings`` (list of
    {title, summary}).
    """
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    state = {
        'epicsOpen': 0, 'epicsClaimed': 0, 'epicsDone': 0, 'epicsBlocked': 0,
        'epicsInFlight': [], 'pendingDecisions': 0, 'charterExists': False,
        'charterVersion': 0, 'northStar': '', 'decisions': [],
        'activePeers': 0, 'recentBlocks': [], 'siblings': [],
    }
    if not project_path:
        return state

    # ── Board: counts + in-flight epics (claimed, live lease) ──
    board_tasks = []
    try:
        from lib.conversations.project_board import read_board
        board = read_board(project_path, user_id=user_id)
        state['epicsOpen'] = int(board.get('open', 0))
        state['epicsClaimed'] = int(board.get('claimed', 0))
        state['epicsDone'] = int(board.get('done', 0))
        state['epicsBlocked'] = int(board.get('blocked', 0))
        board_tasks = board.get('tasks', []) or []
        for t in board_tasks:
            if t.get('status') == 'claimed' and t.get('kind', 'epic') != 'lease':
                state['epicsInFlight'].append({
                    'title': t.get('title', ''),
                    'owner': t.get('owner_conv_id', ''),
                })
    except Exception as e:
        logger.debug('[ProjStatus] board read failed proj=%.40r: %s',
                     project_path, e)

    # ── Charter: north-star + committed decisions + version ──
    try:
        from lib.conversations.project_charter import read_charter
        rec = read_charter(project_path, user_id=user_id)
        state['charterExists'] = bool(rec.get('exists'))
        state['charterVersion'] = int(rec.get('version', 0))
        state['northStar'] = (rec.get('content') or '').strip()
        decisions = []
        for d in (rec.get('decisions') or [])[-20:]:
            txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
            if txt:
                decisions.append(txt)
        state['decisions'] = decisions
    except Exception as e:
        logger.debug('[ProjStatus] charter read failed proj=%.40r: %s',
                     project_path, e)

    # ── Pending decisions (the human-gate count) — SINGLE source ──
    try:
        from lib.conversations.project_charter import pending_proposals
        state['pendingDecisions'] = len(
            pending_proposals(project_path, user_id=user_id))
    except Exception as e:
        logger.debug('[ProjStatus] pending read failed proj=%.40r: %s',
                     project_path, e)

    # ── Presence: active conversation-level peers ──
    try:
        from lib.presence.registry import snapshot
        peers = snapshot(project_path, user_id=user_id).get('peers', []) or []
        conv_ids = {p.get('convId') for p in peers
                    if p.get('convId') and not p.get('agentId')}
        state['activePeers'] = len(conv_ids)
    except Exception as e:
        logger.debug('[ProjStatus] presence read failed proj=%.40r: %s',
                     project_path, e)

    # ── Feed: recent 'blocked' events (why work stalled) ──
    try:
        from lib.conversations.project_feed import read_project_feed
        feed = read_project_feed(project_path, user_id=user_id, limit=80)
        blocks = []
        for e in feed.get('events', []):
            if e.get('kind') == 'blocked':
                s = (e.get('summary') or '').strip()
                if s:
                    blocks.append(s)
        state['recentBlocks'] = blocks[:6]
    except Exception as e:
        logger.debug('[ProjStatus] feed read failed proj=%.40r: %s',
                     project_path, e)

    # ── Sibling digest: bounded title+summary of other conversations ──
    try:
        from lib.conversations.project_summary import project_digest_entries
        entries = project_digest_entries(
            project_path, user_id=user_id, limit=10)
        state['siblings'] = [{'title': e.get('title', ''),
                              'summary': e.get('summary', '')}
                             for e in entries]
    except Exception as e:
        logger.debug('[ProjStatus] digest read failed proj=%.40r: %s',
                     project_path, e)

    return state


def _fingerprint(pillar_state: dict) -> str:
    """Cheap change key for the staleness gate.

    A snapshot is regenerated only when this key differs from the last stored
    snapshot's. Keyed on the coarse, human-meaningful signals (epic counts,
    pending decisions, blocked count, charter version, active-peer count, and
    the set of in-flight epic titles) — NOT on volatile fields (timestamps,
    presence heartbeat jitter) that would defeat the laziness discipline.
    """
    inflight = sorted(e.get('title', '') for e in pillar_state.get('epicsInFlight', []))
    key = {
        'o': pillar_state.get('epicsOpen', 0),
        'c': pillar_state.get('epicsClaimed', 0),
        'd': pillar_state.get('epicsDone', 0),
        'b': pillar_state.get('epicsBlocked', 0),
        'p': pillar_state.get('pendingDecisions', 0),
        'v': pillar_state.get('charterVersion', 0),
        'if': inflight,
        'rb': len(pillar_state.get('recentBlocks', [])),
    }
    return json.dumps(key, ensure_ascii=False, sort_keys=True)


def _build_synthesis_source(pillar_state: dict) -> str:
    """Render the pillar-state evidence into a compact LLM prompt body."""
    lines = []
    if pillar_state.get('charterExists') and pillar_state.get('northStar'):
        lines.append('PROJECT NORTH-STAR:\n' + pillar_state['northStar'][:1600])
    elif not pillar_state.get('charterExists'):
        lines.append('PROJECT NORTH-STAR: (none — no charter committed yet)')
    decisions = pillar_state.get('decisions') or []
    if decisions:
        lines.append('\nCOMMITTED DECISIONS:')
        for d in decisions[:12]:
            lines.append(f'  • {d[:400]}')
    lines.append(
        '\nBOARD: %d open, %d in-flight (claimed), %d done, %d blocked.'
        % (pillar_state.get('epicsOpen', 0), pillar_state.get('epicsClaimed', 0),
           pillar_state.get('epicsDone', 0), pillar_state.get('epicsBlocked', 0)))
    inflight = pillar_state.get('epicsInFlight') or []
    if inflight:
        lines.append('IN-FLIGHT EPICS:')
        for e in inflight[:12]:
            owner = (e.get('owner') or '')[:12]
            lines.append(f'  • {e.get("title", "")[:300]}'
                         + (f' (conv {owner})' if owner else ''))
    if pillar_state.get('pendingDecisions'):
        lines.append('\nDECISIONS AWAITING THE HUMAN: %d'
                     % pillar_state['pendingDecisions'])
    blocks = pillar_state.get('recentBlocks') or []
    if blocks:
        lines.append('\nRECENT BLOCKS:')
        for b in blocks[:6]:
            lines.append(f'  • {b[:300]}')
    siblings = pillar_state.get('siblings') or []
    if siblings:
        lines.append('\nSIBLING CONVERSATIONS:')
        for s in siblings[:10]:
            summ = (s.get('summary') or '').strip()
            lines.append(f'  • {s.get("title", "")[:120]}'
                         + (f' — {summ[:200]}' if summ else ''))
    lines.append('\nActive peer conversations right now: %d'
                 % pillar_state.get('activePeers', 0))
    return '\n'.join(lines)


def generate_narrative(pillar_state: dict) -> str:
    """Synthesize the status narrative from live pillar state via the cheap
    model. Returns '' on failure / empty input (caller keeps the prior text).
    """
    source = _build_synthesis_source(pillar_state)
    if not source.strip():
        return ''
    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user',
                 'content': f'Project state:\n\n{source}\n\nStatus:'},
            ],
            max_tokens=800,
            temperature=0.3,
            capability='cheap',
            log_prefix='[ProjStatus]',
        )
    except Exception as e:
        logger.warning('[ProjStatus] synthesis dispatch failed after %.1fs: %s',
                       time.time() - started, e)
        return ''
    text = (content or '').strip()
    if len(text) > _NARRATIVE_MAX_CHARS:
        text = text[:_NARRATIVE_MAX_CHARS].rstrip() + '…'
    if text:
        logger.info('[ProjStatus] synthesized narrative=%.80r in %.1fs',
                    text, time.time() - started)
    return text


def _read_latest_snapshot(project_path: str, *, user_id: int) -> dict | None:
    """Return the most-recent snapshot row for ``project_path`` (or None)."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return None
    try:
        result = get_storage_client().query(
            'project.status.list', {
                'project_path': project_path,
                'user_id': int(user_id),
                'limit': 1,
            },
        )
    except Exception as e:
        logger.debug('[ProjStatus] latest read failed proj=%.40r: %s',
                     project_path, e)
        return None
    return (result.get('snapshots') or [None])[0]


def read_status_history(
    project_path: str,
    *,
    user_id: int,
    limit: int = 30,
) -> dict:
    """Read the snapshot trail for ``project_path`` (newest-first).

    Read-only, NO synthesis. Returns ``{'snapshots': [...newest-first...],
    'maxSeq': int}``. Returns the empty shape on no project / DB error.
    """
    out = {'snapshots': [], 'maxSeq': 0}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return out
    limit = max(1, min(int(limit or 30), _SNAPSHOTS_KEEP))
    try:
        return get_storage_client().query(
            'project.status.list', {
                'project_path': project_path,
                'user_id': int(user_id),
                'limit': limit,
            },
        )
    except Exception as e:
        logger.warning('[ProjStatus] history read failed proj=%.40r: %s',
                       project_path, e)
        return out


def _persist_snapshot(
    project_path: str,
    narrative: str,
    pillar_state: dict,
    trigger: str,
    *,
    user_id: int,
) -> dict | None:
    """Append one snapshot row under the monotonic-seq lock; prune old rows."""
    snapshot_id = uuid.uuid4().hex
    try:
        snapshot = get_storage_client(write=True).command(
            'project.status.append', {
                'project_path': project_path,
                'user_id': int(user_id),
                'snapshot_id': snapshot_id,
                'narrative': narrative,
                'pillar_state': pillar_state,
                'trigger': trigger or 'manual',
                'keep': _SNAPSHOTS_KEEP,
            },
            f'project.status:{int(user_id)}:{project_path}:{snapshot_id}',
        )
    except Exception as e:
        logger.warning('[ProjStatus] persist failed proj=%.40r: %s',
                       project_path, e)
        return None
    audit_log('project_status_snapshot', user_id=int(user_id),
              project_path=project_path,
              seq=int(snapshot.get('seq') or 0), trigger=trigger)
    return snapshot


def build_status_snapshot(project_path: str, *, user_id: int,
                          trigger: str = 'manual',
                          force: bool = False,
                          blocking: bool = True) -> dict | None:
    """Ensure ``project_path`` has a fresh status snapshot; return the latest.

    Reads live pillar state, and if the pillar-state fingerprint changed since
    the last stored snapshot (or ``force``), synthesizes a new narrative and
    appends it. Otherwise returns the cached latest snapshot WITHOUT an LLM
    call (the laziness gate). Never raises.

    Args:
        trigger: what caused this (``epic_completed`` / ``decision_committed`` /
            ``blocked`` / ``on_open`` / ``manual``).
        force: synthesize even if the fingerprint is unchanged.
        blocking: when False, spawn a daemon thread to do the (possibly LLM)
            work and return the cached latest snapshot immediately — used by
            the event-driven warm-keeping triggers so a settled action never
            blocks on an LLM call.

    Returns the latest snapshot dict (fresh or cached), or None on no project.
    """
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return None

    if not blocking:
        cached = _read_latest_snapshot(project_path, user_id=user_id)
        _schedule_background_snapshot(
            project_path, user_id=user_id, trigger=trigger, force=force)
        return cached

    return _build_status_snapshot_blocking(
        project_path, user_id=user_id, trigger=trigger, force=force)


def _build_status_snapshot_blocking(
    project_path: str,
    *,
    user_id: int,
    trigger: str,
    force: bool,
) -> dict | None:
    """Inline collect → staleness-gate → synthesize-if-stale → persist."""
    pillar_state = collect_pillar_state(project_path, user_id=user_id)
    latest = _read_latest_snapshot(project_path, user_id=user_id)
    if not force and latest is not None:
        prev_fp = _fingerprint(latest.get('pillar_state') or {})
        if prev_fp == _fingerprint(pillar_state):
            # Quiescent — no material change since the last snapshot. Reuse it,
            # no LLM call (the laziness discipline).
            return latest

    narrative = generate_narrative(pillar_state)
    if not narrative:
        # LLM failed / empty — keep the previous snapshot rather than writing
        # an empty one. On a first-ever snapshot with no narrative, return None.
        logger.debug('[ProjStatus] no narrative produced proj=%.40r (kept prior)',
                     project_path)
        return latest

    snap = _persist_snapshot(
        project_path,
        narrative,
        pillar_state,
        trigger,
        user_id=user_id,
    )
    return snap or latest


def get_status_view(project_path: str, *, user_id: int, limit: int = 30,
                    force: bool = False) -> dict:
    """Non-blocking status view for the tab-open path.

    Returns the CACHED latest snapshot + history IMMEDIATELY (never blocks on an
    LLM). Cheaply checks the staleness gate (a pillar-state fingerprint compare,
    no LLM); if the state has moved since the last snapshot (or ``force``), it
    warms a fresh snapshot in a background daemon thread and flags
    ``refreshing=True`` so the client can poll the read-only history endpoint
    for the new row instead of staring at a full-screen "Synthesizing…" box
    while the synthesis runs synchronously.

    This is what fixes the "stuck on Synthesizing project status" tab: the old
    route called ``build_status_snapshot(blocking=True)`` and held the HTTP
    response open for the entire cheap-model synthesis.

    Returns ``{latest, history, maxSeq, refreshing}``. Never raises.
    """
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return {'latest': None, 'history': [], 'maxSeq': 0, 'refreshing': False}

    hist = read_status_history(project_path, user_id=user_id, limit=limit)
    snapshots = hist.get('snapshots', [])
    latest = snapshots[0] if snapshots else None

    # Cheap staleness check — a fingerprint compare, NO LLM. When the project
    # has moved (or on a first-ever open with no snapshot yet, or force), warm
    # a fresh one in the background and tell the client to poll.
    refreshing = False
    try:
        pillar_state = collect_pillar_state(project_path, user_id=user_id)
        stale = (
            force or latest is None
            or _fingerprint(latest.get('pillar_state') or {})
            != _fingerprint(pillar_state))
    except Exception as e:
        logger.debug('[ProjStatus] staleness check failed proj=%.40r: %s',
                     project_path, e)
        stale = False

    if stale:
        refreshing = True
        # Fire-and-forget warm (the blocking builder re-collects + re-checks the
        # gate itself, so a racing warm is harmless — it dedups on fingerprint).
        try:
            build_status_snapshot(
                project_path,
                user_id=user_id,
                trigger='on_open',
                force=force,
                blocking=False,
            )
        except Exception as e:
            logger.warning('[ProjStatus] background warm failed proj=%.40r: %s',
                           project_path, e)
            refreshing = False

    return {'latest': latest, 'history': snapshots,
            'maxSeq': hist.get('maxSeq', 0), 'refreshing': refreshing}


def answer_status_question(
    project_path: str,
    question: str,
    *,
    user_id: int,
) -> dict:
    """Read-only synthesis Q&A: the human's question + LIVE pillar state → an
    answer. Writes NOTHING (no snapshot appended). Returns ``{'ok', 'answer'?,
    'pillar_state'?, 'error'?}``. Never raises.
    """
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    question = (question or '').strip()
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not question:
        return {'ok': False, 'error': 'empty question'}
    pillar_state = collect_pillar_state(project_path, user_id=user_id)
    source = _build_synthesis_source(pillar_state)
    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': _SYSTEM_PROMPT
                 + '\n\nThe human is asking a SPECIFIC question about the '
                   'project. Answer it directly and concretely using ONLY the '
                   'project state provided. If the state does not contain the '
                   'answer, say so plainly — do NOT invent facts.'},
                {'role': 'user',
                 'content': f'Project state:\n\n{source}\n\n'
                            f'Question: {question}\n\nAnswer:'},
            ],
            max_tokens=1000,
            temperature=0.3,
            capability='cheap',
            log_prefix='[ProjStatus:ask]',
        )
    except Exception as e:
        logger.warning('[ProjStatus] ask dispatch failed after %.1fs: %s',
                       time.time() - started, e)
        return {'ok': False, 'error': 'synthesis failed'}
    answer = (content or '').strip()
    if not answer:
        return {'ok': False, 'error': 'empty answer'}
    if len(answer) > _NARRATIVE_MAX_CHARS:
        answer = answer[:_NARRATIVE_MAX_CHARS].rstrip() + '…'
    logger.info('[ProjStatus] answered question=%.60r in %.1fs',
                question, time.time() - started)
    return {'ok': True, 'answer': answer, 'pillar_state': pillar_state}


def status_line(project_path: str, *, user_id: int) -> str:
    """The one-line status headline for the collab-bar (ambient perception).

    Returns the FIRST sentence of the latest stored snapshot's narrative, or ''
    when there is no snapshot yet. Read-only, NO synthesis (cheap enough for the
    always-visible bar).
    """
    latest = _read_latest_snapshot(project_path, user_id=user_id)
    if not latest or not latest.get('narrative'):
        return ''
    text = latest['narrative'].strip()
    # First sentence (bounded), so the bar shows a headline not a paragraph.
    import re
    m = re.split(r'(?<=[.。!?！？])\s', text, maxsplit=1)
    head = (m[0] if m else text).strip()
    return head[:200]


__all__ = [
    'collect_pillar_state', 'generate_narrative', 'build_status_snapshot',
    'read_status_history', 'get_status_view', 'answer_status_question',
    'status_line', '_SNAPSHOTS_KEEP',
]
