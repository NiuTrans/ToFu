"""lib.conversations.project_status — the human↔brain status lane (Pillar #7).

Where the first six pillars (Activity Feed, Charter, Board, presence, per-conv
summaries, peer comms) are agent-facing blackboards the human can read and poke
per-cell, THIS pillar is the human's window back in as automation runs ahead:

  • a **persistent, append-only history of project-status snapshots** — the
    durable memory of *where the project IS* (the Charter is durable memory of
    *intent*; the Feed is a raw ephemeral pulse; per-conv summaries were never
    aggregated). The human works with facts that drift over time and needs the
    TRAIL — how the project got here — not a single overwritten narrative.

  • a **synthesis generator** that reads LIVE pillar state and produces one
    bounded narrative answering "where are we / are we drifting from the
    charter", with an explicit alignment-to-north-star read.

Design invariants (owner-locked 2026-07-08; see docs/PROJECT_BRAIN_STATUS_LANE.md):
  • **Keyed strictly on ``project_path``** — never a process-global singleton.
  • **Append-only**, monotonic per-project ``seq`` minted under one module lock
    (mirrors ``project_feed.emit_project_event`` exactly).
  • **Laziness (reuse ``ensure_summary``'s discipline):** a new snapshot is
    minted only when the pillar-state FINGERPRINT changed since the last
    snapshot; a quiescent project is never re-synthesized (no LLM), and repeated
    tab-opens are free.
  • **Best-effort** — any pillar sub-read failing degrades that field; the
    generator NEVER raises into a caller, and keeps the previous snapshot rather
    than writing an empty one on LLM failure.
  • **HUMAN-FACING ONLY** — this memory is NEVER injected into
    ``lib/tasks_pkg/system_context.py`` / sibling agent prompts. That would blur
    the human↔brain lane into the agent↔agent lane and re-raise the
    coupling/storm concerns. (Guarded by test_project_status_no_ambient_injection.)
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

# Retention: keep at most this many most-recent snapshots per project (pruned
# on insert). A bounded trail, not an unbounded archive.
_SNAPSHOTS_KEEP = 200

# Bounded narrative so a snapshot row stays cheap to store + render.
_NARRATIVE_MAX_CHARS = 2400

# Serializes the read-max-then-insert of the per-project monotonic seq so two
# concurrent writers for the SAME project can't mint the same (path, seq) PK.
_snapshot_lock = threading.Lock()

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


_now_ms = now_ms


def collect_pillar_state(project_path: str) -> dict:
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
        board = read_board(project_path)
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
        rec = read_charter(project_path)
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
        state['pendingDecisions'] = len(pending_proposals(project_path))
    except Exception as e:
        logger.debug('[ProjStatus] pending read failed proj=%.40r: %s',
                     project_path, e)

    # ── Presence: active conversation-level peers ──
    try:
        from lib.presence.registry import snapshot
        peers = snapshot(project_path).get('peers', []) or []
        conv_ids = {p.get('convId') for p in peers
                    if p.get('convId') and not p.get('agentId')}
        state['activePeers'] = len(conv_ids)
    except Exception as e:
        logger.debug('[ProjStatus] presence read failed proj=%.40r: %s',
                     project_path, e)

    # ── Feed: recent 'blocked' events (why work stalled) ──
    try:
        from lib.conversations.project_feed import read_project_feed
        feed = read_project_feed(project_path, limit=80)
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
        entries = project_digest_entries(project_path, limit=10)
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


def _read_latest_snapshot(project_path: str) -> dict | None:
    """Return the most-recent snapshot row for ``project_path`` (or None)."""
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    if not project_path:
        return None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT project_path, seq, snapshot_id, narrative, pillar_state, '
            '       trigger, ts FROM project_status_snapshots '
            'WHERE project_path=? ORDER BY seq DESC LIMIT 1',
            (project_path,)).fetchone()
    except Exception as e:
        logger.debug('[ProjStatus] latest read failed proj=%.40r: %s',
                     project_path, e)
        return None
    return _row_to_snapshot(row) if row else None


def _row_to_snapshot(r) -> dict:
    try:
        pillar_state = json.loads(r['pillar_state']) if r['pillar_state'] else {}
    except (TypeError, ValueError) as e:
        logger.debug('[ProjStatus] pillar_state parse failed, defaulting: %s', e)
        pillar_state = {}
    return {
        'seq': int(r['seq']), 'snapshot_id': r['snapshot_id'],
        'narrative': r['narrative'] or '', 'pillar_state': pillar_state,
        'trigger': r['trigger'] or '', 'ts': int(r['ts'] or 0),
    }


def read_status_history(project_path: str, limit: int = 30) -> dict:
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
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT project_path, seq, snapshot_id, narrative, pillar_state, '
            '       trigger, ts FROM project_status_snapshots '
            'WHERE project_path=? ORDER BY seq DESC LIMIT ?',
            (project_path, limit)).fetchall()
    except Exception as e:
        logger.warning('[ProjStatus] history read failed proj=%.40r: %s',
                       project_path, e)
        return out
    snapshots = [_row_to_snapshot(r) for r in rows]
    out['snapshots'] = snapshots
    out['maxSeq'] = snapshots[0]['seq'] if snapshots else 0
    return out


def _persist_snapshot(project_path: str, narrative: str, pillar_state: dict,
                      trigger: str) -> dict | None:
    """Append one snapshot row under the monotonic-seq lock; prune old rows."""
    snapshot_id = uuid.uuid4().hex
    ts = _now_ms()
    try:
        pillar_json = json.dumps(pillar_state, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[ProjStatus] pillar_state dump failed, defaulting: %s', e)
        pillar_json = '{}'
    try:
        with _snapshot_lock:
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT COALESCE(MAX(seq), 0) AS m FROM project_status_snapshots '
                'WHERE project_path=?', (project_path,)).fetchone()
            seq = (row['m'] if row and row['m'] is not None else 0) + 1
            db.execute(
                'INSERT INTO project_status_snapshots '
                '(project_path, seq, snapshot_id, narrative, pillar_state, '
                ' trigger, ts) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (project_path, seq, snapshot_id, narrative, pillar_json,
                 trigger or 'manual', ts))
            if seq > _SNAPSHOTS_KEEP:
                db.execute(
                    'DELETE FROM project_status_snapshots '
                    'WHERE project_path=? AND seq <= ?',
                    (project_path, seq - _SNAPSHOTS_KEEP))
            db.commit()
    except Exception as e:
        logger.warning('[ProjStatus] persist failed proj=%.40r: %s',
                       project_path, e)
        return None
    audit_log('project_status_snapshot', project_path=project_path,
              seq=seq, trigger=trigger)
    return {'seq': seq, 'snapshot_id': snapshot_id, 'narrative': narrative,
            'pillar_state': pillar_state, 'trigger': trigger or 'manual',
            'ts': ts}


def build_status_snapshot(project_path: str, *, trigger: str = 'manual',
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
        cached = _read_latest_snapshot(project_path)
        threading.Thread(
            target=_build_status_snapshot_blocking,
            args=(project_path,), kwargs={'trigger': trigger, 'force': force},
            name=f'projstatus-{project_path[-8:]}', daemon=True,
        ).start()
        return cached

    return _build_status_snapshot_blocking(project_path, trigger=trigger,
                                           force=force)


def _build_status_snapshot_blocking(project_path: str, *, trigger: str,
                                    force: bool) -> dict | None:
    """Inline collect → staleness-gate → synthesize-if-stale → persist."""
    pillar_state = collect_pillar_state(project_path)
    latest = _read_latest_snapshot(project_path)
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

    snap = _persist_snapshot(project_path, narrative, pillar_state, trigger)
    return snap or latest


def get_status_view(project_path: str, *, limit: int = 30,
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

    hist = read_status_history(project_path, limit=limit)
    snapshots = hist.get('snapshots', [])
    latest = snapshots[0] if snapshots else None

    # Cheap staleness check — a fingerprint compare, NO LLM. When the project
    # has moved (or on a first-ever open with no snapshot yet, or force), warm
    # a fresh one in the background and tell the client to poll.
    refreshing = False
    try:
        pillar_state = collect_pillar_state(project_path)
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
            build_status_snapshot(project_path, trigger='on_open',
                                  force=force, blocking=False)
        except Exception as e:
            logger.warning('[ProjStatus] background warm failed proj=%.40r: %s',
                           project_path, e)
            refreshing = False

    return {'latest': latest, 'history': snapshots,
            'maxSeq': hist.get('maxSeq', 0), 'refreshing': refreshing}


def answer_status_question(project_path: str, question: str) -> dict:
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
    pillar_state = collect_pillar_state(project_path)
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


def status_line(project_path: str) -> str:
    """The one-line status headline for the collab-bar (ambient perception).

    Returns the FIRST sentence of the latest stored snapshot's narrative, or ''
    when there is no snapshot yet. Read-only, NO synthesis (cheap enough for the
    always-visible bar).
    """
    latest = _read_latest_snapshot(project_path)
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
