"""lib.conversations.project_attention — the "needs you" single source of truth.

Every Project Brain surface can tell the human something. Only a FEW of them
are actually waiting ON the human, and before this module they were scattered
across four tabs with no aggregate anywhere: an epic blocked on a structured
question lived only in the Board tab, charter proposals only in the Charter
tab, conflict advisories only in the collab bar's detail lines. The one
question the operator asks at a glance — *"is anything waiting on me?"* — had
no single answer.

This module computes that answer ONCE, backend-side, as a priority-ordered
list of typed items. The collaboration bar, the presence strip and the panel's
attention tab all render THIS list, so a count on the bar and a card in the
panel can never disagree (the same argument ``read_board`` makes for its own
counts).

Severity — deliberately only two (see docs/PROJECT_BRAIN_ATTENTION_REDESIGN.md
§D2). The only decision an operator makes from a glance is "do I have to get
up?"; grades between "urgent" and "very urgent" are noise:

  ``blocking``  work is STOPPED and only a human can restart it.
  ``advisory``  progress continues; a human may improve the outcome.

What is deliberately NOT an attention item:

  • **A cooldown block** (``blocked_until`` in the future, no question).
    It expires at read time and ``select_dispatchable`` re-picks the epic with
    zero human involvement — listing it would train the operator to ignore the
    surface. It is reported as the ``waiting`` count instead, which the UI
    renders as reassurance ("nothing needs you; N are waiting on their own
    gates"), never as a task.
  • **Watch items** — the human's own standing concerns (their outbox, not
    their inbox). They live in the Status tab.
  • **A peer hard-abort approval** — a LIVE, synchronous
    ``request_human_guidance`` prompt, not durable state; there is nothing to
    enumerate after the fact.

Note on ``[human-gated]``: that string is NEVER matched anywhere in the
codebase — ``project_board.py`` only tests for ``[sibling]`` and treats
"human" as the else branch, and the class affects only the backoff curve.
So this module keys "a human must act" on ``block_question`` presence, which
is the field ``project_dispatch`` actually honours when it skips an epic, and
treats the reason prefix as display text only.

Design invariants (shared with every Project Brain surface):
  • **Keyed strictly on ``project_path``** — never a process-global singleton.
  • **Best-effort** — any sub-read failing degrades that source to empty; the
    function never raises into its (read-route) caller.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# Severity ranks — lower sorts first. Frozen: the frontend orders by the
# server's list order, it does not re-sort.
_SEVERITY_RANK = {'blocking': 0, 'advisory': 1}

# Per-type tiebreak within a severity, so the list order is deterministic
# rather than dependent on which sub-read finished first.
_TYPE_RANK = {
    'board_question': 0,
    'conflict': 1,
    'charter_proposal': 2,
}

# A conflict message is a fully-formed backend string; cap it so one pathological
# advisory can't dominate the panel. DISPLAY-ONLY fields: never apply it to a
# field a resolving control submits back (see _charter_proposals).
_TEXT_MAX = 600


def _empty_attention(project_path: str = '') -> dict:
    return {
        'projectPath': project_path,
        'items': [],
        'blocking': 0,
        'advisory': 0,
        'needsYou': 0,
        'waiting': 0,
    }


def _board_questions(project_path: str) -> list[dict]:
    """Epics halted on a structured human question — the ONLY item type that
    stops a workstream indefinitely.

    ``project_dispatch`` skips an epic whose ``block_question`` is set and
    whose ``human_answer`` is empty on EVERY heartbeat, so unlike a cooldown
    block this never resolves on its own. Uses the same partition predicate as
    ``render_board_block``'s pending-question lane and the frontend's awaiting
    lane, so the three can never drift.
    """
    from lib.conversations.project_board import read_board
    out = []
    board = read_board(project_path)
    for t in board.get('tasks', []) or []:
        if t.get('kind') == 'lease':
            continue
        if t.get('status') != 'open':
            continue
        q = t.get('block_question')
        if not q or (t.get('human_answer') or '').strip():
            continue
        out.append({
            'type': 'board_question',
            'severity': 'blocking',
            'id': t.get('id', ''),
            'title': (t.get('title') or '')[:_TEXT_MAX],
            'question': str(q.get('q') or '')[:_TEXT_MAX],
            'options': q.get('options') if isinstance(q.get('options'), list) else [],
            'reason': (t.get('block_reason') or '')[:_TEXT_MAX],
            'blockCount': int(t.get('block_count') or 0),
            'ownerConvId': t.get('owner_conv_id') or '',
            'ts': int(t.get('updated_at') or 0),
            # Where the resolving control lives, for the panel's deep-link.
            'tab': 'board',
        })
    return out


def _charter_proposals(project_path: str) -> list[dict]:
    """Charter amendments proposed but not yet committed or dismissed.

    ADVISORY, not blocking: since the 2026-07-12 de-gating an agent commits its
    own decisions via ``project_charter_commit``, and a proposal is explicitly
    "only for suggestions you are not yet ready to make binding". Nothing stops
    while one is pending — which is exactly why it must NOT drive the bar's
    emphasis the way it used to.

    ``text`` is NOT capped at ``_TEXT_MAX``: unlike a conflict advisory (pure
    display), this field is what the Needs-you tab COMMITS as the durable
    decision, so a display cap here would store a decision truncated
    mid-sentence. ``pending_proposals`` already bounds it at
    ``_DECISION_MAX_CHARS`` — the same bound the commit route applies.
    """
    from lib.conversations.project_charter import pending_proposals
    return [{
        'type': 'charter_proposal',
        'severity': 'advisory',
        'id': p.get('proposalId', ''),
        'title': (p.get('title') or '')[:_TEXT_MAX],
        'text': p.get('summary') or '',
        'convId': p.get('conv_id', ''),
        'ts': int(p.get('ts') or 0),
        'tab': 'charter',
    } for p in pending_proposals(project_path)]


def _conflicts(project_path: str) -> list[dict]:
    """Live file-set overlaps between two active conversations.

    Recomputed from the SAME ``detect_overlaps`` judgment the live conflict
    broadcast uses — no second mirror, no stored state. Advisory: both
    conversations keep running; the human decides whether to intervene.
    """
    from lib.presence.conflict import detect_overlaps
    from lib.presence.registry import snapshot
    peers = snapshot(project_path).get('peers', []) or []
    if not peers:
        return []
    out = []
    # detect_overlaps returns {'path', 'peers': [composite keys], 'message'} —
    # the message is backend-formed and rendered VERBATIM (the frontend never
    # composes conflict text). The contended path is the natural stable id.
    for a in detect_overlaps(peers):
        msg = (a.get('message') or '').strip()
        path = a.get('path') or ''
        if not msg:
            continue
        # A peer key is 'convId' or 'convId#agentId' — project the conversation
        # half so a caller can mark "this involves the conv I'm looking at".
        keys = a.get('peers', []) or []
        conv_ids = []
        for k in keys:
            cid = str(k).split('#', 1)[0]
            if cid and cid not in conv_ids:
                conv_ids.append(cid)
        out.append({
            'type': 'conflict',
            'severity': 'advisory',
            'id': path or msg[:64],
            'path': path,
            'text': msg[:_TEXT_MAX],
            'convIds': conv_ids,
            'peers': keys,
            # detect_overlaps carries no timestamp (it is recomputed live, not
            # stored) — 0 sorts it last within its type, which is right: a
            # conflict is defined by its path, not its age.
            'ts': 0,
            'tab': 'peers',
        })
    return out


def _waiting_count(project_path: str) -> int:
    """Epics on a self-expiring cooldown — informational reassurance only.

    Counted, never listed as an item (see the module docstring): the whole
    point is that these need NO human action, so surfacing them as tasks would
    devalue the surface.
    """
    from lib.conversations.project_board import read_board
    return int(read_board(project_path).get('blocked', 0) or 0)


def build_attention_items(project_path: str, conv_id: str = '') -> dict:
    """Aggregate everything genuinely waiting on the human for ``project_path``.

    Args:
        project_path: the project root (the ONLY key — never a global).
        conv_id: the displayed conversation. Reserved for per-conversation
            marking (``mine``) on items that carry an owning conversation; it
            never changes WHICH items are returned, because attention is
            project-scoped — an epic blocked on a question needs the human
            regardless of which chat they happen to be looking at.

    Returns:
        ``{projectPath, items: [...], blocking, advisory, needsYou, waiting}``
        where ``items`` is priority-ordered (blocking first, then by type, then
        newest-first) and ``needsYou == blocking + advisory == len(items)``.
        Never raises — each source degrades to empty independently, so one bad
        read cannot blank the whole surface.
    """
    if not project_path:
        return _empty_attention(project_path)
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    out = _empty_attention(project_path)
    conv_id = (conv_id or '').strip()

    items: list[dict] = []
    for label, source in (('board', _board_questions),
                          ('charter', _charter_proposals),
                          ('conflict', _conflicts)):
        try:
            items.extend(source(project_path))
        except Exception as e:
            logger.debug('[Attention] %s source failed proj=%.40r: %s',
                         label, project_path, e)

    if conv_id:
        for it in items:
            owner = it.get('ownerConvId') or it.get('convId') or ''
            if owner:
                it['mine'] = owner == conv_id
            elif it.get('convIds'):
                it['mine'] = conv_id in it['convIds']

    items.sort(key=lambda it: (
        _SEVERITY_RANK.get(it.get('severity'), 9),
        _TYPE_RANK.get(it.get('type'), 9),
        -int(it.get('ts') or 0),
    ))

    out['items'] = items
    out['blocking'] = sum(1 for it in items if it.get('severity') == 'blocking')
    out['advisory'] = sum(1 for it in items if it.get('severity') == 'advisory')
    out['needsYou'] = len(items)
    try:
        out['waiting'] = _waiting_count(project_path)
    except Exception as e:
        logger.debug('[Attention] waiting count failed proj=%.40r: %s',
                     project_path, e)
    return out


__all__ = ['build_attention_items']
