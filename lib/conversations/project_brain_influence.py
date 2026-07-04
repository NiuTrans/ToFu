"""lib.conversations.project_brain_influence — per-conversation brain influence.

The global Project Brain surfaces (Charter / Board / Activity Feed) answer
"what is the shared state of this project?". This module answers the DIFFERENT,
conversation-scoped question the human actually asks when they look at one
chat: **"how is THIS conversation being influenced by the project brain right
now?"** — which charter decisions it is bound by, which board epics it OWNS
(claimed by this conv), which epics it must AVOID (a sibling holds an unexpired
lease), and which decisions are still awaiting a human.

Single source of truth (owner invariant — backend is authoritative, the
frontend is a pure renderer): the two prompt-facing markers this returns are
computed from the SAME functions that build the actual injected system blocks
(``render_charter_block`` / ``render_board_block(project_path, conv_id)``), so
the "influence" the panel shows can never drift from what the model really
sees. ``render_board_block`` is already conversation-aware — it stamps "(you)"
on this conv's claims and an explicit avoid-duplication hint on peer-owned
epics — so the per-conv split below is derived from the SAME board read, not a
second heuristic.

Design invariants (shared with every Project Brain surface):
  • **Keyed strictly on ``project_path`` + ``conv_id``** — never a
    process-global singleton.
  • **Best-effort** — any sub-read failing degrades that field to a safe
    default; never raises into the caller (a read route).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _empty_influence(project_path: str = '', conv_id: str = '') -> dict:
    return {
        'projectPath': project_path,
        'convId': conv_id,
        'charter': {
            'exists': False, 'content': '', 'decisions': [], 'version': 0,
            'injected': False,
        },
        'board': {
            'exists': False,
            'injected': False,
            'mine': [],       # epics THIS conv owns (a live claim)
            'avoid': [],      # epics a SIBLING owns → this conv must not redo
            'open': [],       # unclaimed epics this conv could pick up
        },
        'pendingDecisions': [],   # proposals awaiting the human (project-wide)
    }


def build_conv_influence(project_path: str, conv_id: str) -> dict:
    """Compute how the project brain influences ONE conversation.

    Args:
        project_path: the project root the conversation belongs to.
        conv_id: the conversation whose influence we're describing.

    Returns a structured dict (see ``_empty_influence`` for the shape). The
    two ``injected`` flags mirror the system-context injection gate exactly:
    the charter/board block is injected iff its render function returns a
    non-empty block (an empty project adds no prompt weight). Never raises.
    """
    if not project_path:
        return _empty_influence(project_path, conv_id)
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    conv_id = (conv_id or '').strip()
    out = _empty_influence(project_path, conv_id)

    # ── Charter (the shared north star this conv is bound by) ──
    try:
        from lib.conversations.project_charter import (
            read_charter, render_charter_block,
        )
        rec = read_charter(project_path)
        out['charter']['exists'] = bool(rec.get('exists'))
        out['charter']['content'] = rec.get('content', '') or ''
        out['charter']['version'] = int(rec.get('version', 0) or 0)
        # Committed decisions, newest-first, capped to what the prompt shows
        # (render_charter_block injects the last 20).
        decisions = []
        for d in (rec.get('decisions') or [])[-20:]:
            txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
            if txt:
                decisions.append(txt)
        decisions.reverse()
        out['charter']['decisions'] = decisions
        # injected iff the SAME block the prompt uses is non-empty.
        out['charter']['injected'] = bool(render_charter_block(project_path))
    except Exception as e:
        logger.debug('[BrainInfluence] charter read failed proj=%.40r: %s',
                     project_path, e)

    # ── Board (what this conv owns vs. must avoid) ──
    #    Derived from the SAME read_board the injected block uses, then split
    #    by owner relative to THIS conv — a faithful, non-heuristic mirror of
    #    render_board_block's "(you)" vs "avoid" annotations.
    try:
        from lib.conversations.project_board import (
            read_board, render_board_block,
        )
        board = read_board(project_path)
        tasks = board.get('tasks', []) or []
        out['board']['exists'] = bool(tasks)
        for tk in tasks:
            status = tk.get('status')
            owner = tk.get('owner_conv_id') or ''
            entry = {
                'id': tk.get('id', ''),
                'title': tk.get('title', ''),
                'owner': owner,
                'dispatched': bool(tk.get('dispatched')),
                'dependsOn': tk.get('depends_on', []) or [],
            }
            if status == 'claimed' and owner and conv_id and owner == conv_id:
                out['board']['mine'].append(entry)
            elif status == 'claimed' and owner:
                out['board']['avoid'].append(entry)
            elif status == 'open':
                out['board']['open'].append(entry)
        # injected iff the SAME (conv-aware) block the prompt uses is non-empty.
        out['board']['injected'] = bool(
            render_board_block(project_path, current_conv_id=conv_id))
    except Exception as e:
        logger.debug('[BrainInfluence] board read failed proj=%.40r: %s',
                     project_path, e)

    # ── Pending decisions awaiting the human (project-wide human gate) ──
    #    Same single source the collab-bar count + Charter panel read.
    try:
        from lib.conversations.project_charter import pending_proposals
        pending = pending_proposals(project_path)
        out['pendingDecisions'] = [{
            'proposalId': p.get('proposalId', ''),
            'summary': p.get('summary', ''),
            'convId': p.get('conv_id', ''),
            'title': p.get('title', ''),
            'ts': p.get('ts', 0),
            # true when THIS conversation is the one that raised the proposal.
            'mine': bool(conv_id and p.get('conv_id') == conv_id),
        } for p in pending]
    except Exception as e:
        logger.debug('[BrainInfluence] pending read failed proj=%.40r: %s',
                     project_path, e)

    return out


__all__ = ['build_conv_influence']
