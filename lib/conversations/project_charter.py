"""lib.conversations.project_charter — the project "north star" (Pillar #2).

Where the Activity Feed (``project_feed.py``) is the LIVE pulse of what sibling
conversations are *doing*, the Charter is the slow-changing SHARED INTENT every
coordinating conversation reads: the project goal/north-star (``content``) plus
the COMMITTED key decisions (``decisions``). It is the thing that makes N
conversations feel like one mind instead of N amnesiac sessions.

Three-stage discipline (locked by owner, 2026-06-30):

  • **read** — any project-mode conversation may read the charter
    (``read_charter`` / the ``project_charter_read`` tool). Read-only.
  • **propose** — an agent may only PROPOSE an amendment
    (``propose_amendment`` / the ``project_charter_propose`` tool). A proposal
    writes ONE ``proposed_decision`` event into the Activity Feed and NEVER
    touches the ``project_charter`` table. Proposal ≠ commit.
  • **commit** — the actual charter mutation is HUMAN-GATED
    (``commit_charter``). It bumps ``version`` under an optimistic lock so two
    concurrent commits can't silently clobber, and emits ONE ``decided`` event
    so the commit is auditable in the feed. The frontend gate (a confirm UI)
    is wired in a later slice; this module provides the gated function +
    audit.

All functions key STRICTLY on ``project_path`` (a string) — never a
process-global singleton (the read/write-badge thrash trap). Best-effort feed
emission never raises into the caller.
"""

from __future__ import annotations

import json
import time
import uuid

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# Soft caps so a single charter row stays cheap to inject into every prompt.
_CONTENT_MAX_CHARS = 8000
_DECISION_MAX_CHARS = 600
_MAX_DECISIONS = 100


def _empty_charter(project_path: str) -> dict:
    return {
        'project_path': project_path, 'content': '', 'decisions': [],
        'updated_by_conv': '', 'updated_at': 0, 'version': 0, 'exists': False,
    }


def read_charter(project_path: str) -> dict:
    """Return the charter record for ``project_path`` (or an empty shell).

    Read-only. ``{'content', 'decisions': [...], 'version', 'updated_by_conv',
    'updated_at', 'exists'}``. Never raises — returns the empty shell on no
    project / DB error so callers (prompt injection) can treat "no charter"
    uniformly.
    """
    if not project_path:
        return _empty_charter(project_path)
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT project_path, content, decisions, updated_by_conv, '
            '       updated_at, version '
            'FROM project_charter WHERE project_path=?',
            (project_path,)).fetchone()
    except Exception as e:
        logger.warning('[Charter] read failed proj=%.40r: %s', project_path, e)
        return _empty_charter(project_path)
    if not row:
        return _empty_charter(project_path)
    try:
        decisions = json.loads(row['decisions']) if row['decisions'] else []
        if not isinstance(decisions, list):
            decisions = []
    except (TypeError, ValueError):
        decisions = []
    return {
        'project_path': row['project_path'],
        'content': row['content'] or '',
        'decisions': decisions,
        'updated_by_conv': row['updated_by_conv'] or '',
        'updated_at': int(row['updated_at'] or 0),
        'version': int(row['version'] or 0),
        'exists': True,
    }


def propose_amendment(project_path: str, conv_id: str, proposal: str, *,
                      title: str = '') -> dict:
    """Record a PROPOSED charter amendment — feed-only, never writes the table.

    Writes exactly one ``proposed_decision`` event into the Activity Feed so
    the proposal is visible to humans + sibling conversations and leaves an
    audit trail; the charter itself is unchanged until a human commits it.

    Returns ``{'ok': bool, 'event_id'?: str, 'error'?: str}``.
    """
    proposal = (proposal or '').strip()
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not proposal:
        return {'ok': False, 'error': 'empty proposal'}
    proposal = proposal[:_DECISION_MAX_CHARS * 4]  # generous; summary is capped
    # A stable proposal id threaded into the event payload so a later commit /
    # dismiss can resolve THIS proposal by id (not fragile text equality) →
    # the pending count decrements durably once acted on.
    proposal_id = 'prop_' + uuid.uuid4().hex[:16]
    try:
        from lib.conversations.project_feed import emit_project_event
        ev = emit_project_event(
            project_path, conv_id or '', 'proposed_decision',
            proposal, title=title,
            payload={'proposal': proposal, 'proposalId': proposal_id})
    except Exception as e:
        logger.warning('[Charter] propose feed-emit failed proj=%.40r: %s',
                       project_path, e)
        return {'ok': False, 'error': 'feed emit failed'}
    audit_log('charter_proposed', project_path=project_path,
              conv_id=conv_id, chars=len(proposal))
    return {'ok': True, 'event_id': (ev or {}).get('event_id', ''),
            'proposalId': proposal_id}


def commit_charter(project_path: str, *, content: str | None = None,
                   add_decision: str | None = None,
                   expected_version: int | None = None,
                   updated_by_conv: str = '',
                   resolves_proposal: str = '') -> dict:
    """HUMAN-GATED commit of a charter change (optimistic-locked).

    Updates ``content`` and/or appends one committed ``decision``, bumping
    ``version``. If ``expected_version`` is provided and does NOT match the
    current row version, the commit is REJECTED (concurrent-edit guard) — the
    caller must re-read and retry. On success emits ONE ``decided`` event.

    Returns ``{'ok': bool, 'version'?: int, 'error'?: str,
    'current_version'?: int}``.
    """
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        cur = read_charter(project_path)
        if expected_version is not None and cur['version'] != expected_version:
            logger.info('[Charter] commit rejected (version skew) proj=%.40r '
                        'expected=%s current=%s', project_path,
                        expected_version, cur['version'])
            return {'ok': False, 'error': 'version_conflict',
                    'current_version': cur['version']}

        new_content = cur['content'] if content is None else content
        new_content = (new_content or '')[:_CONTENT_MAX_CHARS]
        decisions = list(cur['decisions'])
        committed_decision = ''
        if add_decision:
            committed_decision = add_decision.strip()[:_DECISION_MAX_CHARS]
            if committed_decision:
                decisions.append({
                    'text': committed_decision,
                    'by_conv': updated_by_conv or '',
                    'ts': int(time.time() * 1000),
                })
                if len(decisions) > _MAX_DECISIONS:
                    decisions = decisions[-_MAX_DECISIONS:]
        new_version = cur['version'] + 1
        ts = int(time.time() * 1000)
        decisions_json = json.dumps(decisions, ensure_ascii=False)

        # Upsert: the single-PK row. ON CONFLICT bumps in place. Use the
        # Core-compiled dialect-correct upsert so PG + SQLite both work.
        # Explicit ON CONFLICT upsert with positional ? binds — the same
        # placeholder style project_feed.py uses (proven through the
        # _sql_translate ?→%s bridge on both PG + SQLite). The single-PK
        # project_charter row is created on first commit, updated thereafter.
        db.execute(
            'INSERT INTO project_charter '
            '(project_path, content, decisions, updated_by_conv, updated_at, version) '
            'VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(project_path) DO UPDATE SET '
            'content=excluded.content, decisions=excluded.decisions, '
            'updated_by_conv=excluded.updated_by_conv, '
            'updated_at=excluded.updated_at, version=excluded.version',
            (project_path, new_content, decisions_json,
             updated_by_conv or '', ts, new_version))
        db.commit()
    except Exception as e:
        logger.error('[Charter] commit failed proj=%.40r: %s',
                     project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}

    # Emit a 'decided' event so the commit is auditable in the feed.
    try:
        from lib.conversations.project_feed import emit_project_event
        summary = committed_decision or 'Charter updated'
        _dec_payload = {'version': new_version}
        if resolves_proposal:
            # Mark WHICH pending proposal this commit resolves so
            # pending_proposals() can exclude it durably (no over-count).
            _dec_payload['resolvesProposal'] = resolves_proposal
        emit_project_event(
            project_path, updated_by_conv or '', 'decided',
            summary, payload=_dec_payload)
    except Exception as e:
        logger.debug('[Charter] decided feed-emit skipped (commit persisted): %s', e)
    audit_log('charter_committed', project_path=project_path,
              version=new_version, by_conv=updated_by_conv)
    return {'ok': True, 'version': new_version}


def dismiss_proposal(project_path: str, conv_id: str, proposal_id: str, *,
                     summary: str = '') -> dict:
    """Durably REJECT a pending proposal — emits a ``dismissed`` feed event
    carrying the resolved ``proposalId`` so the proposal drops out of
    ``pending_proposals`` for everyone, permanently (not a local DOM dismiss
    that evaporates on reload). Best-effort. Returns ``{'ok', 'error'?}``.
    """
    proposal_id = (proposal_id or '').strip()
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not proposal_id:
        return {'ok': False, 'error': 'proposalId required'}
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path, conv_id or '', 'dismissed',
            (summary or 'Proposal dismissed')[:_DECISION_MAX_CHARS],
            payload={'resolvesProposal': proposal_id})
    except Exception as e:
        logger.warning('[Charter] dismiss feed-emit failed proj=%.40r: %s',
                       project_path, e)
        return {'ok': False, 'error': 'feed emit failed'}
    audit_log('charter_dismissed', project_path=project_path,
              conv_id=conv_id, proposal_id=proposal_id)
    return {'ok': True}


def pending_proposals(project_path: str) -> list[dict]:
    """The SINGLE source of "decisions awaiting the human".

    A ``proposed_decision`` is PENDING unless a later ``decided`` or
    ``dismissed`` event carries a ``resolvesProposal`` matching its
    ``proposalId``. This is what both ``build_brain_summary`` (the collab-bar
    count) and the Charter panel read — so the action-first number decrements
    the moment a human commits or rejects, and never over-counts. Read-only;
    returns [] on no project / error.

    Each entry: ``{'proposalId', 'event_id', 'conv_id', 'title', 'summary',
    'ts'}`` (newest-first).
    """
    if not project_path:
        return []
    try:
        from lib.conversations.project_feed import read_project_feed
        feed = read_project_feed(project_path, limit=500)
    except Exception as e:
        logger.warning('[Charter] pending read failed proj=%.40r: %s',
                       project_path, e)
        return []
    resolved = set()
    proposals = []
    for e in feed.get('events', []):
        payload = e.get('payload') or {}
        kind = e.get('kind')
        if kind in ('decided', 'dismissed'):
            rid = payload.get('resolvesProposal')
            if rid:
                resolved.add(rid)
        elif kind == 'proposed_decision':
            proposals.append(e)
    out = []
    for e in proposals:
        payload = e.get('payload') or {}
        pid = payload.get('proposalId')
        # A proposal with NO id is legacy (pre-id) — treat as pending (can't
        # be matched, but never silently dropped). A proposal whose id is in
        # the resolved set has been committed or dismissed → excluded.
        if pid and pid in resolved:
            continue
        out.append({
            'proposalId': pid or '', 'event_id': e.get('event_id', ''),
            'conv_id': e.get('conv_id', ''), 'title': e.get('title', ''),
            'summary': e.get('summary', '') or payload.get('proposal', ''),
            'ts': e.get('ts', 0),
        })
    return out


def render_charter_block(project_path: str) -> str:
    """Render the charter as a compact prompt block for system-context
    injection, or '' when there is no charter (so an empty project adds no
    prompt weight). Used by lib/tasks_pkg/system_context.py.
    """
    rec = read_charter(project_path)
    if not rec.get('exists') or not (rec['content'] or rec['decisions']):
        return ''
    lines = ['[PROJECT CHARTER] — the shared north star for this project. '
             'All conversations of this project read it; treat it as '
             'authoritative shared intent.']
    if rec['content']:
        lines.append('')
        lines.append(rec['content'].strip())
    if rec['decisions']:
        lines.append('')
        lines.append('Committed decisions:')
        for d in rec['decisions'][-20:]:
            txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
            if txt:
                lines.append(f'  • {txt}')
    return '\n'.join(lines)


def execute_charter_tool(fn_name: str, fn_args: dict, *,
                         current_conv_id: str = '',
                         project_path: str = '') -> str:
    """Execute a charter agent tool → human-readable string.

    Only ``project_charter_read`` and ``project_charter_propose`` are exposed
    to agents. There is intentionally NO commit tool — commit is human-gated
    (``commit_charter`` is called only from the human-confirm route).
    """
    try:
        if not project_path:
            return ('Error: the project charter is only available in project '
                    'mode (open a project first).')
        if fn_name == 'project_charter_read':
            rec = read_charter(project_path)
            if not rec.get('exists') or not (rec['content'] or rec['decisions']):
                return ('This project has no charter yet. If you reach a '
                        'project-wide decision, use project_charter_propose to '
                        'suggest one (a human commits it).')
            block = render_charter_block(project_path)
            return block + f'\n\n(charter version {rec["version"]})'
        if fn_name == 'project_charter_propose':
            proposal = (fn_args.get('proposal') or '').strip()
            if not proposal:
                return 'Error: proposal text is required.'
            res = propose_amendment(
                project_path, current_conv_id, proposal,
                title=(fn_args.get('title') or '').strip())
            if res.get('ok'):
                return ('Proposal recorded for human review (it appears in the '
                        'project activity feed as a proposed decision). The '
                        'charter is unchanged until a human commits it.')
            return f'Error: could not record proposal ({res.get("error", "unknown")}).'
        return f"Error: Unknown charter tool '{fn_name}'"
    except Exception as e:
        logger.warning('[Charter] tool %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'read_charter', 'propose_amendment', 'commit_charter', 'dismiss_proposal',
    'pending_proposals', 'render_charter_block', 'execute_charter_tool',
]
