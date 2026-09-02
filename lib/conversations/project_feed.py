"""Authoritative project activity feed.

Entry points: :func:`emit_project_event` and :func:`read_project_feed`.
Storage is owned exclusively by the Sidecar ``project.feed`` operations;
this module validates presentation fields and publishes post-commit wake hints.
Every operation is keyed by an explicit normalized project path.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from lib.log import get_logger
from lib.storage import get_storage_client

logger = get_logger(__name__)

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

# The Sidecar ``project.feed.list`` operation accepts at most 200 rows.  Keep
# the public helper on that same contract in both storage modes so a default or
# caller-supplied large backfill cannot become a permanent protocol error.
_PROJECT_EVENTS_READ_MAX = 200

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


def emit_project_event(
    project_path: str,
    conv_id: str,
    kind: str,
    summary: str,
    *,
    user_id: int,
    task_id: str = '',
    title: str = '',
    payload: dict | None = None,
) -> dict | None:
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
        json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[ProjFeed] payload not serializable (using {}): %s', e)
        payload = {}

    try:
        event = get_storage_client(write=True).command(
            'project.feed.append', {
                'project_path': project_path,
                'user_id': int(user_id),
                'event': {
                    'event_id': event_id,
                    'conv_id': conv_id or '',
                    'task_id': task_id or '',
                    'kind': kind,
                    'title': title,
                    'summary': summary,
                    'payload': payload,
                    'ts': ts,
                },
                'keep': _PROJECT_EVENTS_KEEP,
            },
            f'project.feed:{int(user_id)}:{project_path}:{event_id}',
        )
    except Exception as e:
        logger.warning('[ProjFeed] emit failed kind=%s conv=%s: %s',
                       kind, (conv_id or '')[:8], e)
        return None
    # Mirror over the PushHub project channel, routed by the path-hash key so
    # the raw path never reaches a client. Best-effort: a push failure must
    # not undo the durable insert above.
    try:
        from lib.agent_core.push import push_event
        push_event(
            PROJECT_CHANNEL,
            project_channel_key(project_path),
            {'type': 'activity', 'event': event},
            user_id=int(user_id),
        )
    except Exception as e:
        logger.debug('[ProjFeed] push mirror failed (event persisted): %s', e)
    logger.debug('[ProjFeed] emitted kind=%s seq=%d conv=%s proj=%.40r',
                 kind, int(event.get('seq') or 0), (conv_id or '')[:8],
                 project_path)
    return event


def read_project_feed(
    project_path: str,
    *,
    user_id: int,
    since_seq: int = 0,
    limit: int = 100,
) -> dict:
    """Read recent events for ``project_path`` (REST backfill for the panel).

    Returns ``{'events': [...newest-first...], 'maxSeq': int}``. ``since_seq``
    filters to events with ``seq > since_seq`` (incremental fetch). Read-only;
    returns the empty shape on no project / DB error.
    """
    out = {'events': [], 'maxSeq': 0}
    if not project_path:
        return out
    project_path = normalize_project_path(project_path)
    limit = max(1, min(int(limit or 100), _PROJECT_EVENTS_READ_MAX))
    try:
        return get_storage_client().query(
            'project.feed.list', {
                'project_path': project_path,
                'user_id': int(user_id),
                'since_seq': int(since_seq or 0),
                'limit': limit,
            },
        )
    except Exception as e:
        logger.warning('[ProjFeed] read failed proj=%.40r: %s', project_path, e)
        return out


__all__ = [
    'emit_project_event', 'read_project_feed', 'project_channel_key',
    'normalize_project_path', 'VALID_KINDS', 'PROJECT_CHANNEL',
    '_PROJECT_EVENTS_KEEP', '_PROJECT_EVENTS_READ_MAX',
]
