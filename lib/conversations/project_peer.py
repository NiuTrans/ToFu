"""lib.conversations.project_peer — cross-conversation COMMUNICATION + INTERVENTION.

The Project Brain's first five pillars (feed / charter / board / presence /
dispatch) give conversations shared PERCEPTION and one-way engine-driven
dispatch, but no way for one conversation to *talk to* or *intervene in*
another. This module closes that gap with three agent verbs, all project-scoped
and all preserving the board's "advisory, never a hard lock" ethos:

  1. **``build_peer_status(project_path, conv_id?)``** — READ-ONLY live
     introspection. Joins ``presence.snapshot`` (who is active + current phase /
     file) with the live task registry (current round / status) and the board's
     ``claims_by_conv`` map (which epic each peer is advancing). This is LIVE
     state — not ``get_conversation`` history.

  2. **``send_peer_message(project_path, from_conv, to_conv, text)``** —
     agent-initiated peer messaging. Enqueues a ``KIND_PEER_MSG`` turn source
     into the target's queue (seen on its NEXT turn — NEVER interrupts a live
     turn mid-stream) and mirrors ONE ``note`` event into the activity feed,
     stamped with the sender. Rate-limited per (sender, target) so an A↔B
     chatter loop is mathematically bounded.

  3. **``intervene_peer(...)``** — advisory-first intervention. The DEFAULT is
     a high-priority advisory note ("stop, you're duplicating epic X"); a
     genuine hard abort of the peer's running task
     (``abort_running_tasks_for_conv``) is a SEPARATE, AUDIT-GATED action that
     refuses without an ``approved_by`` token — never a silent kill, and it
     aborts the task only, never the host/process.

Loop / storm guard (owner hard constraint — A→B→A auto-amplification must be
impossible):
  • **Rate limit per ordered (sender, target) pair** — at most
    ``_PEER_MSG_MAX_PER_WINDOW`` messages per ``_PEER_MSG_WINDOW_S``. With N
    conversations and every pair capped per window, total peer traffic per
    window is bounded by ``N·(N-1)·cap`` — a storm is impossible by
    construction, not by convention.
  • **No self-send** — a conversation can never message itself.
  • **No auto-relay** — receiving a peer message enqueues plain turn content;
    it invokes NO send path. The ONLY way a message is sent is an explicit
    model tool call, so nothing here can auto-trigger another send.

Every function keys STRICTLY on ``project_path`` (never a process-global) and is
best-effort — a sub-read failing degrades that field; the verbs never raise into
the caller.
"""

from __future__ import annotations

import threading
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# ── Rate-limit knobs (the storm guard) ──
# At most _PEER_MSG_MAX_PER_WINDOW messages per ordered (sender, target) pair
# per _PEER_MSG_WINDOW_S seconds. Deliberately small: peer messages are a
# coordination nudge, not a chat channel.
_PEER_MSG_WINDOW_S = 120.0
_PEER_MSG_MAX_PER_WINDOW = 3

# Soft cap on a peer message body (mirrors the feed summary cap intent).
_PEER_MSG_MAX_CHARS = 1200


# The canonical conversation-id width the message-queue / task-registry key is
# matched on. A shorter value is a TRUNCATED id (a prefix a peer copied from a
# status view); enqueuing under it lands in a phantom queue no conversation
# drains. _resolve_target_conv_id fails CLOSED for a sub-length id on DB error.
_FULL_CONV_ID_LEN = 14

# In-memory sliding-window history: (sender_conv, target_conv) -> [ts_seconds].
# Guarded by _rate_lock. Bounded (pruned on every check) so it can't grow.
_peer_msg_history: dict[tuple, list] = {}
_rate_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════
#  Pure cores (unit-testable without a DB / task registry)
# ═══════════════════════════════════════════════════════════════════

def _prune_and_check(timestamps: list, now: float, *,
                     window_s: float = _PEER_MSG_WINDOW_S,
                     max_n: int = _PEER_MSG_MAX_PER_WINDOW) -> tuple:
    """Pure sliding-window rate check.

    Given a list of prior send timestamps (seconds) for ONE ordered
    (sender, target) pair and the current time, return
    ``(allowed: bool, kept: list, retry_after: float)`` where ``kept`` is the
    pruned in-window timestamp list the caller should persist (with ``now``
    appended IFF allowed). ``retry_after`` is the seconds until the oldest
    in-window send ages out (0 when allowed). Never mutates its input.
    """
    cutoff = now - max(0.0, window_s)
    kept = [t for t in (timestamps or []) if t > cutoff]
    if len(kept) < max(1, max_n):
        return True, kept + [now], 0.0
    # At capacity: the oldest in-window send determines when a slot frees.
    oldest = min(kept)
    retry_after = max(0.0, (oldest + window_s) - now)
    return False, kept, retry_after


def _refund_rate_slot(from_conv: str, to_conv: str, ts: float) -> bool:
    """Give back the rate-limit slot recorded by :func:`_rate_gate` for one
    (sender, target) pair — used when the send it was gating FAILED.

    ``_rate_gate`` records ``ts`` at CHECK time (before the fallible enqueue) so
    concurrent/rapid retries correctly see the budget consumed and cannot storm
    a live target. But if the enqueue then RAISES, no message was delivered, so
    a flapping target would otherwise silently drain a sender's window budget.
    This removes exactly ONE occurrence of ``ts`` (the slot that send took),
    restoring the budget without disarming the guard for the racing case.

    Returns True if a slot was removed. Thread-safe (takes ``_rate_lock``).
    """
    key = (from_conv, to_conv)
    with _rate_lock:
        hist = _peer_msg_history.get(key)
        if not hist:
            return False
        try:
            hist.remove(ts)
        except ValueError as _e:
            # The exact timestamp already aged out / was pruned — nothing to
            # refund (the window moved on, which is itself budget relief).
            logger.debug('refund rate slot: unparseable (%s)', _e)
            return False
        if hist:
            _peer_msg_history[key] = hist
        else:
            _peer_msg_history.pop(key, None)
        return True


def _authorize_hard_abort(hard_abort: bool, approved_by: str) -> tuple:
    """Pure gate for the coercive hard-abort path.

    Returns ``(allowed: bool, reason: str)``. A hard abort is authorized ONLY
    when an explicit non-empty ``approved_by`` token is present — this is the
    audit gate that keeps intervention advisory-first. An advisory intervention
    (``hard_abort=False``) is always authorized here (its own rate limit still
    applies at the messaging layer).
    """
    if not hard_abort:
        return True, 'advisory'
    if not (approved_by or '').strip():
        return False, 'hard_abort_requires_approval'
    return True, 'approved'


def _join_peers(peers: list, task_by_conv: dict, claim_by_conv: dict,
                *, exclude_conv: str = '') -> list:
    """Pure JOIN of the three live sources into a peer-status view.

    Args:
        peers: decorated ACTIVE peer dicts from ``presence.snapshot`` (each has
            ``convId``, optional ``agentId``, ``title``, ``phase``,
            ``currentFile``, ``statusLabel``, ``taskId``, ``objective``).
        task_by_conv: ``convId -> {'round': int, 'status': str}`` derived from
            the live task registry (current tool-round + status).
        claim_by_conv: ``convId -> claimed-epic title`` (the board join; shared
            with ``build_brain_summary`` via ``claims_by_conv``).
        exclude_conv: a conversation id to omit (typically the caller's own).

    Returns a list of view dicts (conversation peers and sub-agent peers), each:
    ``{convId, agentId, title, phase, statusLabel, currentFile, round,
    taskStatus, claimedEpic}``. Side-effect-free.
    """
    out = []
    for p in (peers or []):
        if not isinstance(p, dict):
            continue
        conv_id = p.get('convId') or ''
        if not conv_id or (exclude_conv and conv_id == exclude_conv):
            continue
        agent_id = p.get('agentId') or ''
        # Live task round/status is a conversation-level fact; attribute it to
        # conversation peers (a sub-agent peer carries its own phase already).
        live = task_by_conv.get(conv_id, {}) if not agent_id else {}
        out.append({
            'convId': conv_id,
            'agentId': agent_id,
            'title': p.get('title') or p.get('parentTitle') or '',
            'phase': p.get('phase') or '',
            'statusLabel': p.get('statusLabel') or '',
            'currentFile': p.get('currentFile') or '',
            'objective': p.get('objective') or '',
            'round': int(live.get('round', 0) or 0),
            'taskStatus': live.get('status', '') or '',
            'claimedEpic': claim_by_conv.get(conv_id, '') if not agent_id else '',
        })
    return out


# ═══════════════════════════════════════════════════════════════════
#  Live task-registry probe (impure — reads the in-proc registry)
# ═══════════════════════════════════════════════════════════════════

def _live_task_by_conv() -> dict:
    """Map ``convId -> {'round': int, 'status': str}`` for RUNNING tasks.

    Reads the live task registry (the same one ``abort_running_tasks_for_conv``
    consults). ``round`` is the count of tool rounds executed so far (the peer's
    "current round"). Best-effort — returns {} on any failure so the join
    degrades to presence-only.
    """
    out = {}
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            for t in tasks.values():
                if not isinstance(t, dict):
                    continue
                cid = t.get('convId')
                if not cid or t.get('status') != 'running' or t.get('aborted'):
                    continue
                rounds = t.get('toolRounds') or []
                out[cid] = {'round': len(rounds), 'status': 'running'}
    except Exception as e:
        logger.debug('[PeerStatus] live-task probe failed: %s', e)
    return out


def _titles_by_conv(conv_ids) -> dict:
    """Resolve human-readable titles for a set of conversation ids.

    Presence ``announce`` is frequently called with an empty ``convTitle`` (the
    task config often has no title yet), so the presence snapshot's ``title``
    field is blank and the roster / agent-facing status fall back to a bare
    ``conv <id>``. This backfills the REAL stored conversation title from the
    DB. When a conversation has no usable stored title (never titled), it falls
    back to a short snippet of the opening user turn so the label is still
    human-readable — never a bare id.

    Returns ``{convId -> label}`` (only ids that resolved to a non-empty label).
    Best-effort: returns ``{}`` on any DB failure so the caller degrades to the
    presence-supplied title (or the id fallback).
    """
    ids = [c for c in (conv_ids or []) if c]
    if not ids:
        return {}
    out = {}
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        placeholders = ','.join('?' * len(ids))
        rows = db.execute(
            f'SELECT id, title FROM conversations WHERE id IN ({placeholders})',
            tuple(ids)).fetchall()
        missing = []
        for r in rows:
            title = (r['title'] or '').strip()
            if title and title.lower() != 'untitled':
                out[r['id']] = title
            else:
                missing.append(r['id'])
        if missing:
            import json
            from lib.conversations.title_gen import first_user_text
            ph2 = ','.join('?' * len(missing))
            mrows = db.execute(
                f'SELECT id, messages FROM conversations WHERE id IN ({ph2})',
                tuple(missing)).fetchall()
            for r in mrows:
                try:
                    msgs = r['messages']
                    if isinstance(msgs, str):
                        msgs = json.loads(msgs or '[]')
                    snippet = first_user_text(msgs or [], max_chars=60)
                except Exception as _pe:
                    logger.debug('[PeerStatus] first-user snippet failed conv=%s: %s',
                                 (r['id'] or '')[:8], _pe)
                    snippet = ''
                if snippet:
                    out[r['id']] = snippet
    except Exception as e:
        logger.debug('[PeerStatus] title backfill failed: %s', e)
    return out


def build_peer_status(project_path: str, conv_id: str = '') -> dict:
    """Aggregate LIVE peer status for ``project_path`` (the introspection tool).

    Joins presence (active peers + phase/file), the live task registry (current
    round/status) and the board claim map (which epic each peer advances).
    Reuses ``claims_by_conv`` — the SAME join ``build_brain_summary`` uses — so
    the two views can never drift. Returns
    ``{'peers': [...], 'count': int}`` (the caller's own conv is excluded when
    ``conv_id`` is given). Never raises.
    """
    out = {'peers': [], 'count': 0}
    if not project_path:
        return out
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)

    peers = []
    try:
        from lib.presence.registry import snapshot
        peers = snapshot(project_path).get('peers', []) or []
    except Exception as e:
        logger.debug('[PeerStatus] presence read failed proj=%.40r: %s',
                     project_path, e)

    claim_by_conv = {}
    try:
        from lib.conversations.project_board import claims_by_conv, read_board
        claim_by_conv = claims_by_conv(read_board(project_path).get('tasks', []))
    except Exception as e:
        logger.debug('[PeerStatus] board join failed proj=%.40r: %s',
                     project_path, e)

    task_by_conv = _live_task_by_conv()
    view = _join_peers(peers, task_by_conv, claim_by_conv, exclude_conv=conv_id)

    # Backfill a human-readable title for any peer whose presence-supplied title
    # is empty (announce is usually called with no convTitle) so neither the
    # Team panel nor the agent-facing status shows a bare `conv <id>`.
    need_titles = [p['convId'] for p in view if not p.get('title') and p.get('convId')]
    if need_titles:
        titles = _titles_by_conv(need_titles)
        for p in view:
            if not p.get('title'):
                resolved = titles.get(p['convId'])
                if resolved:
                    p['title'] = resolved

    out['peers'] = view
    out['count'] = len(view)
    # Backend-authoritative CONVERSATION count for the Team-panel headline/badge.
    # A running conversation's sub-agents are separate presence peers (keyed
    # convId#agentId) and must NOT inflate a "conversations here" count, or the
    # Team panel would report e.g. 5 while the sidebar shows 3. Uses the SAME
    # rule build_brain_summary applies for activePeers (dedup on convId, exclude
    # agentId) so the collab bar and the Team panel can never drift. The full
    # peer list (incl. sub-agents) is still returned + rendered as cards.
    out['convCount'] = len({p.get('convId') for p in view
                            if p.get('convId') and not p.get('agentId')})
    return out


# ═══════════════════════════════════════════════════════════════════
#  Peer messaging (agent-initiated, rate-limited, advisory)
# ═══════════════════════════════════════════════════════════════════

def _rate_gate(from_conv: str, to_conv: str, now: float) -> tuple:
    """Apply the per-(sender,target) sliding-window rate limit under the lock.

    Returns ``(allowed: bool, retry_after: float)``. On allow, records ``now``
    as a send timestamp so the window advances. This is the storm guard: a
    caller that is over its window budget is refused.
    """
    key = (from_conv, to_conv)
    with _rate_lock:
        allowed, kept, retry_after = _prune_and_check(
            _peer_msg_history.get(key, []), now)
        _peer_msg_history[key] = kept
        return allowed, retry_after


def _resolve_target_conv_id(to_conv_id: str) -> tuple:
    """Resolve a possibly-truncated target conv id to its canonical FULL id.

    The peer tools surface conversation ids in an 8-char display form
    (``project_peer_status`` prints ``[{convId[:8]}]``), and an agent copies
    that short id verbatim into ``project_message`` / ``project_intervene``.
    But the message queue (``enqueue_message`` → ``dequeue_next``) and the task
    registry key on the FULL 14-char id, so enqueuing under a truncated id
    lands in a PHANTOM queue that no conversation ever drains — the message is
    silently lost and the short id registers as an orphaned-dispatchable conv
    that maps to nothing. Resolve to the canonical id by exact match, else by
    UNIQUE prefix.

    Returns ``(full_id, '')`` on success, or ``('', reason)`` where reason is
    ``'unknown_target'`` (no conversation matches) or ``'ambiguous_target'``
    (the prefix matches >1 conversation — refuse rather than mis-deliver). On a
    DB error a FULL-length id is passed through unchanged (already canonical —
    a transient blip must not drop a valid send), but a sub-length (truncated /
    prefix) id fails CLOSED with ``'resolve_failed'``: enqueuing a truncated id
    is exactly the phantom-queue silent loss this function exists to prevent.
    """
    tid = (to_conv_id or '').strip()
    if not tid:
        return '', 'unknown_target'
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT id FROM conversations WHERE id=? LIMIT 1', (tid,)).fetchone()
        if row:
            return row['id'], ''
        rows = db.execute(
            'SELECT id FROM conversations WHERE id LIKE ? LIMIT 2',
            (tid + '%',)).fetchall()
        if len(rows) == 1:
            return rows[0]['id'], ''
        if len(rows) > 1:
            logger.info('[PeerMsg] target id/prefix %s is AMBIGUOUS (%d matches)',
                        tid[:12], len(rows))
            return '', 'ambiguous_target'
        logger.info('[PeerMsg] target id/prefix %s matches no conversation',
                    tid[:12])
        return '', 'unknown_target'
    except Exception as e:
        logger.warning('[PeerMsg] target-id resolve failed for %s: %s',
                       tid[:12], e)
        # Fail-CLOSED for a truncated/prefix id: enqueuing it would land in a
        # phantom queue key no conversation drains (silent loss) — the exact
        # thing this function exists to prevent. A full-length id is already
        # canonical, so passing it through on a transient DB blip is harmless
        # (the subsequent enqueue surfaces any real DB fault). _FULL_CONV_ID_LEN
        # is the registry-key width the drain path matches on.
        if len(tid) < _FULL_CONV_ID_LEN:
            return '', 'resolve_failed'
        return tid, ''


def render_peer_protocol_block(project_path: str) -> str:
    """The ambient ``[PEER MESSAGING PROTOCOL]`` system-prompt block.

    Injected in project mode (system_context.py §4.47) so the model has AMBIENT
    guidance that ``project_message`` / ``project_intervene`` write to ANOTHER
    AGENT — mirroring the ``[PROJECT BOARD]`` block's imperative style. Without
    this the model's only steer was the tool description, which biased it to
    narrate status for a human reader (Pillar #6 Symptom-C).

    Returns '' outside project mode (no ``project_path``) so it splices to
    nothing.
    """
    if not project_path:
        return ''
    return (
        '[PEER MESSAGING PROTOCOL]\n'
        'project_message / project_intervene write to ANOTHER AGENT (a sibling '
        'conversation of this project), NOT to a human. Use the imperative '
        'agent-to-agent coordination register — the four coordination acts:\n'
        '  \u2022 CLAIM work you are taking ("taking the parser refactor, stand '
        'down on lib/parser/").\n'
        '  \u2022 CONFIRM a boundary before you cross it ("are you touching '
        'styles.css? I am about to rewrite it").\n'
        '  \u2022 HAND OFF context a peer needs ("the schema bump you needed is '
        'done on the shared tree").\n'
        '  \u2022 WARN of an OVERLAP ("your epic duplicates the one I already '
        'own — re-check the board").\n'
        'Do NOT send status updates, progress narration, or FYI notes as if '
        'reporting to a human — a peer message that does not change what the '
        'peer DOES is wasted budget. The peer sees your message on its NEXT turn '
        '(never mid-stream) and acts autonomously; it '
        'decides how to act.\n'
        'When you RECEIVE a peer message (it arrives as a turn prefixed '
        '"[Peer message from a sibling conversation …]" and carries a reply '
        'id), you MAY reply EXACTLY ONCE via '
        'project_message(to_conv_id=<that reply id>) — but only to CONFIRM a '
        'boundary, HAND OFF context, or DECLINE. If a reply would not change '
        'what the peer does, do NOT acknowledge for its own sake: incorporate '
        'the message and keep working. Your rate-limit budget is the ceiling '
        '— this is coordination, not a chat channel, so never reply just to '
        'be polite.'
    )


def _live_drain_eligible_task(conv_id: str) -> str:
    """Return the task_id of a LIVE, round-boundary-DRAINABLE task for ``conv_id``
    — or ``''`` if the target has none.

    The Pillar #6 fast-path lane is offered ONLY to an ordinary orchestrator
    turn that drains its inbox at each round boundary. It is deliberately NOT
    offered to:
      * a conv with no live task (idle → the durable queue row + brain
        idle-drain own delivery);
      * an ``aborted`` task (it is winding down, will not drain another round);
      * an endpoint (planner/critic/worker) or VU sub-task turn — those loops do
        NOT run the standard round-boundary inbox drain, so an inbox twin would
        strand. They keep the durable queue-lane (cycle-end) delivery.

    Mirrors the eligibility shape ``drain_idle_peer_messages`` inspects
    (running + not aborted), plus the endpoint/VU exclusion the fast path needs.
    Best-effort: returns '' on any probe error (degrade to queue-lane only).
    """
    if not conv_id:
        return ''
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            for t in tasks.values():
                # Match on EITHER convId OR _peer_drain_key — a VU sub-task runs
                # with convId='' and carries the parent conv in _peer_drain_key
                # (the key its twin is enqueued under). Endpoint/VU loops are no
                # longer excluded: they gained iteration-boundary drain hooks
                # (drain_peer_messages_into), so the fast-path twin is delivered.
                if (t.get('convId') != conv_id
                        and t.get('_peer_drain_key') != conv_id):
                    continue
                if t.get('status') != 'running' or t.get('aborted'):
                    continue
                tid = t.get('id')
                if tid:
                    return tid
    except Exception as e:
        logger.debug('[PeerMsg] live-task eligibility probe failed for conv=%s: %s',
                     conv_id[:8], e)
    return ''


def send_peer_message(project_path: str, from_conv_id: str, to_conv_id: str,
                      text: str, *, config: dict | None = None,
                      _kind_label: str = 'note', human: bool = False) -> dict:
    """Send an advisory message to a sibling conversation.

    The message is enqueued as a ``KIND_PEER_MSG`` turn source into the target's
    queue — the target sees it on its NEXT turn and NEVER mid-stream — and one
    ``note`` event is mirrored into the activity feed, stamped with the sender.
    Rate-limited per (sender, target). Self-send is refused. Best-effort; never
    raises.

    Args:
        human: when True the message is a HUMAN operator's nudge sent from the
            Team panel (rather than an agent-initiated peer note). The turn the
            target sees is framed as an operator instruction (a human directive,
            not a peer's advisory opinion), the payload carries ``_peerHuman``
            so the receiving banner attributes it to the operator, and the feed
            row is stamped ``operator`` for provenance. The SAME rate-limit +
            self-send guards apply — the human path is not a storm bypass.

    Returns ``{'ok', 'queueId'?, 'error'?, 'retryAfter'?}``.
    """
    text = (text or '').strip()
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not from_conv_id or not to_conv_id:
        return {'ok': False, 'error': 'missing sender/target'}
    # Resolve the (possibly truncated) target id to its canonical FULL id
    # BEFORE the self-check, rate-gate, enqueue and feed emit — all of which
    # key on the id. Refuse on ambiguity / no-match rather than enqueuing under
    # a phantom queue key the real conversation never drains.
    to_conv_id, _resolve_err = _resolve_target_conv_id(to_conv_id)
    if _resolve_err:
        logger.info('[PeerMsg] refused %s→%s: %s', from_conv_id[:8],
                    (to_conv_id or '?')[:8], _resolve_err)
        return {'ok': False, 'error': _resolve_err}
    if from_conv_id == to_conv_id:
        return {'ok': False, 'error': 'cannot_message_self'}
    if not text:
        return {'ok': False, 'error': 'empty message'}
    text = text[:_PEER_MSG_MAX_CHARS]
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)

    now = time.time()
    allowed, retry_after = _rate_gate(from_conv_id, to_conv_id, now)
    if not allowed:
        logger.info('[PeerMsg] rate-limited %s→%s (retry in %.0fs)',
                    from_conv_id[:8], to_conv_id[:8], retry_after)
        return {'ok': False, 'error': 'rate_limited',
                'retryAfter': round(retry_after, 1)}

    # The turn content the target sees next turn. It is PLAIN content — it
    # triggers no send path (the no-auto-relay guarantee). A HUMAN operator
    # nudge is framed as a directive (the human is the authority); an agent
    # peer note is explicitly advisory ("weigh it and act as you see fit").
    if human:
        body = (f'[Message from the project operator, relayed via a sibling '
                f'conversation (conv {from_conv_id[:8]})]\n\n{text}\n\n'
                f'(This is a note the human operator sent to this conversation '
                f'from the project Team panel. Treat it as operator guidance.)')
    else:
        body = (f'[Peer message from a sibling conversation of this project '
                f'(conv {from_conv_id[:8]}, reply id {from_conv_id})]\n\n{text}\n\n'
                f'(This is an advisory note from a peer conversation, not a human '
                f'instruction. Weigh it and act as you see fit.)')
    payload = {'text': body, '_peerMessage': True, '_fromConv': from_conv_id}
    if human:
        payload['_peerHuman'] = True
    try:
        from lib.message_queue import KIND_PEER_MSG, enqueue_message
        res = enqueue_message(
            to_conv_id, payload, config or {}, kind=KIND_PEER_MSG)
    except Exception as e:
        # The send FAILED — no message was delivered, so refund the rate-limit
        # slot _rate_gate consumed at check time. Otherwise a flapping (always-
        # raising) target would silently drain the sender's per-window budget
        # for messages that never landed. The refund keeps the storm guard
        # intact for the concurrent case (the slot WAS held during the attempt)
        # while not penalising the sender for the target's failure.
        _refund_rate_slot(from_conv_id, to_conv_id, now)
        logger.error('[PeerMsg] enqueue failed %s→%s (rate slot refunded): %s',
                     from_conv_id[:8], to_conv_id[:8], e, exc_info=True)
        return {'ok': False, 'error': str(e)}

    queue_id = res.get('queueId')

    # ── Fast-path lane (Pillar #6 round-boundary accelerator) ──
    # When the target has a LIVE, drain-eligible orchestrator turn, ALSO enqueue
    # an agent_inbox twin tagged with the durable row's queueId. The orchestrator
    # injects it at the NEXT round boundary (never mid-stream); its de-dup hooks
    # (forward: dedup_peer_durable_rows; reverse: consume_peer) guarantee the
    # message is delivered EXACTLY ONCE across the two lanes. An idle / endpoint /
    # VU target gets no twin — the durable row + brain idle-drain own delivery
    # there. Best-effort: a twin failure only means the message waits for the
    # queue-lane (completion-hook / idle-drain) delivery — never a loss.
    if queue_id and _live_drain_eligible_task(to_conv_id):
        # The inbox is keyed by ``swarm_key_for(task)`` = the CONV id when
        # present (orchestrator.py:1874 drains _drain_inbox(swarm_key_for(task))),
        # NOT the task id — enqueue under the target conv so the live turn's
        # round-boundary drain finds it.
        try:
            from lib import agent_inbox
            agent_inbox.enqueue(
                to_conv_id, body, priority='next', mode='peer-msg',
                extra={'queueId': queue_id, 'fromConv': from_conv_id})
            logger.info('[PeerMsg] fast-path inbox twin queued for live conv %s '
                        '(queueId=%s)', to_conv_id[:8], (queue_id or '?')[:8])
        except Exception as e:
            logger.warning('[PeerMsg] fast-path inbox twin failed for conv=%s '
                           '(queue lane still delivers): %s',
                           to_conv_id[:8], e)

    # Mirror ONE auditable note into the feed, stamped with the sender. A human
    # operator nudge is stamped 'operator' so the Team thread + feed can render
    # it distinctly from an agent peer note.
    feed_kind = 'operator' if human else _kind_label
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path, from_conv_id, 'note',
            f'{feed_kind} → conv {to_conv_id[:8]}: {text}',
            payload={'toConv': to_conv_id, 'fromConv': from_conv_id,
                     'kind': feed_kind, 'human': bool(human)})
    except Exception as e:
        logger.debug('[PeerMsg] feed mirror skipped (message enqueued): %s', e)

    audit_log('peer_message', project_path=project_path,
              from_conv=from_conv_id, to_conv=to_conv_id, chars=len(text),
              human=bool(human))
    logger.info('[PeerMsg] %s→%s queued=%s (%d chars)', from_conv_id[:8],
                to_conv_id[:8], (res.get('queueId') or '?')[:8], len(text))

    # ── Event-channel idle drain (no 30 s heartbeat wait) ──
    # An IDLE target used to wait for the brain 30 s tick's
    # drain_idle_peer_messages pass to render this note as a turn. Drain it
    # NOW via the SAME dispatch_next_queued seam, gated on the SAME live-task
    # predicate that pass mirrors (a conv whose only live task is aborted /
    # winding down IS drained — the strand-closing behaviour). A LIVE target
    # keeps the fast-path twin + completion-hook delivery above. Best-effort:
    # a drain failure leaves delivery to the heartbeat pass — never a loss.
    if queue_id:
        try:
            if not _live_drain_eligible_task(to_conv_id):
                from lib.message_queue import dispatch_next_queued
                _tid = dispatch_next_queued(to_conv_id)
                if _tid:
                    logger.info('[PeerMsg] idle target %s drained at send time '
                                '→ task %s (no 30s heartbeat wait)',
                                to_conv_id[:8], _tid[:8])
        except Exception as e:
            logger.warning('[PeerMsg] send-time idle drain failed conv=%s '
                           '(heartbeat owns delivery): %s',
                           to_conv_id[:8], e)
    return {'ok': True, 'queueId': res.get('queueId')}


# ═══════════════════════════════════════════════════════════════════
#  Intervention (advisory-first; hard abort audit-gated)
# ═══════════════════════════════════════════════════════════════════

def intervene_peer(project_path: str, from_conv_id: str, to_conv_id: str,
                   message: str, *, hard_abort: bool = False,
                   approved_by: str = '', approval_fn=None,
                   config: dict | None = None) -> dict:
    """Intervene in a sibling conversation — advisory by default.

    Advisory (``hard_abort=False``, the default): sends a high-priority advisory
    note (via :func:`send_peer_message`) the target sees next turn. This is the
    coordination nudge ("stop, you're duplicating epic X / re-read the board").

    Hard abort (``hard_abort=True``): a genuine abort of the target's running
    task. AUDIT-GATED — the coercive action requires human approval:

      • If a non-empty ``approved_by`` token is already supplied, it is honored.
      • Else, if an ``approval_fn`` is injected, it is CALLED to REQUEST human
        approval (this is what makes the coercive half reachable end-to-end —
        the handler wires ``approval_fn`` to the ``request_human_guidance`` UI
        seam). ``approval_fn(prompt: str)`` blocks until the human decides and
        returns the APPROVER identity (truthy) on grant, or a falsy value on
        deny. Grant → ``approved_by`` is stamped and the abort proceeds. Deny →
        ``denied_by_human`` (stays non-coercive).
      • Else (no token, no approval mechanism — e.g. a headless caller with no
        human) → refused via the pure gate.

    When authorized, emits ``audit_log('intervention', ...)`` and calls
    ``abort_running_tasks_for_conv`` (aborts the TASK only — never touches the
    host/process). Never a silent kill.

    Returns ``{'ok', 'mode', 'aborted'?, 'error'?, ...}``.
    """
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not from_conv_id or not to_conv_id:
        return {'ok': False, 'error': 'missing sender/target'}
    # Resolve the (possibly truncated) target id to its canonical FULL id so the
    # self-check AND the hard-abort registry lookup (abort_running_tasks_for_conv
    # keys on the full id) operate on the real conversation. The advisory path
    # re-resolves inside send_peer_message (idempotent on an already-full id).
    to_conv_id, _resolve_err = _resolve_target_conv_id(to_conv_id)
    if _resolve_err:
        logger.info('[Intervene] refused %s→%s: %s', from_conv_id[:8],
                    (to_conv_id or '?')[:8], _resolve_err)
        return {'ok': False, 'error': _resolve_err}
    if from_conv_id == to_conv_id:
        return {'ok': False, 'error': 'cannot_intervene_self'}

    # ── Reachability seam: request human approval when a coercive abort is
    #    asked for with no pre-supplied token. This is the ONLY place the
    #    approval token can be minted at runtime — without it hard_abort was
    #    unreachable dead code. The pure _authorize_hard_abort below is still
    #    the final arbiter (so the audit gate can't be bypassed). ──
    if hard_abort and not (approved_by or '').strip() and approval_fn is not None:
        prompt = (f'Conversation {from_conv_id[:8]} requests a HARD ABORT of '
                  f'conversation {to_conv_id[:8]}\'s running task(s). Approve? '
                  f'This stops the task only — it never touches the host process.')
        approver = None
        try:
            approver = approval_fn(prompt)
        except Exception as e:
            logger.warning('[Intervene] approval_fn raised %s→%s: %s',
                           from_conv_id[:8], to_conv_id[:8], e)
        if approver:
            approved_by = str(approver).strip()
        else:
            logger.info('[Intervene] hard-abort DENIED by human %s→%s',
                        from_conv_id[:8], to_conv_id[:8])
            return {'ok': False, 'mode': 'hard_abort', 'error': 'denied_by_human'}

    allowed, reason = _authorize_hard_abort(hard_abort, approved_by)
    if not allowed:
        logger.info('[Intervene] hard-abort refused %s→%s: %s',
                    from_conv_id[:8], to_conv_id[:8], reason)
        return {'ok': False, 'mode': 'hard_abort', 'error': reason}

    if not hard_abort:
        # Advisory path: a high-priority peer note. Reuses the rate-limited
        # messaging seam so an intervention storm is equally impossible.
        note = message or ('Heads-up: a sibling conversation believes your current '
                           'work may overlap or duplicate an epic in progress — '
                           're-check the project board and reconcile. Advisory: keep '
                           'making progress; you decide how to reconcile.')
        res = send_peer_message(project_path, from_conv_id, to_conv_id, note,
                                config=config, _kind_label='intervention')
        res['mode'] = 'advisory'
        return res

    # ── Coercive path (authorized) — audit FIRST, then abort the task only. ──
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    audit_log('intervention', project_path=project_path,
              from_conv=from_conv_id, to_conv=to_conv_id,
              approved_by=approved_by, hard_abort=True)
    try:
        from lib.tasks_pkg.manager import abort_running_tasks_for_conv
        n = abort_running_tasks_for_conv(to_conv_id)
    except Exception as e:
        logger.error('[Intervene] abort failed %s→%s: %s',
                     from_conv_id[:8], to_conv_id[:8], e, exc_info=True)
        return {'ok': False, 'mode': 'hard_abort', 'error': str(e)}
    # Leave an auditable trail in the feed too.
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path, from_conv_id, 'note',
            f'intervention (hard abort, approved_by={approved_by}) → conv '
            f'{to_conv_id[:8]}: aborted {n} running task(s)',
            payload={'toConv': to_conv_id, 'fromConv': from_conv_id,
                     'kind': 'hard_abort', 'aborted': n,
                     'approvedBy': approved_by})
    except Exception as e:
        logger.debug('[Intervene] feed mirror skipped (abort done): %s', e)
    logger.info('[Intervene] HARD ABORT %s→%s aborted=%d approved_by=%s',
                from_conv_id[:8], to_conv_id[:8], n, approved_by)
    return {'ok': True, 'mode': 'hard_abort', 'aborted': n}


# ═══════════════════════════════════════════════════════════════════
#  Agent tool dispatch
# ═══════════════════════════════════════════════════════════════════

def _fmt_feed(events: list, current_conv_id: str = '') -> str:
    """Format a recent-activity slice for the ``project_feed_read`` tool.

    Renders the feed newest-first as one line per event (kind · who · summary),
    marking the caller's own conversation. Pure — no DB / no side effects.
    """
    if not events:
        return ('No recent cross-conversation activity in this project. (The '
                'feed is a live pulse of what sibling conversations have been '
                'doing — task starts/completions, board claims, decisions, and '
                'peer notes. Nothing has happened recently.)')
    lines = [f'Recent project activity — {len(events)} event(s), newest first:']
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = e.get('kind', 'note') or 'note'
        conv = (e.get('conv_id') or '')
        who = e.get('title') or (f'conv {conv[:8]}' if conv else 'unknown')
        mine = ' (this conversation)' if conv and conv == current_conv_id else ''
        summary = (e.get('summary') or '').strip()
        row = f'  • [{kind}] {who}{mine}'
        if summary:
            row += f' — {summary}'
        lines.append(row)
    return '\n'.join(lines)


def _fmt_peer_status(status: dict, current_conv_id: str) -> str:
    peers = status.get('peers', [])
    if not peers:
        return ('No other conversations of this project are active right now. '
                '(Peer status is LIVE — it reflects who is running this instant, '
                'not conversation history.)')
    lines = [f'Live peer status — {len(peers)} active peer(s) in this project:']
    for p in peers:
        who = p.get('title') or f'conv {p.get("convId", "")[:8]}'
        if p.get('agentId'):
            who = f'sub-agent {p["agentId"]} (in {who})'
        bits = []
        if p.get('statusLabel'):
            bits.append(p['statusLabel'])
        if p.get('round'):
            bits.append(f'round {p["round"]}')
        if p.get('currentFile'):
            bits.append(f'editing {p["currentFile"]}')
        if p.get('claimedEpic'):
            bits.append(f'advancing «{p["claimedEpic"]}»')
        detail = '; '.join(bits) if bits else 'active'
        lines.append(f'  • [{p.get("convId","")[:8]}] {who} — {detail}')
    return '\n'.join(lines)


def execute_peer_tool(fn_name: str, fn_args: dict, *,
                      current_conv_id: str = '', project_path: str = '',
                      config: dict | None = None, approval_fn=None) -> str:
    """Execute a peer-collaboration agent tool → human-readable string.

    Tools: ``project_peer_status`` (read live peers), ``project_feed_read``
    (read the recent cross-conversation activity feed), ``project_message``
    (advisory messaging), ``project_intervene`` (advisory nudge; hard abort
    REQUESTS human approval via ``approval_fn`` — the handler wires it to the
    ``request_human_guidance`` UI seam). All project-scoped — refuse outside
    project mode.

    Args:
        approval_fn: optional callable ``(prompt: str) -> approver | None`` that
            requests human approval for a coercive hard abort (grant → approver
            identity; deny → falsy). Injected by the handler; absent for a
            headless caller (hard abort then refuses via the pure gate).
    """
    try:
        if not project_path:
            return ('Error: peer collaboration tools are only available in '
                    'project mode (open a project first).')
        if fn_name == 'project_peer_status':
            target = (fn_args.get('conv_id') or '').strip()
            status = build_peer_status(project_path, current_conv_id)
            if target:
                status['peers'] = [p for p in status['peers']
                                   if p.get('convId', '').startswith(target)
                                   or p.get('convId') == target]
                status['count'] = len(status['peers'])
            return _fmt_peer_status(status, current_conv_id)

        if fn_name == 'project_feed_read':
            # On-demand read of the cross-conversation activity feed. Kept as a
            # TOOL (not always-on prompt injection) deliberately: the feed can
            # hold up to _PROJECT_EVENTS_KEEP events and CHANGES every turn a
            # sibling acts — injecting it each turn would bloat context AND bust
            # the append-only prompt-cache prefix. The small, stable board +
            # charter summaries are injected; the chronological pulse is pulled.
            try:
                limit = int(fn_args.get('limit') or 25)
            except (TypeError, ValueError) as e:
                logger.debug('[Peer] feed limit parse failed, defaulting: %s', e)
                limit = 25
            limit = max(1, min(limit, 60))
            from lib.conversations.project_feed import read_project_feed
            feed = read_project_feed(project_path, limit=limit)
            return _fmt_feed(feed.get('events', []), current_conv_id)

        if fn_name == 'project_message':
            to_conv = (fn_args.get('to_conv_id') or '').strip()
            text = (fn_args.get('text') or '').strip()
            if not to_conv:
                return 'Error: to_conv_id is required.'
            if not text:
                return 'Error: text is required.'
            res = send_peer_message(project_path, current_conv_id, to_conv, text,
                                    config=config)
            if res.get('ok'):
                return (f'Message delivered to conversation {to_conv[:8]} — it '
                        f'will see your note on its next turn (peer messages '
                        f'never interrupt a live turn). This is advisory; the '
                        f'peer decides whether to act on it.')
            if res.get('error') == 'rate_limited':
                return (f'Not sent — you have reached the peer-message rate limit '
                        f'for conversation {to_conv[:8]} (retry in '
                        f'~{res.get("retryAfter", "?")}s). This guards against '
                        f'message storms; wait before sending again.')
            if res.get('error') == 'cannot_message_self':
                return 'Error: a conversation cannot message itself.'
            if res.get('error') == 'unknown_target':
                return (f'Error: no conversation in this project matches '
                        f'"{to_conv}". Check the id via project_peer_status '
                        f'(use the id shown in [brackets]).')
            if res.get('error') == 'ambiguous_target':
                return (f'Error: "{to_conv}" matches more than one conversation '
                        f'— it is too short to be unambiguous. Supply more '
                        f'characters of the target conversation id.')
            return f'Error sending peer message: {res.get("error", "unknown")}.'

        if fn_name == 'project_intervene':
            to_conv = (fn_args.get('to_conv_id') or '').strip()
            message = (fn_args.get('message') or '').strip()
            hard_abort = bool(fn_args.get('hard_abort'))
            if not to_conv:
                return 'Error: to_conv_id is required.'
            # The agent surface can REQUEST a hard abort, but the coercive
            # action is human-gated: intervene_peer calls approval_fn to REQUEST
            # a human decision (grant → run; deny → advisory). A pre-supplied
            # cfg token (e.g. an already-authorized headless caller) is honored.
            approved_by = str((config or {}).get('interventionApprovedBy') or '').strip()
            res = intervene_peer(project_path, current_conv_id, to_conv, message,
                                 hard_abort=hard_abort, approved_by=approved_by,
                                 approval_fn=approval_fn if hard_abort else None,
                                 config=config)
            if res.get('ok'):
                if res.get('mode') == 'hard_abort':
                    return (f'Hard intervention executed (human-approved) — aborted '
                            f'{res.get("aborted", 0)} running task(s) in '
                            f'conversation {to_conv[:8]}. The abort stops the '
                            f'task only; it never touches the host process.')
                return (f'Advisory intervention sent to conversation {to_conv[:8]} '
                        f'— it will see the notice on its next turn. This is a '
                        f'nudge, not a kill; the peer decides how to respond.')
            if res.get('error') == 'denied_by_human':
                return (f'Hard abort of conversation {to_conv[:8]} was DENIED by '
                        f'the user. The peer was not stopped. Consider sending an '
                        f'advisory intervention instead (omit hard_abort).')
            if res.get('error') == 'hard_abort_requires_approval':
                return ('A hard abort of another conversation\'s running task '
                        'requires explicit human approval, and no approval '
                        'mechanism is available in this context (e.g. a headless '
                        'run with no human). Send an advisory intervention '
                        'instead (omit hard_abort).')
            if res.get('error') == 'rate_limited':
                return (f'Not sent — intervention rate limit reached for '
                        f'conversation {to_conv[:8]} (retry in '
                        f'~{res.get("retryAfter", "?")}s).')
            if res.get('error') == 'unknown_target':
                return (f'Error: no conversation in this project matches '
                        f'"{to_conv}". Check the id via project_peer_status.')
            if res.get('error') == 'ambiguous_target':
                return (f'Error: "{to_conv}" matches more than one conversation '
                        f'— supply more characters of the target id.')
            return f'Error intervening: {res.get("error", "unknown")}.'

        return f"Error: Unknown peer tool '{fn_name}'"
    except Exception as e:
        logger.warning('[PeerTool] %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'build_peer_status', 'send_peer_message', 'intervene_peer',
    'execute_peer_tool', '_join_peers', '_prune_and_check',
    '_authorize_hard_abort', '_fmt_feed', '_PEER_MSG_MAX_PER_WINDOW',
    '_PEER_MSG_WINDOW_S',
]
