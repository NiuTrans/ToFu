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
  • **A live file conflict** (two active peers touching the same file).
    Notify-only and SELF-CLEARING — it is recomputed from the presence
    registry and vanishes when a peer goes idle — and it has no resolving
    control: the system deliberately never locks, the operator decides
    whether to intervene. Owner directive 2026-08-01: an item that needs
    nothing from the human must not occupy this surface. The overlap stays
    visible where LIVE STATUS belongs — the collab bar's detail lines
    (``summary.conflictMessages``) and the Team tab, both fed by
    ``lib.presence.conflict.detect_overlaps`` directly.
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
    'charter_proposal': 1,
}

# Cap for DISPLAY-ONLY short fields (e.g. an epic title) so one pathological
# value can't dominate the panel. Never apply it to a field a resolving
# control submits back (see _charter_proposals).
_TEXT_MAX = 600

# The block REASON is the card's background section ("why did this stop?") —
# the operator's whole complaint when it is missing context. It renders
# through the clamp (collapsible), so a longer allowance costs no screen
# space; cap only against pathological payloads.
_REASON_MAX = 2000


def _empty_attention(project_path: str = '') -> dict:
    return {
        'projectPath': project_path,
        'items': [],
        'blocking': 0,
        'advisory': 0,
        'needsYou': 0,
        'waiting': 0,
    }


def _conv_titles(conv_ids: list) -> dict:
    """Map ``convId -> title`` for the attention items' provenance chips.

    The operator's first question on a halted-epic card is "which chat asked
    me this?" — a bare conv id answers nothing. One bounded IN query over the
    conversations table; best-effort (a failure degrades to id-only chips,
    never blanks the surface).
    """
    ids = []
    for c in conv_ids or []:
        c = str(c or '').strip()
        if c and c not in ids:
            ids.append(c)
    if not ids:
        return {}
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        marks = ','.join('?' * len(ids))
        rows = db.execute(
            f'SELECT id, title FROM conversations WHERE id IN ({marks})',
            tuple(ids)).fetchall()
        return {r['id']: (r['title'] or '') for r in rows}
    except Exception as e:
        logger.debug('[Attention] conv title lookup failed: %s', e)
        return {}


def _board_questions(project_path: str) -> list[dict]:
    """Epics halted on a structured human question — the ONLY item type that
    stops a workstream indefinitely.

    ``project_dispatch`` skips an epic whose ``block_question`` is set and
    whose ``human_answer`` is empty on EVERY heartbeat, so unlike a cooldown
    block this never resolves on its own. Uses the same partition predicate as
    ``render_board_block``'s pending-question lane and the frontend's awaiting
    lane, so the three can never drift.

    Provenance: ``blocked_by`` (who raised the block) is projected as
    ``askedByConvId`` (+ resolved ``askedByTitle``), falling back to
    ``created_by_conv`` for rows blocked before the column existed. The same
    id also fills ``convId`` so ``build_attention_items``' ``mine`` marking
    works for board questions exactly as it does for proposals.
    """
    from lib.conversations.project_board import read_board
    rows = []
    board = read_board(project_path)
    for t in board.get('tasks', []) or []:
        if t.get('kind') == 'lease':
            continue
        if t.get('status') != 'open':
            continue
        q = t.get('block_question')
        if not q or (t.get('human_answer') or '').strip():
            continue
        rows.append((t, q))
    titles = _conv_titles([
        (t.get('blocked_by') or t.get('created_by_conv') or '')
        for t, _q in rows])
    out = []
    for t, q in rows:
        asked_by = (t.get('blocked_by') or t.get('created_by_conv') or '')
        out.append({
            'type': 'board_question',
            'severity': 'blocking',
            'id': t.get('id', ''),
            'title': (t.get('title') or '')[:_TEXT_MAX],
            'question': str(q.get('q') or '')[:_TEXT_MAX],
            'options': q.get('options') if isinstance(q.get('options'), list) else [],
            'reason': (t.get('block_reason') or '')[:_REASON_MAX],
            'blockCount': int(t.get('block_count') or 0),
            'ownerConvId': t.get('owner_conv_id') or '',
            'convId': asked_by,
            'askedByConvId': asked_by,
            'askedByTitle': titles.get(asked_by, ''),
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
    props = pending_proposals(project_path)
    # The author is the card's provenance ("which conversation proposed
    # this?") — the same askedBy* shape the board-question card carries, so
    # every decision the panel shows wears ONE provenance chip.
    titles = _conv_titles([p.get('conv_id', '') for p in props])
    return [{
        'type': 'charter_proposal',
        'severity': 'advisory',
        'id': p.get('proposalId', ''),
        'title': (p.get('title') or '')[:_TEXT_MAX],
        'text': p.get('summary') or '',
        'convId': p.get('conv_id', ''),
        'askedByConvId': p.get('conv_id', ''),
        'askedByTitle': titles.get(p.get('conv_id', ''), ''),
        'ts': int(p.get('ts') or 0),
        'tab': 'charter',
    } for p in props]


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
                          ('charter', _charter_proposals)):
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
