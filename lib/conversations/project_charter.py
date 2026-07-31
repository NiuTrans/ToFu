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
    (``commit_charter(add_decision=…)`` via the human REST route; the
    ``project_charter_commit`` agent tool was withdrawn 2026-07-30
    tool): it bumps ``version`` under an optimistic lock so two concurrent
    commits can't silently clobber, and emits ONE ``decided`` event so the
    commit is auditable. The agent path is ``add_decision``-ONLY — it can never
    edit the north-star ``content``.
    **Kind routing (owner-directed 2026-07-28):** every commit declares a
    ``kind``. Only ``invariant`` (a binding rule constraining FUTURE
    decisions) lands here; ``lesson`` (methodology experience) routes to the
    project memory system with BM25 dedup; ``report`` (completion record)
    is rejected to JOURNAL.md. The per-turn injection
    (``render_charter_injection_block``) renders each invariant's one-line
    ``summary`` — the full text is the ``project_charter_read`` detail path.

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
# block. `_refresh_tail_block` enforces idempotency by STRIPPING every block
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


# How many times a pure append re-reads and replays after losing a CAS race.
# Contention here is a handful of humans/agents committing decisions, not a hot
# loop, so a small bound is ample; exhausting it is REPORTED as a failure
# rather than silently dropping the decision.
_CAS_MAX_ATTEMPTS = 6


def commit_charter(project_path: str, *, content: str | None = None,
                   add_decision: str | None = None,
                   decision_kind: str = '',
                   summary: str = '',
                   expected_version: int | None = None,
                   updated_by_conv: str = '',
                   resolves_proposal: str = '') -> dict:
    """Commit a charter change. Concurrency-safe per OPERATION, not per caller.

    ``content`` and ``add_decision`` are MUTUALLY EXCLUSIVE — a mixed call is
    refused with ``invalid_combination``. That is not tidiness: it is what makes
    "is this a pure append?" decidable from the arguments, which is the
    precondition for replaying one safely below. While the combination was
    representable, replay safety rested on caller habit.

    Two operations, two concurrency contracts:

    * **append** (``add_decision``) — COMMUTES with every other append, so a
      concurrent commit is not a conflict. The write is a CAS on ``version``;
      on a miss we RE-READ and re-append only our OWN entry, up to
      ``_CAS_MAX_ATTEMPTS``. ``expected_version`` is therefore advisory here: a
      stale one does NOT refuse the append. (The panel bakes the version it
      rendered into the button and sibling agents self-commit constantly, so
      refusing would break that button exactly when the project is busy.)
      ``content`` is never written by this path — an append carries no opinion
      about the north star and must not revert a concurrent edit to it.
    * **overwrite** (``content``) — does NOT commute: rewriting the north star
      from a stale base destroys the other edit. ``expected_version`` stays a
      HARD gate (``version_conflict``), and the write is still CAS'd so the
      check cannot be defeated by a race after the read.

    Before this split the whole function was a read-modify-write that wrote the
    ENTIRE row from a stale read, so two interleaved commits clobbered each
    other and the loser was told ``ok=True`` — measured, see
    tests/test_project_charter_concurrency.py.

    Returns ``{'ok': bool, 'version'?: int, 'error'?: str,
    'current_version'?: int}``.
    """
    if not project_path:
        return {'ok': False, 'error': 'no project'}
    if content is not None and add_decision:
        # Refused BEFORE any write: a partial application is worse than none.
        logger.info('[Charter] commit refused (content + add_decision in one '
                    'call) proj=%.40r', project_path)
        return {'ok': False, 'error': 'invalid_combination',
                'detail': 'content and add_decision are mutually exclusive'}
    from lib.conversations.project_feed import normalize_project_path
    project_path = normalize_project_path(project_path)

    committed_decision = ''
    if add_decision:
        committed_decision = add_decision.strip()[:_DECISION_MAX_CHARS]

    try:
        db = get_thread_db(DOMAIN_CHAT)
        new_version = None
        for attempt in range(_CAS_MAX_ATTEMPTS):
            cur = read_charter(project_path)
            base_version = cur['version']

            if content is not None:
                if expected_version is not None and base_version != expected_version:
                    logger.info('[Charter] commit rejected (version skew) '
                                'proj=%.40r expected=%s current=%s',
                                project_path, expected_version, base_version)
                    return {'ok': False, 'error': 'version_conflict',
                            'current_version': base_version}
                new_content = (content or '')[:_CONTENT_MAX_CHARS]
                decisions = list(cur['decisions'])
            else:
                # Pure append: carry the CURRENT content through untouched.
                new_content = (cur['content'] or '')[:_CONTENT_MAX_CHARS]
                decisions = list(cur['decisions'])
                if committed_decision:
                    decisions.append(_decision_entry(
                        committed_decision, decision_kind=decision_kind,
                        summary=summary, updated_by_conv=updated_by_conv))
                    if len(decisions) > _MAX_DECISIONS:
                        decisions = decisions[-_MAX_DECISIONS:]

            if _cas_write(db, project_path, content=new_content,
                          decisions=decisions,
                          updated_by_conv=updated_by_conv,
                          base_version=base_version):
                new_version = base_version + 1
                break

            # Lost the race. An overwrite that pinned a version must NOT be
            # silently replayed onto the winner's text — report the skew.
            if content is not None and expected_version is not None:
                return {'ok': False, 'error': 'version_conflict',
                        'current_version': read_charter(project_path)['version']}
            logger.debug('[Charter] CAS miss proj=%.40r attempt=%d base=%s',
                         project_path, attempt + 1, base_version)
        else:
            logger.warning('[Charter] commit gave up after %d CAS attempts '
                           'proj=%.40r', _CAS_MAX_ATTEMPTS, project_path)
            return {'ok': False, 'error': 'contention',
                    'current_version': read_charter(project_path)['version']}
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


def _cas_write(db, project_path: str, *, content: str, decisions: list,
               updated_by_conv: str, base_version: int) -> bool:
    """Write the row ONLY if it is still at ``base_version``. Returns whether
    it landed.

    The version test lives in the WHERE clause, not in a preceding read: a
    read-then-compare only narrows the race window, it does not close it. This
    is the single place the charter row is written under contention.

    ``base_version == 0`` means "the row must not exist yet", so that case takes
    the INSERT branch whose PK conflict is itself the CAS-failure signal — a
    concurrent creator wins and we re-read rather than clobbering their row.
    """
    ts = int(time.time() * 1000)
    decisions_json = json.dumps(decisions, ensure_ascii=False)
    if base_version == 0:
        cur = db.execute(
            'INSERT INTO project_charter '
            '(project_path, content, decisions, updated_by_conv, updated_at, version) '
            'VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(project_path) DO NOTHING',
            (project_path, content, decisions_json,
             updated_by_conv or '', ts, 1))
    else:
        cur = db.execute(
            'UPDATE project_charter SET content=?, decisions=?, '
            'updated_by_conv=?, updated_at=?, version=? '
            'WHERE project_path=? AND version=?',
            (content, decisions_json, updated_by_conv or '', ts,
             base_version + 1, project_path, base_version))
    landed = (getattr(cur, 'rowcount', 0) or 0) > 0
    if landed:
        db.commit()
    return landed


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


# Sentinel distinguishing "caller said nothing about the summary" from "caller
# explicitly asked to clear it". `None` cannot carry that distinction, and the
# difference is load-bearing: see update_decision's omission semantics.
_SUMMARY_UNSET = object()


def update_decision(project_path: str, index: int, text: str, *,
                    summary=_SUMMARY_UNSET,
                    expected_version: int | None = None,
                    updated_by_conv: str = '') -> dict:
    """HUMAN-GATED edit of ONE committed decision, addressed by ``index``.

    The index is resolved against the CURRENT decisions list; ``expected_version``
    (when provided) must match the row version, so the caller is guaranteed the
    list is exactly what it rendered (the index can't silently address the wrong
    decision after a concurrent edit). Bumps ``version`` and emits a ``decided``
    event. Returns ``{'ok', 'version'?, 'error'?, 'current_version'?}``.

    **``summary`` is what agents actually read.** ``_decision_headline`` prefers
    the stored summary, and the per-turn injection renders ONLY that headline —
    the body is one ``project_charter_read`` call away. Until 2026-07-30 this
    function had no ``summary`` parameter at all, so a human correction rewrote
    the body, returned ok=True, bumped the version, and left the one line every
    sibling conversation reads unchanged FOREVER. Measured on the live project:
    decision #0's body described the shipped design while its summary was still
    broadcasting the design that design had replaced. The edit looked applied in
    the panel and was inert in the prompt — the same shape as a badge asserting
    something already untrue.

    Omission semantics, chosen deliberately:

    * entry HAS a summary and ``summary`` is omitted → **refused**
      (``summary_required``). A caller rewriting the rule's body without saying
      what the new rule line is has almost certainly hit the trap above. The two
      safe answers are refuse or clear; the unsafe one is to keep broadcasting
      the old line. Refusing is better than clearing because clearing silently
      downgrades a curated one-liner to an abridged first line — a quiet loss
      the human never asked for — whereas a refusal is a question they can
      answer. A refused edit changes NOTHING, not even the version.
    * ``summary=''`` → clear it, so the headline falls back to the fresh text.
      That is an explicit instruction, not an omission.
    * entry has NO summary and ``summary`` is omitted → edit proceeds. Legacy
      (pre-summary) entries already render an abridged first line; demanding a
      summary to touch them would make this a tax on every legacy edit instead
      of a trap for the stale-summary case.
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
        had_summary = bool(
            isinstance(d, dict) and (d.get('summary') or '').strip())
        if summary is _SUMMARY_UNSET and had_summary:
            # Refuse BEFORE any mutation: a rejected edit must leave the text,
            # the summary and the version exactly as they were.
            return {'ok': False, 'error': 'summary_required',
                    'current_version': cur['version'],
                    'current_summary': (d.get('summary') or '').strip()}
        if isinstance(d, dict):
            d = dict(d)
            d['text'] = text
            if summary is not _SUMMARY_UNSET:
                new_summary = (summary or '').strip()[:_SUMMARY_MAX_CHARS]
                if new_summary:
                    d['summary'] = new_summary
                else:
                    d.pop('summary', None)
            d['edited_by_conv'] = updated_by_conv or ''
            d['edited_at'] = int(time.time() * 1000)
        else:
            d = {'text': text, 'by_conv': updated_by_conv or '',
                 'ts': int(time.time() * 1000)}
            if summary is not _SUMMARY_UNSET:
                new_summary = (summary or '').strip()[:_SUMMARY_MAX_CHARS]
                if new_summary:
                    d['summary'] = new_summary
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


def render_charter_injection_block(project_path: str) -> str:
    """Render the charter for PER-TURN prompt injection: goal in full,
    decisions as a one-line headline list (summary when stored, abridged
    first line otherwise), with a pointer to project_charter_read for the
    full text of any entry.

    The model needs the RULE always resident, not the evidence chain — the
    1.5–2.2k-char measured-evidence narratives are read back on demand.
    Mirrors the board split (render_board_injection_block vs
    render_board_block). Returns '' when there is no charter.
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


def render_charter_block(project_path: str) -> str:
    """Render the charter with EVERY decision's complete stored text, never
    abridged. The per-turn prompt injection uses
    ``render_charter_injection_block`` instead; this full renderer backs the
    ``project_charter_read`` tool — the on-demand detail path the injection
    block points to.
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


def execute_charter_tool(fn_name: str, fn_args: dict, *,
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
            rec = read_charter(project_path)
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
                except (TypeError, ValueError):
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
            block = render_charter_injection_block(project_path)
            return (block + f'\n\n(charter version {rec["version"]}; pass '
                    'index=N for an entry\'s full text)')
        if fn_name == 'project_charter_propose':
            proposal = (fn_args.get('proposal') or '').strip()
            if not proposal:
                return 'Error: proposal text is required.'
            res = propose_amendment(
                project_path, current_conv_id, proposal,
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
