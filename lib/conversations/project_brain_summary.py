"""lib.conversations.project_brain_summary — the collaboration-bar summary.

A single cheap, read-only aggregation across the three Project Brain surfaces
(Board + Activity Feed + presence registry) that backs the always-visible
**collaboration bar** — the slim line under the top bar that replaces the old
"who's working" presence strip.

The bar's headline is ordered by ACTION VALUE, not activity: the number of
decisions awaiting the human (the Charter human-gate) comes first, then epics
in progress / open, then the peer count. Crucially, each active peer is joined
to the epic it is *advancing* (``peerEpics``: peer conv_id → its claimed epic
title) so the bar can say "conversation X · advancing «Refactor the parser»"
instead of the meaningless "(untitled) · generating".

Design invariants (same as every Project Brain surface):
  • **Keyed strictly on ``project_path``** — never a process-global singleton.
  • **Best-effort** — any sub-read failing degrades that field to a safe
    default; the summary never raises.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _empty_summary() -> dict:
    return {
        'epicsOpen': 0, 'epicsClaimed': 0, 'epicsDone': 0,
        'pendingDecisions': 0, 'activePeers': 0, 'peerEpics': {},
        'charterExists': False, 'conflicts': 0, 'conflictMessages': [],
        'statusLine': '',
    }


def build_brain_summary(project_path: str) -> dict:
    """Aggregate the collaboration-bar summary for ``project_path``.

    Returns ``{epicsOpen, epicsClaimed, epicsDone, pendingDecisions,
    activePeers, peerEpics, charterExists}``. ``peerEpics`` maps an active
    peer's conv_id → the title of the board epic it currently owns (an
    UNEXPIRED claim), so the bar can show what each peer is *advancing*.
    Peers with no claim are simply absent from the map (the bar falls back to
    the activity word for them). Never raises — every field degrades safely.
    """
    if not project_path:
        return _empty_summary()
    out = _empty_summary()

    # ── Board counts + the claim→epic join source ──
    board_tasks = []
    try:
        from lib.conversations.project_board import read_board
        board = read_board(project_path)
        out['epicsOpen'] = int(board.get('open', 0))
        out['epicsClaimed'] = int(board.get('claimed', 0))
        out['epicsDone'] = int(board.get('done', 0))
        board_tasks = board.get('tasks', []) or []
    except Exception as e:
        logger.debug('[BrainSummary] board read failed proj=%.40r: %s',
                     project_path, e)

    # owner_conv_id → epic title, for epics whose EFFECTIVE status is claimed.
    # Single source of the claim→conv join (shared with build_peer_status).
    from lib.conversations.project_board import claims_by_conv
    claim_by_conv = claims_by_conv(board_tasks)

    # ── Pending decisions = proposals NOT yet resolved by a commit/dismiss ──
    #    The SINGLE source (project_charter.pending_proposals) both this count
    #    and the Charter panel read, so a committed/rejected proposal drops out
    #    durably — the action-first "N awaiting you" number decrements and
    #    never over-counts. (Was: a raw proposed_decision count that never
    #    decremented after commit.)
    try:
        from lib.conversations.project_charter import pending_proposals
        out['pendingDecisions'] = len(pending_proposals(project_path))
    except Exception as e:
        logger.debug('[BrainSummary] pending read failed proj=%.40r: %s',
                     project_path, e)

    # ── Charter existence ──
    try:
        from lib.conversations.project_charter import read_charter
        rec = read_charter(project_path)
        out['charterExists'] = bool(rec.get('exists'))
    except Exception as e:
        logger.debug('[BrainSummary] charter read failed proj=%.40r: %s',
                     project_path, e)

    # ── Active peers + the peer→epic JOIN (the deep-collaboration signal) ──
    peers = []
    try:
        from lib.presence.registry import snapshot
        peers = snapshot(project_path).get('peers', []) or []
        # Conversation-level peers only (a sub-agent carries agentId); a claim
        # is owned by a conversation, so we join on the conversation peer.
        conv_ids = {p.get('convId') for p in peers
                    if p.get('convId') and not p.get('agentId')}
        out['activePeers'] = len(conv_ids)
        peer_epics = {}
        for cid in conv_ids:
            title = claim_by_conv.get(cid)
            if title:
                peer_epics[cid] = title
        out['peerEpics'] = peer_epics
    except Exception as e:
        logger.debug('[BrainSummary] presence read failed proj=%.40r: %s',
                     project_path, e)

    # ── Conflict advisories: file-set overlaps between 2+ ACTIVE peers ──
    #    Recomputed from the SAME peer snapshot via the SAME backend judgment
    #    (detect_overlaps) the live conflict broadcast uses — no second mirror,
    #    no stored state. Each message is a fully-formed backend string the bar
    #    renders verbatim.
    try:
        if peers:
            from lib.presence.conflict import detect_overlaps
            advisories = detect_overlaps(peers)
            out['conflicts'] = len(advisories)
            out['conflictMessages'] = [a.get('message', '') for a in advisories
                                       if a.get('message')]
    except Exception as e:
        logger.debug('[BrainSummary] conflict detect failed proj=%.40r: %s',
                     project_path, e)

    # ── Pillar #7: the ambient one-line project-status headline. Read-only
    #    (no synthesis on this hot always-visible-bar path) — the LATEST stored
    #    snapshot's first sentence, or '' when none exists yet. ──
    try:
        from lib.conversations.project_status import status_line
        out['statusLine'] = status_line(project_path)
    except Exception as e:
        logger.debug('[BrainSummary] status line read failed proj=%.40r: %s',
                     project_path, e)

    return out


__all__ = ['build_brain_summary']
