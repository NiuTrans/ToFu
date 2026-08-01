"""lib.conversations.project_brain_influence — per-conversation brain influence.

The global Project Brain surfaces (Charter / Board / Activity Feed) answer
"what is the shared state of this project?". This module answers the DIFFERENT,
conversation-scoped question the human actually asks when they look at one
chat: **"how is THIS conversation being influenced by the project brain right
now?"** — which charter decisions it is bound by, which goals the owner set,
which board epics it OWNS (claimed by this conv), which epics it must AVOID (a
sibling holds an unexpired lease), and which decisions await a human.

The answer separates TWO channels that look identical on screen but are not the
same thing, and conflating them is what makes the lens misleading:

  • **injected** — the block really is spliced into this turn's prompt, so the
    model has read it whether or not it wanted to (charter headlines, open
    goals, the abridged board).
  • **tool-visible** — the model can REACH it, but only by spending a tool
    round (each epic's full text, the activity feed, live peer status). It
    costs no context until asked for, and the model may never look.

Reporting both as one undifferentiated list tells the human their goal is
steering the model when nothing had to read it. Each lane therefore carries
``injected`` PLUS the measured ``chars`` of the real block: a boolean says a
lane reaches the model, only the count says what it costs every turn.

Single source of truth (owner invariant — backend is authoritative, the
frontend is a pure renderer): every ``injected`` flag and ``chars`` count is
produced by CALLING the exact function ``lib/tasks_pkg/system_context/
_inject.py`` calls — ``render_charter_injection_block`` /
``render_goals_injection_block`` / ``render_board_injection_block`` — so the
panel cannot drift from what the model really sees.

★ Two drifts measured on the live project 2026-07-31, both of which this
module previously reported as healthy:
  1. The GOALS lane did not exist here at all, while ``_inject.py`` ★4.455 was
     shipping a 308-char ``[PROJECT GOALS]`` block every turn. The panel
     omitted the one input the human sets by hand.
  2. ``board.injected`` was read off ``render_board_block`` — the pull-based
     TOOL renderer (18,178 chars) — not ``render_board_injection_block``, the
     one the prompt actually uses (8,845 chars, abridged to id + headline).
     Both are non-empty on a live board, so the boolean agreed by coincidence
     and the wrong call site survived; they diverge exactly when the board is
     in a state the two renderers disagree about. The panel also shows each
     epic's FULL stored title while the prompt ships ~200 chars of it, so
     ``abridgedInPrompt`` is now reported rather than left to be discovered.

Design invariants (shared with every Project Brain surface):
  • **Keyed strictly on ``project_path`` + ``conv_id``** — never a
    process-global singleton.
  • **Best-effort** — any sub-read failing degrades that field to a safe
    default; never raises into the caller (a read route).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# The brain surfaces that reach the model ONLY through a deliberate tool round.
# Static because this set is fixed by the tool registry
# (``lib/tools/conversation.py``), not by project state — the per-lane facts
# that DO vary (does the feed have events, is a peer live) are answered by the
# panel's own tabs. Listing them beside the injected lanes is what stops the
# panel implying the model has already read everything the brain knows.
_TOOL_VISIBLE = (
    ('project_board_read', 'infToolBoardFull'),
    ('project_charter_read', 'infToolCharterFull'),
    ('project_feed_read', 'infToolFeed'),
    ('project_peer_status', 'infToolPeer'),
)


def _empty_influence(project_path: str = '', conv_id: str = '') -> dict:
    return {
        'projectPath': project_path,
        'convId': conv_id,
        'charter': {
            'exists': False, 'content': '', 'decisions': [], 'version': 0,
            'injected': False, 'chars': 0,
            'contentSet': False, 'decisionCount': 0, 'injectedCount': 0,
        },
        # The human owner's standing intent (Status & Focus, kind='goal').
        # Its OWN lane because a goal reaches the model through its own
        # [PROJECT GOALS] block and never through the charter (charter #0).
        'goals': {
            'injected': False, 'chars': 0,
            'items': [],      # [{text}] — the open goals actually shipped
        },
        'board': {
            'exists': False,
            'injected': False, 'chars': 0,
            # True when the prompt ships abridged headlines while this panel
            # shows the full stored text — without it the human reads a
            # 1000-char epic here and assumes the model received all of it.
            'abridgedInPrompt': False,
            'mine': [],       # epics THIS conv owns (a live claim)
            'avoid': [],      # epics a SIBLING owns → this conv must not redo
            'open': [],       # unclaimed epics this conv could pick up
        },
        'pendingDecisions': [],   # proposals awaiting the human (project-wide)
        'toolVisible': [{'tool': t, 'labelKey': k} for t, k in _TOOL_VISIBLE],
    }


def build_conv_influence(project_path: str, conv_id: str) -> dict:
    """Compute how the project brain influences ONE conversation.

    Args:
        project_path: the project root the conversation belongs to.
        conv_id: the conversation whose influence we're describing.

    Returns a structured dict (see ``_empty_influence`` for the shape). Every
    ``injected`` flag and ``chars`` count mirrors the system-context injection
    gate exactly, because each is computed by calling the SAME render function
    ``_inject.py`` calls: a lane is injected iff its block is non-empty (an
    empty project adds no prompt weight), and ``chars`` is that block's real
    length. ``toolVisible`` names the surfaces that reach the model only via a
    tool round, so the panel can separate "the model has read this" from "the
    model could go look". Never raises.
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
            read_charter, render_charter_injection_block,
        )
        rec = read_charter(project_path)
        out['charter']['exists'] = bool(rec.get('exists'))
        out['charter']['content'] = rec.get('content', '') or ''
        out['charter']['version'] = int(rec.get('version', 0) or 0)
        # Committed decisions, STRUCTURED (the frontend is a pure renderer —
        # it must never re-derive kind/summary from raw text), newest-first,
        # capped to what the prompt shows (the injection's tail window).
        from lib.conversations.project_charter import (
            _INJECTION_DECISION_WINDOW)
        decisions = []
        for d in (rec.get('decisions') or [])[-_INJECTION_DECISION_WINDOW:]:
            if isinstance(d, dict):
                decisions.append({
                    'text': d.get('text') or '',
                    'summary': d.get('summary') or '',
                    'kind': d.get('kind') or '',
                    'ts': d.get('ts') or 0,
                    'by_conv': d.get('by_conv') or '',
                })
            elif str(d).strip():
                decisions.append({'text': str(d), 'summary': '', 'kind': '',
                                  'ts': 0, 'by_conv': ''})
        decisions.reverse()
        out['charter']['decisions'] = decisions
        # Health signals for the panel's health strip — computed HERE, never
        # re-derived in the frontend (backend single source of truth).
        all_decisions = rec.get('decisions') or []
        out['charter']['contentSet'] = bool(
            (rec.get('content') or '').strip())
        out['charter']['decisionCount'] = len(all_decisions)
        out['charter']['injectedCount'] = min(
            len(all_decisions), _INJECTION_DECISION_WINDOW)
        # injected iff the SAME block the prompt uses is non-empty — the
        # per-turn INJECTION renderer (headlines), not the tool's full one.
        _charter_block = render_charter_injection_block(project_path)
        out['charter']['injected'] = bool(_charter_block)
        out['charter']['chars'] = len(_charter_block)
    except Exception as e:
        logger.debug('[BrainInfluence] charter read failed proj=%.40r: %s',
                     project_path, e)

    # ── Goals (Status & Focus — the human owner's standing intent) ──
    #    A goal is live because it EXISTS and is unresolved (charter #0): it
    #    never travels through the charter, it ships as its own block. This
    #    lane was absent until 2026-07-31, so a panel titled "how this
    #    conversation is influenced" omitted the human's own directive.
    try:
        from lib.conversations.project_watch import (
            list_watch_items, render_goals_injection_block,
        )
        _goals_block = render_goals_injection_block(project_path)
        out['goals']['injected'] = bool(_goals_block)
        out['goals']['chars'] = len(_goals_block)
        # Filtered by the SAME rule the renderer uses (kind='goal' AND
        # status='open') so a resolved goal can never be listed here as if it
        # were still steering the model.
        out['goals']['items'] = [
            {'text': it.get('text') or ''}
            for it in (list_watch_items(project_path).get('items') or [])
            if it.get('kind') == 'goal' and it.get('status') == 'open'
            and (it.get('text') or '').strip()
        ]
    except Exception as e:
        logger.debug('[BrainInfluence] goals read failed proj=%.40r: %s',
                     project_path, e)

    # ── Board (what this conv owns vs. must avoid) ──
    #    Derived from the SAME read_board the injected block uses, then split
    #    by owner relative to THIS conv — a faithful, non-heuristic mirror of
    #    the injected block's "(you)" vs "avoid" annotations.
    try:
        from lib.conversations.project_board import (
            read_board, render_board_injection_block, _INJECT_TITLE_MAX_CHARS,
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
        # injected iff the block the PROMPT uses is non-empty. It MUST be
        # render_board_injection_block — render_board_block is the pull-based
        # TOOL renderer, and reading the flag off it described a block the
        # model never received (18,178 vs 8,845 chars measured live).
        _board_block = render_board_injection_block(
            project_path, current_conv_id=conv_id)
        out['board']['injected'] = bool(_board_block)
        out['board']['chars'] = len(_board_block)
        # The prompt ships id + abridged headline; the rows above carry each
        # epic's FULL stored text. Say so when they genuinely differ.
        out['board']['abridgedInPrompt'] = bool(_board_block) and any(
            len((tk.get('title') or '').strip()) > _INJECT_TITLE_MAX_CHARS
            or '\n' in (tk.get('title') or '').strip()
            for tk in tasks)
    except Exception as e:
        logger.debug('[BrainInfluence] board read failed proj=%.40r: %s',
                     project_path, e)

    # ── Pending decisions awaiting the human (project-wide human gate) ──
    #    Same single source the collab-bar count + Charter panel read. NOTE
    #    these are NOT injected: a proposal reaches agents only once a human
    #    commits it, which is why they render outside the injected lanes.
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
