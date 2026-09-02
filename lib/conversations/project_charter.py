"""Project charter aggregate: north star and committed invariants.

Reads and optimistic writes use the Sidecar record authority. Public mutation
functions own validation, audit/feed publication, and background refresh
triggers; agents may append invariants while corrective overwrite/delete
operations remain route-controlled.
"""

from __future__ import annotations

import time

from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.storage import StorageError, get_storage_client

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

# Decision taxonomy (owner-directed 2026-07-28). A charter entry is ONE of:
#   invariant — a binding rule that constrains FUTURE code/decisions
#               (e.g. 'credential redaction is a fail-closed whitelist').
#               Lives in the charter; agent-committable (the 2026-07-12
#               de-gating stands); MUST carry a one-line `summary` — the
#               binding rule itself — which is what the per-turn injection
#               renders. The full text (evidence, archaeology) is read back
#               on demand via project_charter_read.
#   lesson    — a methodology experience note (e.g. 'guards must assert
#               results, not implementation'). Does NOT belong in the
#               always-injected charter; the tool ROUTES it to the project
#               memory system (BM25 relevance-gated injection, updatable,
#               mergeable) instead.
#   report    — a completion / rejection record ('TTFT watchdog landed,
#               commit 69cd968c'). Constrains nothing; belongs in JOURNAL.md.
#               The tool REJECTS these with a pointer to the journal.
_DECISION_KINDS = ('invariant', 'lesson', 'report')
# One line: the binding rule itself. Long enough for 'A is a fail-closed
# whitelist over B; never revert to name-based exclusion', short enough that
# 20 of them stay a scannable list in the injected block.
_SUMMARY_MAX_CHARS = 240
# Conservative auto-fold gate for the lesson-router (channel 2 — see
# _route_lesson_to_memory): fold a new lesson into the top project memory
# only when query-term containment >= 0.5 (near-duplicate). Measured reality:
# genuine same-FAMILY variants score ~0.10 (semantic family != lexical
# overlap), cross-topic ~0.04, verbatim repeats 1.0 — so 0.5 catches repeats
# without ever guessing at family. Family folding is the model's job via the
# explicit `into_memory` channel.
_LESSON_AUTOFOLD_MIN_CONTAINMENT = 0.5

# Rendered in place of the north star when `content` is empty. The goal lives
# in its OWN column precisely so it can never be pushed out of the injected
# window nor FIFO-evicted by decision churn — a goal committed as a decision
# instead is subject to both, which is how one previously went invisible.
# CRITICAL: this text must NOT contain the literal marker of any OTHER injected
# block. The Context Composer enforces idempotency by replacing every block
# whose text contains the marker it is placing — so when this notice spelled the
# goals marker out in full, injecting the goals block DELETED the charter block.
# Measured 2026-07-30: the log showed charter:656 built, then absent from the
# messages. Guarded by test_no_block_text_contains_another_blocks_marker.
_NO_GOAL_NOTICE = (
    '(No north-star statement is set in the charter — the committed decisions '
    'below are implementation-level intent only. This does NOT mean the project '
    'has no goals: the owner sets those in the Project Brain\'s Status & Focus '
    'lane and they arrive as their own separate Project Goals reminder. The '
    'charter\'s north star is human-owned and is edited in the Charter panel.)')

# How many decisions the per-turn injection shows (the tail window). Single
# source for BOTH renderers and the panel health strip's `injectedCount` —
# never re-hardcode 20 elsewhere.
_INJECTION_DECISION_WINDOW = 20


def _empty_charter(project_path: str) -> dict:
    return {
        'project_path': project_path, 'content': '', 'decisions': [],
        'updated_by_conv': '', 'updated_at': 0, 'version': 0, 'exists': False,
    }


def read_charter(project_path: str, *, user_id: int) -> dict:
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
        record = get_storage_client().query(
            'project.charter.get', {
                'project_path': project_path,
                'user_id': int(user_id),
            },
        )
    except Exception as e:
        logger.warning('[Charter] read failed proj=%.40r: %s', project_path, e)
        return _empty_charter(project_path)
    if not record:
        return _empty_charter(project_path)
    value = record.get('value') or {}
    return {
        'project_path': project_path,
        'content': str(value.get('content') or ''),
        'decisions': list(value.get('decisions') or []),
        'updated_by_conv': str(value.get('updated_by_conv') or ''),
        'updated_at': int(
            value.get('updated_at') or record.get('updated_at_ms') or 0),
        'version': int(record.get('version') or value.get('version') or 0),
        'exists': True,
    }


def propose_amendment(
    project_path: str,
    conv_id: str,
    proposal: str,
    *,
    user_id: int,
    title: str = '',
) -> dict:
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
            proposal, user_id=int(user_id), title=title,
            payload={'proposal': proposal, 'proposalId': proposal_id})
    except Exception as e:
        logger.warning('[Charter] propose feed-emit failed proj=%.40r: %s',
                       project_path, e)
        return {'ok': False, 'error': 'feed emit failed'}
    audit_log('charter_proposed', project_path=project_path,
              conv_id=conv_id, chars=len(proposal))
    return {'ok': True, 'event_id': (ev or {}).get('event_id', ''),
            'proposalId': proposal_id}


# How many times a pure append re-reads and replays after losing a CAS race.
# Contention here is a handful of humans/agents committing decisions, not a hot
# loop, so a small bound is ample; exhausting it is REPORTED as a failure
# rather than silently dropping the decision.
_CAS_MAX_ATTEMPTS = 6
_PROJECT_FEED_REPAIR_LIMIT = 200

def commit_charter(project_path: str, *, user_id: int,
                   content: str | None = None,
                   add_decision: str | None = None,
                   decision_kind: str = '',
                   summary: str = '',
                   expected_version: int | None = None,
                   updated_by_conv: str = '',
                   resolves_proposal: str = '') -> dict:
    """Apply one optimistic charter mutation.

    Decision appends commute and retry against the newest version. North-star
    overwrites do not commute, so an explicit expected version is a hard gate.
    Storage acknowledgement precedes feed, audit, status, and watch effects.
    """
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if content is not None and add_decision is not None:
        return {
            'ok': False,
            'error': 'invalid_combination',
            'detail': 'content and add_decision are mutually exclusive',
        }
    committed_decision = (
        (add_decision or '').strip()[:_DECISION_MAX_CHARS]
        if add_decision is not None else ''
    )
    if add_decision is not None and not committed_decision:
        return {'ok': False, 'error': 'empty decision'}
    if content is None and add_decision is None:
        return {'ok': False, 'error': 'no change'}

    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    mutation_id = short_id('charter_', 16)
    client = get_storage_client(write=True)

    try:
        for attempt in range(_CAS_MAX_ATTEMPTS):
            current = read_charter(project_path, user_id=user_id)
            base_version = int(current['version'])
            if (
                content is not None
                and expected_version is not None
                and base_version != expected_version
            ):
                return {
                    'ok': False,
                    'error': 'version_conflict',
                    'current_version': base_version,
                }

            decisions = list(current['decisions'])
            if committed_decision:
                decisions.append(_decision_entry(
                    committed_decision,
                    decision_kind=decision_kind,
                    summary=summary,
                    updated_by_conv=updated_by_conv,
                ))
                decisions = decisions[-_MAX_DECISIONS:]
            value = {
                'content': (
                    (content or '')[:_CONTENT_MAX_CHARS]
                    if content is not None
                    else current['content'][:_CONTENT_MAX_CHARS]
                ),
                'decisions': decisions,
                'updated_by_conv': updated_by_conv or '',
                'updated_at': int(time.time() * 1000),
                'version': base_version + 1,
            }
            try:
                result = client.command(
                    'project.charter.put',
                    {
                        'project_path': project_path,
                        'user_id': int(user_id),
                        'value': value,
                        'expected_version': base_version,
                    },
                    f'charter.commit:{mutation_id}:{base_version}:{attempt}',
                )
                new_version = int(result['version'])
                break
            except StorageError as exc:
                if exc.code != 'database_conflict':
                    raise
                if content is not None and expected_version is not None:
                    return {
                        'ok': False,
                        'error': 'version_conflict',
                        'current_version': read_charter(
                            project_path, user_id=user_id
                        )['version'],
                    }
        else:
            return {
                'ok': False,
                'error': 'contention',
                'current_version': read_charter(
                    project_path, user_id=user_id
                )['version'],
            }
    except Exception as exc:
        logger.error(
            '[Charter] commit failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}

    try:
        from lib.conversations.project_feed import emit_project_event
        event_payload = {'version': new_version}
        if resolves_proposal:
            event_payload['resolvesProposal'] = resolves_proposal
        emit_project_event(
            project_path,
            updated_by_conv or '',
            'decided',
            committed_decision or 'Charter updated',
            user_id=int(user_id),
            payload=event_payload,
        )
    except Exception as exc:
        logger.debug('[Charter] decided feed skipped: %s', exc)

    audit_log(
        'charter_committed',
        project_path=project_path,
        version=new_version,
        by_conv=updated_by_conv,
    )
    try:
        from lib.conversations.project_status import build_status_snapshot
        build_status_snapshot(
            project_path,
            user_id=user_id,
            trigger='decision_committed',
            blocking=False,
        )
    except Exception as exc:
        logger.debug('[Charter] status snapshot trigger skipped: %s', exc)
    try:
        from lib.conversations.project_watch import address_open_items
        address_open_items(
            project_path,
            user_id=user_id,
            trigger='decision_committed',
            blocking=False,
        )
    except Exception as exc:
        logger.debug('[Charter] watch address trigger skipped: %s', exc)
    return {'ok': True, 'version': new_version}

def _decision_entry(text: str, *, decision_kind: str, summary: str,
                    updated_by_conv: str) -> dict:
    """Build ONE committed-decision entry (the shape stored in ``decisions``)."""
    entry = {
        'text': text,
        'by_conv': updated_by_conv or '',
        'ts': int(time.time() * 1000),
    }
    if decision_kind and decision_kind in _DECISION_KINDS:
        entry['kind'] = decision_kind
    summary = (summary or '').strip()[:_SUMMARY_MAX_CHARS]
    if summary:
        entry['summary'] = summary
    return entry


# Sentinel distinguishing "caller said nothing about the summary" from "caller
# explicitly asked to clear it". `None` cannot carry that distinction, and the
# difference is load-bearing: see update_decision's omission semantics.
_SUMMARY_UNSET = object()

def update_decision(project_path: str, index: int, text: str, *, user_id: int,
                    summary=_SUMMARY_UNSET,
                    expected_version: int | None = None,
                    updated_by_conv: str = '') -> dict:
    """Edit one current decision using optimistic version control."""
    text = (text or '').strip()[:_DECISION_MAX_CHARS]
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if not text:
        return {'ok': False, 'error': 'empty decision'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    mutation_id = short_id('charter_edit_', 16)
    client = get_storage_client(write=True)

    try:
        for attempt in range(_CAS_MAX_ATTEMPTS):
            current = read_charter(project_path, user_id=user_id)
            if not current.get('exists'):
                return {'ok': False, 'error': 'no charter'}
            if (
                expected_version is not None
                and current['version'] != expected_version
            ):
                return {
                    'ok': False,
                    'error': 'version_conflict',
                    'current_version': current['version'],
                }
            decisions = list(current['decisions'])
            if index < 0 or index >= len(decisions):
                return {
                    'ok': False,
                    'error': 'index_out_of_range',
                    'current_version': current['version'],
                }

            original = decisions[index]
            had_summary = bool(
                isinstance(original, dict)
                and (original.get('summary') or '').strip()
            )
            if summary is _SUMMARY_UNSET and had_summary:
                return {
                    'ok': False,
                    'error': 'summary_required',
                    'current_version': current['version'],
                    'current_summary': (original.get('summary') or '').strip(),
                }

            decision = (
                dict(original) if isinstance(original, dict)
                else {
                    'by_conv': updated_by_conv or '',
                    'ts': int(time.time() * 1000),
                }
            )
            decision['text'] = text
            if summary is not _SUMMARY_UNSET:
                new_summary = (
                    (summary or '').strip()[:_SUMMARY_MAX_CHARS])
                if new_summary:
                    decision['summary'] = new_summary
                else:
                    decision.pop('summary', None)
            decision['edited_by_conv'] = updated_by_conv or ''
            decision['edited_at'] = int(time.time() * 1000)
            decisions[index] = decision

            value = {
                'content': current['content'],
                'decisions': decisions,
                'updated_by_conv': updated_by_conv or '',
                'updated_at': int(time.time() * 1000),
                'version': current['version'] + 1,
            }
            try:
                result = client.command(
                    'project.charter.put',
                    {
                        'project_path': project_path,
                        'user_id': int(user_id),
                        'value': value,
                        'expected_version': current['version'],
                    },
                    f'charter.update:{mutation_id}:{current["version"]}:{attempt}',
                )
                new_version = int(result['version'])
                break
            except StorageError as exc:
                if exc.code != 'database_conflict':
                    raise
        else:
            return {
                'ok': False,
                'error': 'contention',
                'current_version': read_charter(
                    project_path, user_id=user_id
                )['version'],
            }
    except Exception as exc:
        logger.error(
            '[Charter] update decision failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}

    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path,
            updated_by_conv or '',
            'decided',
            'Decision edited: ' + text,
            user_id=int(user_id),
            payload={'version': new_version, 'charterEdit': True},
        )
    except Exception as exc:
        logger.debug('[Charter] edit feed skipped: %s', exc)
    audit_log(
        'charter_decision_edited',
        project_path=project_path,
        index=index,
        version=new_version,
        by_conv=updated_by_conv,
    )
    return {'ok': True, 'version': new_version}


def delete_decision(project_path: str, index: int, *, user_id: int,
                    expected_version: int | None = None,
                    updated_by_conv: str = '') -> dict:
    """Delete one current decision using optimistic version control."""
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    mutation_id = short_id('charter_drop_', 16)
    client = get_storage_client(write=True)

    try:
        for attempt in range(_CAS_MAX_ATTEMPTS):
            current = read_charter(project_path, user_id=user_id)
            if not current.get('exists'):
                return {'ok': False, 'error': 'no charter'}
            if (
                expected_version is not None
                and current['version'] != expected_version
            ):
                return {
                    'ok': False,
                    'error': 'version_conflict',
                    'current_version': current['version'],
                }
            decisions = list(current['decisions'])
            if index < 0 or index >= len(decisions):
                return {
                    'ok': False,
                    'error': 'index_out_of_range',
                    'current_version': current['version'],
                }
            removed = decisions.pop(index)
            removed_text = (
                removed.get('text') if isinstance(removed, dict)
                else str(removed)
            ) or ''
            value = {
                'content': current['content'],
                'decisions': decisions,
                'updated_by_conv': updated_by_conv or '',
                'updated_at': int(time.time() * 1000),
                'version': current['version'] + 1,
            }
            try:
                result = client.command(
                    'project.charter.put',
                    {
                        'project_path': project_path,
                        'user_id': int(user_id),
                        'value': value,
                        'expected_version': current['version'],
                    },
                    f'charter.delete-decision:{mutation_id}:'
                    f'{current["version"]}:{attempt}',
                )
                new_version = int(result['version'])
                break
            except StorageError as exc:
                if exc.code != 'database_conflict':
                    raise
        else:
            return {
                'ok': False,
                'error': 'contention',
                'current_version': read_charter(
                    project_path, user_id=user_id
                )['version'],
            }
    except Exception as exc:
        logger.error(
            '[Charter] delete decision failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}

    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path,
            updated_by_conv or '',
            'decided',
            'Decision removed: ' + removed_text,
            user_id=int(user_id),
            payload={'version': new_version, 'charterEdit': True},
        )
    except Exception as exc:
        logger.debug('[Charter] delete decision feed skipped: %s', exc)
    audit_log(
        'charter_decision_deleted',
        project_path=project_path,
        index=index,
        version=new_version,
        by_conv=updated_by_conv,
    )
    return {'ok': True, 'version': new_version}


def delete_charter(project_path: str, *, user_id: int,
                   expected_version: int | None = None,
                   updated_by_conv: str = '') -> dict:
    """Delete the complete charter only if the rendered version is current."""
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)
    current = read_charter(project_path, user_id=user_id)
    if not current.get('exists'):
        return {'ok': True, 'deleted': False}
    if (
        expected_version is not None
        and current['version'] != expected_version
    ):
        return {
            'ok': False,
            'error': 'version_conflict',
            'current_version': current['version'],
        }
    try:
        result = get_storage_client(write=True).command(
            'project.charter.delete',
            {
                'project_path': project_path,
                'user_id': int(user_id),
                'expected_version': current['version'],
            },
            f'charter.delete:{project_path}:{current["version"]}',
        )
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return {
                'ok': False,
                'error': 'version_conflict',
                'current_version': read_charter(
                    project_path, user_id=user_id
                )['version'],
            }
        logger.error(
            '[Charter] delete failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}
    except Exception as exc:
        logger.error(
            '[Charter] delete failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'error': str(exc)}

    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path,
            updated_by_conv or '',
            'decided',
            'Charter deleted',
            user_id=int(user_id),
            payload={'charterDeleted': True},
        )
    except Exception as exc:
        logger.debug('[Charter] delete feed skipped: %s', exc)
    audit_log(
        'charter_deleted',
        project_path=project_path,
        by_conv=updated_by_conv,
    )
    return {'ok': True, 'deleted': bool(result.get('deleted'))}

def dismiss_proposal(
    project_path: str,
    conv_id: str,
    proposal_id: str,
    *,
    user_id: int,
    summary: str = '',
) -> dict:
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
            user_id=int(user_id),
            payload={'resolvesProposal': proposal_id})
    except Exception as e:
        logger.warning('[Charter] dismiss feed-emit failed proj=%.40r: %s',
                       project_path, e)
        return {'ok': False, 'error': 'feed emit failed'}
    audit_log('charter_dismissed', project_path=project_path,
              conv_id=conv_id, proposal_id=proposal_id)
    return {'ok': True}


def pending_proposals(project_path: str, *, user_id: int) -> list[dict]:
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
        feed = read_project_feed(project_path, user_id=user_id, limit=500)
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



def repair_truncated_decisions(project_path: str, *, user_id: int) -> dict:
    """Restore decisions that are strict prefixes of full proposal payloads."""
    if not project_path:
        return {'ok': False, 'repaired': 0, 'error': 'no project'}
    from lib.conversations.project_feed import (
        normalize_project_path,
        read_project_feed,
    )
    project_path = normalize_project_path(project_path)
    try:
        current = read_charter(project_path, user_id=user_id)
        if not current.get('exists') or not current.get('decisions'):
            return {'ok': True, 'repaired': 0}

        proposals = [
            ((event.get('payload') or {}).get('proposal') or '').strip()
            for event in read_project_feed(
                project_path, limit=_PROJECT_FEED_REPAIR_LIMIT,
                user_id=user_id,
            ).get('events', [])
            if event.get('kind') == 'proposed_decision'
        ]
        proposals = sorted(filter(None, proposals), key=len, reverse=True)
        decisions = [
            dict(item) if isinstance(item, dict) else item
            for item in current['decisions']
        ]
        repaired = 0
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            stored = (decision.get('text') or '').strip()
            replacement = next(
                (
                    proposal for proposal in proposals
                    if len(proposal) > len(stored)
                    and proposal.startswith(stored)
                ),
                '',
            )
            if replacement:
                decision['text'] = replacement[:_DECISION_MAX_CHARS]
                repaired += 1
        if not repaired:
            return {
                'ok': True,
                'repaired': 0,
                'version': current['version'],
            }

        result = get_storage_client(write=True).command(
            'project.charter.put',
            {
                'project_path': project_path,
                'user_id': int(user_id),
                'value': {
                    'content': current['content'],
                    'decisions': decisions,
                    'updated_by_conv': current['updated_by_conv'] or '',
                    'updated_at': int(time.time() * 1000),
                    'version': current['version'] + 1,
                },
                'expected_version': current['version'],
            },
            f'charter.repair:{project_path}:{current["version"]}',
        )
        new_version = int(result['version'])
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return {
                'ok': False,
                'repaired': 0,
                'error': 'version_conflict',
            }
        logger.error(
            '[Charter] repair failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'repaired': 0, 'error': str(exc)}
    except Exception as exc:
        logger.error(
            '[Charter] repair failed proj=%.40r: %s',
            project_path, exc, exc_info=True,
        )
        return {'ok': False, 'repaired': 0, 'error': str(exc)}

    audit_log(
        'charter_decisions_repaired',
        project_path=project_path,
        repaired=repaired,
        version=new_version,
    )
    return {
        'ok': True,
        'repaired': repaired,
        'version': new_version,
    }

def _decision_headline(d) -> str:
    """The ONE line a per-turn injection shows for a decision.

    The stored `summary` (the binding rule itself) when present; otherwise a
    first-line abridgement of the full text — legacy entries from before the
    summary field existed still render as a scannable headline rather than a
    2,000-char wall. The full text is always one tool call away
    (project_charter_read).
    """
    if isinstance(d, dict):
        summary = (d.get('summary') or '').strip()
        if summary:
            return summary
        txt = (d.get('text') or '').strip()
    else:
        txt = str(d).strip()
    first = txt.split('\n', 1)[0].strip()
    if len(first) > _SUMMARY_MAX_CHARS:
        first = first[:_SUMMARY_MAX_CHARS].rstrip() + '…'
    return first


def render_charter_injection_block(project_path: str, *, user_id: int) -> str:
    """Render the charter for PER-TURN prompt injection: goal in full,
    decisions as a one-line headline list (summary when stored, abridged
    first line otherwise), with a pointer to project_charter_read for the
    full text of any entry.

    The model needs the RULE always resident, not the evidence chain — the
    1.5–2.2k-char measured-evidence narratives are read back on demand.
    Mirrors the board split (render_board_injection_block vs
    render_board_block). Returns '' when there is no charter.
    """
    rec = read_charter(project_path, user_id=user_id)
    if not rec.get('exists') or not (rec['content'] or rec['decisions']):
        return ''
    lines = ['[PROJECT CHARTER] — the shared north star for this project. '
             'All conversations of this project read it; treat it as '
             'authoritative shared intent.']
    if rec['content']:
        lines.append('')
        lines.append(rec['content'].strip())
    else:
        # An ABSENT goal must announce itself. Rendering nothing here is how a
        # real incident stayed hidden: the goal had been committed as an
        # ordinary DECISION, the `content` column was empty, and since the
        # decision list is injected tail-first the goal fell outside the window
        # — so every conversation read implementation decisions as its
        # "authoritative shared intent" with no hint the north star was gone.
        lines.append('')
        lines.append(_NO_GOAL_NOTICE)
    if rec['decisions']:
        lines.append('')
        lines.append('Committed decisions (headlines — call '
                     'project_charter_read with index=N for an entry\'s full '
                     'text):')
        start = max(0, len(rec['decisions']) - _INJECTION_DECISION_WINDOW)
        for i, d in enumerate(rec['decisions'][start:], start):
            head = _decision_headline(d)
            if head:
                lines.append(f'  • [#{i}] {head}')
    return '\n'.join(lines)


def render_charter_block(project_path: str, *, user_id: int) -> str:
    """Render the charter with EVERY decision's complete stored text, never
    abridged. The per-turn prompt injection uses
    ``render_charter_injection_block`` instead; this full renderer backs the
    ``project_charter_read`` tool — the on-demand detail path the injection
    block points to.
    """
    rec = read_charter(project_path, user_id=user_id)
    if not rec.get('exists') or not (rec['content'] or rec['decisions']):
        return ''
    lines = ['[PROJECT CHARTER] — the shared north star for this project. '
             'All conversations of this project read it; treat it as '
             'authoritative shared intent.']
    if rec['content']:
        lines.append('')
        lines.append(rec['content'].strip())
    else:
        lines.append('')
        lines.append(_NO_GOAL_NOTICE)
    if rec['decisions']:
        lines.append('')
        lines.append('Committed decisions:')
        for d in rec['decisions'][-20:]:
            txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
            if txt:
                lines.append(f'  • {txt}')
    return '\n'.join(lines)


def _topic_containment(query: str, memories: list) -> list:
    """Rank ``memories`` by unweighted query-term containment, best-first.

    Returns ``[(coverage, mem)]`` with coverage = |query_terms ∩ doc_terms| /
    |query_terms| ∈ [0, 1]. Chosen over a BM25 threshold after measurement:
    raw BM25 scores are corpus-SIZE dependent (IDF collapses at N=1), and
    global-background IDF punishes exactly the shared family vocabulary —
    containment is deterministic across environments. Used ONLY as a
    conservative near-duplicate gate (≥0.5); semantic family detection is the
    model's job (the `into_memory` parameter).
    """
    from lib.memory.relevance._tokenize import _build_memory_doc, _tokenize
    qt = set(_tokenize(query or ''))
    if not qt:
        return []
    out = []
    for m in memories:
        doc = set(_build_memory_doc(m, include_body=True))
        if not doc:
            continue
        cov = len(qt & doc) / len(qt)
        if cov > 0:
            out.append((cov, m))
    out.sort(key=lambda x: -x[0])
    return out


def _route_lesson_to_memory(project_path: str, lesson_text: str,
                            conv_id: str = '',
                            into_memory: str = '') -> dict:
    """Route a lesson to the PROJECT MEMORY system, not the charter.

    ⚠️ CURRENTLY UNREACHABLE (2026-07-30). Its only caller was the kind=lesson
    branch of the ``project_charter_commit`` agent tool, which was withdrawn
    when the charter became human-review-only. Agents still record lessons via
    ``create_memory``, so the CAPABILITY survives — but the three-channel
    dedup-into-an-existing-memory logic below does NOT run on that path, so
    same-topic lessons will accumulate as separate files again.

    Kept rather than deleted because the dedup measurement it encodes is the
    expensive part and would have to be redone: real same-family lesson pairs
    score ~0.10 lexical containment, which is why channel 2 exists at all.
    Re-pointing ``create_memory`` at this helper is tracked as follow-up debt
    (see the board epic for the goals-inject change) rather than folded into
    that change, per the owner's rule about not fixing adjacent latent issues
    inside another batch.

    Dedup (owner-directed "search same-topic first, fold, else create") is
    three-channel, because measurement showed lexical similarity alone cannot
    detect a semantic family (real same-family lesson pairs score ~0.10
    containment — the family head noun only exists from the second variant
    onward):

      1. EXPLICIT — the caller passes ``into_memory`` (id or exact name of a
         project memory). The common case: the model READ the family memory
         via BM25 prefetch while working, so when it commits a new variant it
         KNOWS the fold target. This is the primary channel.
      2. CONSERVATIVE AUTO-FOLD — top project-candidate containment ≥ 0.5
         (near-duplicate vocabulary). Misses light variants (they take
         channel 3) but never corrupts the corpus with a wrong merge.
      3. CREATE + ADVISE — a new memory; the response lists the closest
         candidates so the model can immediately fold explicitly instead.

    Idempotent — a lesson already present verbatim is a no-op. Returns
    ``{'ok', 'action', 'memory_id'?, 'candidates'?, 'error'?}``. Never raises.
    """
    try:
        from lib.memory.storage import (create_memory, list_memories,
                                        update_memory)
        today = time.strftime('%Y-%m-%d')

        def _fold(mem, via):
            body = (mem.get('body') or '').rstrip()
            if lesson_text[:200] in body:
                return {'ok': True, 'action': 'already_present',
                        'memory_id': mem['id']}
            new_body = (body + f'\n\n---\n\n### 变体（{today}，charter 路由）'
                        f'\n\n' + lesson_text)
            update_memory(mem['id'], {'body': new_body},
                          project_path=project_path)
            audit_log('charter_lesson_routed', project_path=project_path,
                      memory_id=mem['id'], action='updated', via=via,
                      by_conv=conv_id)
            return {'ok': True, 'action': 'updated', 'memory_id': mem['id'],
                    'via': via}

        proj_mems = [m for m in list_memories(project_path, scope='project')
                     if not m.get('is_package')]

        # Channel 1: explicit fold target (id or exact name).
        into_memory = (into_memory or '').strip()
        if into_memory:
            target = next((m for m in proj_mems
                           if m['id'] == into_memory
                           or m.get('name') == into_memory), None)
            if not target:
                return {'ok': False,
                        'error': f"into_memory '{into_memory}' matches no "
                                 'project memory (id or exact name)'}
            return _fold(target, via='explicit')

        # Channel 2: conservative auto-fold on near-duplicate containment.
        ranked = _topic_containment(lesson_text, proj_mems)
        if ranked and ranked[0][0] >= 0.5:
            return _fold(ranked[0][1], via=f'auto containment={ranked[0][0]:.2f}')

        # Channel 3: create + advise with the closest candidates.
        first = lesson_text.split('\n', 1)[0].strip().lstrip('*# ').strip()
        mem = create_memory(
            name=(first[:60] or 'charter lesson'),
            description=first[:240], body=lesson_text,
            tags=['charter-lesson'], scope='project',
            project_path=project_path)
        audit_log('charter_lesson_routed', project_path=project_path,
                  memory_id=mem['id'], action='created', by_conv=conv_id)
        cands = [{'id': m['id'], 'name': (m.get('name') or '')[:80],
                  'containment': round(c, 2)} for c, m in ranked[:3]]
        return {'ok': True, 'action': 'created', 'memory_id': mem['id'],
                'candidates': cands}
    except Exception as e:
        logger.warning('[Charter] lesson route failed proj=%.40r: %s',
                       project_path, e, exc_info=True)
        return {'ok': False, 'error': str(e)}


def execute_charter_tool(fn_name: str, fn_args: dict, *, user_id: int,
                         current_conv_id: str = '',
                         project_path: str = '') -> str:
    """Execute a charter agent tool → human-readable string.

    Exposes ``project_charter_read`` and ``project_charter_propose`` ONLY.

    ``project_charter_commit`` was WITHDRAWN from agents on 2026-07-30
    (owner-directed, reversing the 2026-07-12 de-gating): a charter always
    requires human review. The name is still recognised here so the call is
    refused with an explanation pointing at ``project_charter_propose``, rather
    than failing as an unknown tool for a model that learned it from an older
    transcript. Committing, editing and removing decisions, and deleting the
    charter, are human actions on the REST routes.
    """
    try:
        if not project_path:
            return ('Error: the project charter is only available in project '
                    'mode (open a project first).')
        if fn_name == 'project_charter_read':
            rec = read_charter(project_path, user_id=user_id)
            if not rec.get('exists') or not (rec['content'] or rec['decisions']):
                return ('This project has no charter yet. If you reach a '
                        'project-wide binding rule, raise it with '
                        'project_charter_propose for the human to approve — the '
                        'charter is human-reviewed. Note the owner\'s GOALS are '
                        'a separate surface and arrive in their own separate '
                        'Project Goals reminder, so an empty charter does not '
                        'mean the project has no stated intent.')
            idx = fn_args.get('index')
            if idx is not None and idx != '':
                # Per-entry read: the detail half of the two-tier design. The
                # default (no index) returns the SAME headline list the
                # injection shows; index=N returns ONE entry's full text so
                # the evidence chain costs one entry, not the whole charter.
                try:
                    i = int(idx)
                except (TypeError, ValueError) as e:
                    logger.debug('[Charter] charter-read index %r not an '
                                 'integer: %s', idx, e)
                    return f'Error: index must be an integer, got {idx!r}.'
                decisions = rec['decisions']
                if i < 0:
                    i += len(decisions)
                if i < 0 or i >= len(decisions):
                    return (f'Error: index {idx} out of range '
                            f'(0..{len(decisions) - 1}).')
                d = decisions[i]
                txt = (d.get('text') if isinstance(d, dict) else str(d)) or ''
                summary = (d.get('summary') or '') if isinstance(d, dict) else ''
                head = f'[PROJECT CHARTER] decision #{i}'
                if summary:
                    head += f' — {summary}'
                return (head + '\n\n' + txt +
                        f'\n\n(charter version {rec["version"]}, '
                        f'{i + 1} of {len(decisions)})')
            block = render_charter_injection_block(project_path, user_id=user_id)
            return (block + f'\n\n(charter version {rec["version"]}; pass '
                    'index=N for an entry\'s full text)')
        if fn_name == 'project_charter_propose':
            proposal = (fn_args.get('proposal') or '').strip()
            if not proposal:
                return 'Error: proposal text is required.'
            res = propose_amendment(
                project_path, current_conv_id, proposal,
                user_id=user_id,
                title=(fn_args.get('title') or '').strip())
            if res.get('ok'):
                return ('Proposal recorded — it appears in the project '
                        'activity feed and in the human\'s review surface as a '
                        'proposed decision. It is NOT yet binding: a human '
                        'approves it, because the charter is human-reviewed by '
                        'design. Do not wait on it — continue working, and '
                        'record what you actually did in JOURNAL.md.')
            return f'Error: could not record proposal ({res.get("error", "unknown")}).'
        if fn_name == 'project_charter_commit':
            # HUMAN-ONLY since 2026-07-30 (owner-directed, reversing the
            # 2026-07-12 de-gating): a charter always requires human review.
            # The tool is no longer in CHARTER_TOOLS, so a well-formed turn
            # cannot reach here — but a model that learned the name from an
            # older transcript can still emit the call, and it deserves a
            # reason rather than an opaque unknown-tool error.
            return (
                'project_charter_commit is no longer available to agents: the '
                'charter is human-reviewed, so nothing lands in it '
                'unilaterally. Use project_charter_propose to put this in front '
                'of the human — the proposal is recorded and they approve it. '
                'Your work does not wait on that; continue, and record what you '
                'actually did in JOURNAL.md. For a methodology lesson (how to '
                'work rather than a fact about this codebase), use '
                'create_memory instead.')
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
