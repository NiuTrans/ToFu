"""lib.conversations.project_charter — the project "north star" (Pillar #2).

Where the Activity Feed (``project_feed.py``) is the LIVE pulse of what sibling
conversations are *doing*, the Charter is the slow-changing SHARED INTENT every
coordinating conversation reads: the project goal/north-star (``content``) plus
the COMMITTED key decisions (``decisions``). It is the thing that makes N
conversations feel like one mind instead of N amnesiac sessions.

Discipline (locked 2026-06-30; DECISION-commit de-gated by owner 2026-07-12 to
"further reduce human involvement — humans no longer participate in charter
decision-making"):

  • **read** — any project-mode conversation may read the charter
    (``read_charter`` / the ``project_charter_read`` tool). Read-only.
  • **propose** — an agent may PROPOSE an amendment (``propose_amendment`` /
    the ``project_charter_propose`` tool). A proposal writes ONE
    ``proposed_decision`` event into the Activity Feed and NEVER touches the
    ``project_charter`` table. Now optional — a suggestion the agent is not yet
    ready to make binding.
  • **commit** — an agent may now self-COMMIT a DECISION
    (``commit_charter(add_decision=…)`` via the ``project_charter_commit``
    tool): it bumps ``version`` under an optimistic lock so two concurrent
    commits can't silently clobber, and emits ONE ``decided`` event so the
    commit is auditable. The agent path is ``add_decision``-ONLY — it can never
    edit the north-star ``content``.

HUMAN-ONLY corrective levers (optional, NOT required for normal progress): the
north-star ``content`` edit, ``update_decision`` / ``delete_decision`` /
``delete_charter`` — all reachable only through the REST routes. The human
defines the goal and can veto/correct a decision; it need not approve each one.

All functions key STRICTLY on ``project_path`` (a string) — never a
process-global singleton (the read/write-badge thrash trap). Best-effort feed
emission never raises into the caller.
"""

from __future__ import annotations

import json
import time

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.ids import short_id
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# Soft caps so a single charter row stays cheap to inject into every prompt.
# _DECISION_MAX_CHARS is the SHARED ceiling for BOTH a proposal's full text
# AND the committed decision derived from it — the two MUST match, or a commit
# would silently clip a decision that the panel + injected [PROJECT CHARTER]
# block then render mid-sentence. It is deliberately decoupled from the
# feed-row cap (project_feed._SUMMARY_MAX_CHARS = 280), which is scoped to the
# one-line activity summary ONLY and must never bound a committed decision (a
# charter decision is prompt-injected shared intent).
_CONTENT_MAX_CHARS = 8000
_DECISION_MAX_CHARS = 2400
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
    except (TypeError, ValueError) as e:
        logger.debug('[Charter] decisions JSON parse failed (using []): %s', e)
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
    proposal = proposal[:_DECISION_MAX_CHARS]  # full text; the feed-row summary is capped separately
    # A stable proposal id threaded into the event payload so a later commit /
    # dismiss can resolve THIS proposal by id (not fragile text equality) →
    # the pending count decrements durably once acted on.
    proposal_id = short_id('prop_', 16)
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
    """Commit a charter change (optimistic-locked).

    Two callers: the human REST route (may set ``content`` AND/OR append a
    decision) and the ``project_charter_commit`` agent tool (append a decision
    ONLY — never ``content``). Updates ``content`` and/or appends one committed
    ``decision``, bumping ``version``. If ``expected_version`` is provided and
    does NOT match the
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
    # ── Pillar #7: keep the human-facing status lane warm on a committed
    #    decision (non-blocking; the snapshot's staleness gate elides the LLM
    #    when nothing material moved). Best-effort — never raises into commit. ──
    try:
        from lib.conversations.project_status import build_status_snapshot
        build_status_snapshot(project_path, trigger='decision_committed',
                              blocking=False)
    except Exception as e:
        logger.debug('[Charter] status snapshot trigger skipped: %s', e)
    try:
        from lib.conversations.project_watch import address_open_items
        address_open_items(project_path, trigger='decision_committed',
                           blocking=False)
    except Exception as e:
        logger.debug('[Charter] watch address trigger skipped: %s', e)
    return {'ok': True, 'version': new_version}


def _persist_charter(db, project_path: str, content: str, decisions: list,
                     updated_by_conv: str, version: int) -> None:
    """Upsert the single-PK charter row (the same dialect-correct ON CONFLICT
    pattern commit_charter/repair use). Caller owns the transaction commit."""
    db.execute(
        'INSERT INTO project_charter '
        '(project_path, content, decisions, updated_by_conv, updated_at, version) '
        'VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(project_path) DO UPDATE SET '
        'content=excluded.content, decisions=excluded.decisions, '
        'updated_by_conv=excluded.updated_by_conv, '
        'updated_at=excluded.updated_at, version=excluded.version',
        (project_path, content, json.dumps(decisions, ensure_ascii=False),
         updated_by_conv or '', int(time.time() * 1000), version))


def update_decision(project_path: str, index: int, text: str, *,
                    expected_version: int | None = None,
                    updated_by_conv: str = '') -> dict:
    """HUMAN-GATED edit of ONE committed decision, addressed by ``index``.

    The index is resolved against the CURRENT decisions list; ``expected_version``
    (when provided) must match the row version, so the caller is guaranteed the
    list is exactly what it rendered (the index can't silently address the wrong
    decision after a concurrent edit). Bumps ``version`` and emits a ``decided``
    event. Returns ``{'ok', 'version'?, 'error'?, 'current_version'?}``.
    """
    text = (text or '').strip()[:_DECISION_MAX_CHARS]
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not text:
        return {'ok': False, 'error': 'empty decision'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        cur = read_charter(project_path)
        if not cur.get('exists'):
            return {'ok': False, 'error': 'no charter'}
        if expected_version is not None and cur['version'] != expected_version:
            return {'ok': False, 'error': 'version_conflict',
                    'current_version': cur['version']}
        decisions = list(cur['decisions'])
        if index < 0 or index >= len(decisions):
            return {'ok': False, 'error': 'index_out_of_range',
                    'current_version': cur['version']}
        d = decisions[index]
        if isinstance(d, dict):
            d = dict(d)
            d['text'] = text
            d['edited_by_conv'] = updated_by_conv or ''
            d['edited_at'] = int(time.time() * 1000)
        else:
            d = {'text': text, 'by_conv': updated_by_conv or '',
                 'ts': int(time.time() * 1000)}
        decisions[index] = d
        new_version = cur['version'] + 1
        _persist_charter(db, project_path, cur['content'], decisions,
                         updated_by_conv, new_version)
        db.commit()
    except Exception as e:
        logger.error('[Charter] update_decision failed proj=%.40r: %s',
                     project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(project_path, updated_by_conv or '', 'decided',
                           'Decision edited: ' + text,
                           payload={'version': new_version, 'charterEdit': True})
    except Exception as e:
        logger.debug('[Charter] edit feed-emit skipped (persisted): %s', e)
    audit_log('charter_decision_edited', project_path=project_path,
              index=index, version=new_version, by_conv=updated_by_conv)
    return {'ok': True, 'version': new_version}


def delete_decision(project_path: str, index: int, *,
                    expected_version: int | None = None,
                    updated_by_conv: str = '') -> dict:
    """HUMAN-GATED removal of ONE committed decision, addressed by ``index``.

    Same optimistic-lock + index-resolution contract as ``update_decision``.
    Bumps ``version`` and emits a ``decided`` event so the removal is auditable.
    Returns ``{'ok', 'version'?, 'error'?, 'current_version'?}``.
    """
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        cur = read_charter(project_path)
        if not cur.get('exists'):
            return {'ok': False, 'error': 'no charter'}
        if expected_version is not None and cur['version'] != expected_version:
            return {'ok': False, 'error': 'version_conflict',
                    'current_version': cur['version']}
        decisions = list(cur['decisions'])
        if index < 0 or index >= len(decisions):
            return {'ok': False, 'error': 'index_out_of_range',
                    'current_version': cur['version']}
        removed = decisions.pop(index)
        removed_txt = (removed.get('text') if isinstance(removed, dict)
                       else str(removed)) or ''
        new_version = cur['version'] + 1
        _persist_charter(db, project_path, cur['content'], decisions,
                         updated_by_conv, new_version)
        db.commit()
    except Exception as e:
        logger.error('[Charter] delete_decision failed proj=%.40r: %s',
                     project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(project_path, updated_by_conv or '', 'decided',
                           'Decision removed: ' + removed_txt,
                           payload={'version': new_version, 'charterEdit': True})
    except Exception as e:
        logger.debug('[Charter] delete feed-emit skipped (persisted): %s', e)
    audit_log('charter_decision_deleted', project_path=project_path,
              index=index, version=new_version, by_conv=updated_by_conv)
    return {'ok': True, 'version': new_version}


def delete_charter(project_path: str, *, expected_version: int | None = None,
                   updated_by_conv: str = '') -> dict:
    """HUMAN-GATED deletion of the ENTIRE charter row (north star + all
    committed decisions). Optimistic-locked. Emits a ``decided`` event so the
    deletion is auditable. Deleting a non-existent charter is a no-op success.
    Returns ``{'ok', 'error'?, 'current_version'?}``.
    """
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        db = get_thread_db(DOMAIN_CHAT)
        cur = read_charter(project_path)
        if not cur.get('exists'):
            return {'ok': True, 'deleted': False}
        if expected_version is not None and cur['version'] != expected_version:
            return {'ok': False, 'error': 'version_conflict',
                    'current_version': cur['version']}
        db.execute('DELETE FROM project_charter WHERE project_path=?',
                   (project_path,))
        db.commit()
    except Exception as e:
        logger.error('[Charter] delete_charter failed proj=%.40r: %s',
                     project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(project_path, updated_by_conv or '', 'decided',
                           'Charter deleted',
                           payload={'charterDeleted': True})
    except Exception as e:
        logger.debug('[Charter] delete-charter feed-emit skipped (persisted): %s', e)
    audit_log('charter_deleted', project_path=project_path,
              by_conv=updated_by_conv)
    return {'ok': True, 'deleted': True}


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
            # Payload-FIRST: payload.proposal carries the FULL proposal text;
            # the event `summary` is only the 280-char feed-row cap. A commit
            # derives the durable decision from this field, so it must be the
            # full text — never the truncated feed summary.
            'summary': payload.get('proposal', '') or e.get('summary', ''),
            'ts': e.get('ts', 0),
        })
    return out


def repair_truncated_decisions(project_path: str) -> dict:
    """Re-source committed decisions that were stored truncated.

    Historically a commit derived its decision text from the feed-row
    ``summary`` (capped to 280 chars by ``project_feed._SUMMARY_MAX_CHARS``)
    instead of the full ``proposed_decision`` payload — so decisions landed
    clipped mid-sentence in the panel AND the injected ``[PROJECT CHARTER]``
    block. This idempotent repair walks the committed decisions and, for any
    whose stored ``text`` is a strict PREFIX of a longer proposal payload found
    in the feed, replaces it with the full payload text (re-capped to the
    current ``_DECISION_MAX_CHARS``). Bumps ``version`` only when something
    actually changed. Best-effort — never raises into the caller.

    Returns ``{'ok': bool, 'repaired': int, 'version'?: int, 'error'?: str}``.
    """
    if not project_path:
        return {'ok': False, 'repaired': 0, 'error': 'no project'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    try:
        rec = read_charter(project_path)
        if not rec.get('exists') or not rec.get('decisions'):
            return {'ok': True, 'repaired': 0}
        # Full proposal texts available in the feed (payload.proposal is the
        # untruncated source), longest-first so a decision matches its longest
        # available superset.
        from lib.conversations.project_feed import read_project_feed
        feed = read_project_feed(project_path, limit=500)
        proposals = []
        for e in feed.get('events', []):
            if e.get('kind') != 'proposed_decision':
                continue
            full = ((e.get('payload') or {}).get('proposal') or '').strip()
            if full:
                proposals.append(full)
        proposals.sort(key=len, reverse=True)

        decisions = list(rec['decisions'])
        repaired = 0
        for d in decisions:
            if not isinstance(d, dict):
                continue
            cur_txt = (d.get('text') or '').strip()
            if not cur_txt:
                continue
            for full in proposals:
                # A stored decision that is a strict prefix of a longer full
                # proposal was clipped from THAT proposal → restore it.
                if len(full) > len(cur_txt) and full.startswith(cur_txt):
                    d['text'] = full[:_DECISION_MAX_CHARS]
                    repaired += 1
                    break
        if not repaired:
            return {'ok': True, 'repaired': 0, 'version': rec['version']}

        db = get_thread_db(DOMAIN_CHAT)
        new_version = rec['version'] + 1
        ts = int(time.time() * 1000)
        decisions_json = json.dumps(decisions, ensure_ascii=False)
        db.execute(
            'INSERT INTO project_charter '
            '(project_path, content, decisions, updated_by_conv, updated_at, version) '
            'VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(project_path) DO UPDATE SET '
            'content=excluded.content, decisions=excluded.decisions, '
            'updated_by_conv=excluded.updated_by_conv, '
            'updated_at=excluded.updated_at, version=excluded.version',
            (project_path, rec['content'], decisions_json,
             rec['updated_by_conv'] or '', ts, new_version))
        db.commit()
    except Exception as e:
        logger.error('[Charter] repair failed proj=%.40r: %s',
                     project_path, e, exc_info=True)
        return {'ok': False, 'repaired': 0, 'error': str(e)}
    audit_log('charter_decisions_repaired', project_path=project_path,
              repaired=repaired, version=new_version)
    logger.info('[Charter] repaired %d truncated decision(s) proj=%.40r',
                repaired, project_path)
    return {'ok': True, 'repaired': repaired, 'version': new_version}


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

    Exposes ``project_charter_read``, ``project_charter_propose`` and (since
    2026-07-12, owner-directed) ``project_charter_commit``. The commit tool
    calls ``commit_charter`` with ``add_decision`` ONLY — an agent can append a
    committed DECISION (implementation-level shared intent) but can NEVER edit
    the north-star ``content`` through this path. Editing/removing a committed
    decision and deleting the charter stay human-only (the REST routes), as the
    human's corrective levers.
    """
    try:
        if not project_path:
            return ('Error: the project charter is only available in project '
                    'mode (open a project first).')
        if fn_name == 'project_charter_read':
            rec = read_charter(project_path)
            if not rec.get('exists') or not (rec['content'] or rec['decisions']):
                return ('This project has no charter yet. If you reach a '
                        'project-wide decision, commit it with '
                        'project_charter_commit so every sibling conversation '
                        'aligns to it (the human sets the north-star goal).')
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
                return ('Proposal recorded (it appears in the project activity '
                        'feed as a proposed decision). Note: you can COMMIT a '
                        'decision directly with project_charter_commit — a '
                        'proposal is only for suggestions you are not yet ready '
                        'to make binding.')
            return f'Error: could not record proposal ({res.get("error", "unknown")}).'
        if fn_name == 'project_charter_commit':
            # Agent self-commit of a DECISION (owner-directed 2026-07-12). The
            # SAME commit_charter the human REST route uses, exposed to agents
            # so shared intent advances without a human gate. add_decision-ONLY:
            # the north-star `content` is NOT passed, so this tool can never
            # edit the goal/direction (guarded by test). resolves_proposal drops
            # a matching pending proposal out of the human-review list. The
            # _MAX_DECISIONS rolling truncation in commit_charter applies
            # unchanged (no pagination).
            decision = (fn_args.get('decision') or '').strip()
            if not decision:
                return 'Error: decision text is required.'
            ev = fn_args.get('expected_version')
            res = commit_charter(
                project_path, add_decision=decision,
                updated_by_conv=current_conv_id,
                expected_version=(int(ev) if isinstance(ev, (int, float))
                                  or (isinstance(ev, str) and ev.isdigit())
                                  else None),
                resolves_proposal=(fn_args.get('resolves_proposal') or '').strip())
            if res.get('ok'):
                return (f'Decision committed to the charter (version '
                        f'{res.get("version")}). Every sibling conversation now '
                        f'reads it as shared intent. A human can still edit or '
                        f'remove it later if it needs correcting.')
            if res.get('error') == 'version_conflict':
                return ('NOT committed — the charter changed since you read it '
                        f'(current version {res.get("current_version")}). '
                        'Re-read it with project_charter_read and retry.')
            return f'Error: could not commit decision ({res.get("error", "unknown")}).'
        return f"Error: Unknown charter tool '{fn_name}'"
    except Exception as e:
        logger.warning('[Charter] tool %s failed: %s', fn_name, e, exc_info=True)
        return f'Error executing {fn_name}: {e}'


__all__ = [
    'read_charter', 'propose_amendment', 'commit_charter', 'dismiss_proposal',
    'update_decision', 'delete_decision', 'delete_charter',
    'pending_proposals', 'repair_truncated_decisions', 'render_charter_block',
    'execute_charter_tool',
]
