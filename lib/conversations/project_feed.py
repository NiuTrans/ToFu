"""lib.conversations.project_feed — cross-conversation activity feed.

Pillar #1 of the "project brain" (see ``docs/PROJECT_BRAIN.md``). Where
``project_summary.py`` gives ambient awareness of what sibling conversations
*accomplished* (finished summaries), this module gives a LIVE pulse of what is
*happening right now* across all conversations of one project: an append-only,
per-project event log mirrored over the ``PushHub`` so the frontend can render
a real-time "Activity" stream.

Design (locked by owner, 2026-06-30):

  • **Keyed on ``project_path``**, never a process-global. Every call addresses
    its project explicitly; this module holds NO project identity (mirrors the
    per-conv-roots discipline that avoided the global ``_state`` thrash trap).
  • **Monotonic per-project ``seq``** computed inside the insert under a single
    module lock, so two concurrent emitters can't collide on the
    ``(project_path, seq)`` PK. The lock guards only the DB write — it carries
    no project state.
  • **Best-effort, never raises into the caller.** A feed failure (DB down,
    bad payload) is logged at WARNING and swallowed — emitting an activity
    event must NEVER break the task that triggered it (§2.2 + the project's
    "audit logic must not block" rule).
  • **Bounded log.** On each emit, rows older than ``_PROJECT_EVENTS_KEEP`` per
    project are pruned (cheap ``seq``-window delete) so the table can't grow
    without bound — the same cost-ceiling stance as the digest.
  • **Path never goes on the wire.** The PushHub channel routing key is
    ``sha1(project_path)[:16]`` (``project_channel_key``), computed identically
    on backend and frontend, so an absolute filesystem path is never leaked
    into the channel namespace or a push frame (§3.5).

The only LIVE producers in Pillar #1 are task-lifecycle ``started`` /
``completed`` / ``aborted`` events (plus a ``run_concluded`` roll-up for
autopilot runs). The richer ``decided`` / ``proposed_decision`` / ``blocked``
kinds are accepted by the validator now but gain producers only in later
pillars (Charter / Board).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from lib.database import (
    DOMAIN_CHAT,
    allocate_scoped_sequence,
    get_thread_db,
    write_transaction,
)
from lib.log import get_logger

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # mirrors lib.conversations.project_summary

# The frozen set of event kinds. A kind outside this set is coerced to 'note'
# (never raises) so a typo in a future producer degrades to a generic row
# rather than a crash. 'claimed' joined the set with Pillar #3 (the Board claim
# path produces it); 'blocked'/'decided'/'proposed_decision' gained producers
# in Pillars #2 (Charter) and #3 (Board).
VALID_KINDS = frozenset({
    'started',
    'completed',
    'aborted',
    'run_concluded',
    'claimed',
    'blocked',
    'answered',
    'decided',
    'proposed_decision',
    'dismissed',
    'note',
})

# Retention: keep at most this many most-recent events per project. Older rows
# are pruned on emit. A bounded pulse, not an archive.
_PROJECT_EVENTS_KEEP = 500

# A short, one-line summary cap so a single event row stays cheap to ship and
# render (mirrors SUMMARY_MAX_CHARS' intent for the digest). This is the DISPLAY
# summary only — the UNtruncated text is preserved in payload['summary_full']
# (see emit_project_event) so the panel can expand a clamped row rather than
# losing the second half of a sentence mid-word (a data-loss bug).
_SUMMARY_MAX_CHARS = 280

# Ceiling for the preserved full summary. Generous (well above a 2000-char board
# title + a "Completed: …" prefix + a reason) so realistic feed summaries are
# kept verbatim, while a pathological multi-KB summary can't bloat every row.
_SUMMARY_FULL_MAX_CHARS = 4000

# PushHub channel for the project pulse (sibling of paper/translate/notify/chat).
PROJECT_CHANNEL = 'project'


import re as _re

# Trailing path-separator stripper. MUST match the frontend's
# `.replace(/[/\\]+$/, '')` byte-for-byte (project-brain.js `_displayedProjectPath`
# + presence.js `_norm`) so a write-side path and a read-side path canonicalise
# to the SAME storage key. Without this, an agent that writes a board/feed row
# under `/proj/x/` (a `conv.projectPath` that happened to carry a trailing
# slash) lands rows the panel — which reads the stripped `/proj/x` — can never
# find → the board/feed render EMPTY despite having data. This is the single
# canonical seam every project-brain read AND write funnels through.
_TRAILING_SEP_RE = _re.compile(r'[/\\]+$')


def normalize_project_path(project_path: str) -> str:
    """Canonicalise a project path for use as a project-brain storage key.

    Strips trailing ``/`` and ``\\`` (matching the frontend normalizer exactly)
    so the write side (agent tools / feed / presence) and the read side (panel /
    collab bar) always agree on the key. Falsy → ''. Never raises.
    """
    if not project_path:
        return ''
    return _TRAILING_SEP_RE.sub('', str(project_path))


def project_channel_key(project_path: str) -> str:
    """Stable 16-char routing key for a project's push channel.

    ``sha1(project_path)[:16]`` — computed identically on backend and the
    frontend subscriber so a tab subscribes to its own project's pulse only,
    WITHOUT ever putting the absolute filesystem path on the wire (§3.5).
    Returns '' for a falsy path (caller skips emission). The path is
    canonicalised first so a trailing-slash variant routes to the SAME channel.
    """
    if not project_path:
        return ''
    project_path = normalize_project_path(project_path)
    return hashlib.sha1(project_path.encode('utf-8', 'replace')).hexdigest()[:16]


def _coerce_kind(kind: str) -> str:
    """Map an arbitrary kind onto the frozen set; unknown → 'note'."""
    return kind if kind in VALID_KINDS else 'note'


def emit_project_event(project_path: str, conv_id: str, kind: str,
                       summary: str, *, task_id: str = '', title: str = '',
                       payload: dict | None = None) -> dict | None:
    """Append one activity event for ``project_path`` and mirror it live.

    Best-effort: any failure is logged at WARNING and swallowed (returns None)
    — this MUST NEVER raise into the task-lifecycle caller.

    Args:
        project_path: the project the event belongs to. Falsy → no-op (non
            -project conversations have no feed).
        conv_id: originating conversation id.
        kind: one of :data:`VALID_KINDS` (coerced to 'note' if unknown).
        summary: one-line human-readable "what happened" (length-capped).
        task_id: originating task id, if any.
        title: denormalized conversation title at emit time (so the frontend
            can render the row without a join).
        payload: kind-specific extra dict (json-serialized into the row).

    Returns:
        The inserted event dict (also the push-frame ``event`` body), or None
        on no-op / failure.
    """
    if not project_path:
        return None
    project_path = normalize_project_path(project_path)
    kind = _coerce_kind(kind or 'note')
    # DISPLAY summary is capped for a cheap row; but preserve the FULL text so
    # the panel can expand a clamped row instead of dropping the second half of
    # a sentence mid-word. The full text rides in payload['summary_full'] ONLY
    # when it actually exceeds the display cap (no redundant copy for the common
    # short summary). Never overwrites a caller-supplied payload['summary_full'].
    summary_full = (summary or '').strip()
    summary = summary_full[:_SUMMARY_MAX_CHARS]
    payload = dict(payload or {})
    if len(summary_full) > _SUMMARY_MAX_CHARS and 'summary_full' not in payload:
        payload['summary_full'] = summary_full[:_SUMMARY_FULL_MAX_CHARS]
    title = (title or '').strip()
    event_id = uuid.uuid4().hex
    ts = int(time.time() * 1000)
    try:
        payload_json = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[ProjFeed] payload not serializable (using {}): %s', e)
        payload_json = '{}'
        payload = {}

    try:
        db = get_thread_db(DOMAIN_CHAT)
        with write_transaction(db, label='project-feed-append'):
            seq = allocate_scoped_sequence(
                db, 'project_events', project_path)
            db.execute(
                'INSERT INTO project_events '
                '(project_path, seq, event_id, conv_id, task_id, kind, title, '
                ' summary, payload, ts) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (project_path, seq, event_id, conv_id or '', task_id or '',
                 kind, title, summary, payload_json, ts))
            # Retention: drop everything older than the keep-window for THIS
            # project (cheap seq-windowed delete; idx_project_events_path_ts
            # makes the read above an index scan).
            if seq > _PROJECT_EVENTS_KEEP:
                db.execute(
                    'DELETE FROM project_events '
                    'WHERE project_path=? AND seq <= ?',
                    (project_path, seq - _PROJECT_EVENTS_KEEP))
    except Exception as e:
        logger.warning('[ProjFeed] emit failed kind=%s conv=%s: %s',
                       kind, (conv_id or '')[:8], e)
        return None

    event = {
        'seq': seq, 'event_id': event_id, 'conv_id': conv_id or '',
        'task_id': task_id or '', 'kind': kind, 'title': title,
        'summary': summary, 'payload': payload, 'ts': ts,
    }
    # Mirror over the PushHub project channel, routed by the path-hash key so
    # the raw path never reaches a client. Best-effort: a push failure must
    # not undo the durable insert above.
    try:
        from lib.agent_core.push import push_event
        push_event(PROJECT_CHANNEL, project_channel_key(project_path),
                   {'type': 'activity', 'event': event})
    except Exception as e:
        logger.debug('[ProjFeed] push mirror failed (event persisted): %s', e)
    logger.debug('[ProjFeed] emitted kind=%s seq=%d conv=%s proj=%.40r',
                 kind, seq, (conv_id or '')[:8], project_path)
    return event


def read_project_feed(project_path: str, since_seq: int = 0,
                      limit: int = 100) -> dict:
    """Read recent events for ``project_path`` (REST backfill for the panel).

    Returns ``{'events': [...newest-first...], 'maxSeq': int}``. ``since_seq``
    filters to events with ``seq > since_seq`` (incremental fetch). Read-only;
    returns the empty shape on no project / DB error.
    """
    out = {'events': [], 'maxSeq': 0}
    if not project_path:
        return out
    project_path = normalize_project_path(project_path)
    limit = max(1, min(int(limit or 100), _PROJECT_EVENTS_KEEP))
    try:
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT project_path, seq, event_id, conv_id, task_id, kind, '
            '       title, summary, payload, ts '
            'FROM project_events WHERE project_path=? AND seq > ? '
            'ORDER BY seq DESC LIMIT ?',
            (project_path, int(since_seq or 0), limit)).fetchall()
    except Exception as e:
        logger.warning('[ProjFeed] read failed proj=%.40r: %s', project_path, e)
        return out
    events = []
    max_seq = 0
    for r in rows:
        try:
            payload = json.loads(r['payload']) if r['payload'] else {}
        except (TypeError, ValueError) as e:
            logger.debug('[ProjFeed] row payload parse failed (using {}): %s', e)
            payload = {}
        seq = int(r['seq'])
        max_seq = max(max_seq, seq)
        events.append({
            'seq': seq, 'event_id': r['event_id'], 'conv_id': r['conv_id'],
            'task_id': r['task_id'], 'kind': r['kind'], 'title': r['title'],
            'summary': r['summary'], 'payload': payload, 'ts': int(r['ts']),
        })
    out['events'] = events
    out['maxSeq'] = max_seq
    return out


__all__ = [
    'emit_project_event', 'read_project_feed', 'project_channel_key',
    'normalize_project_path', 'VALID_KINDS', 'PROJECT_CHANNEL',
    '_PROJECT_EVENTS_KEEP',
]
