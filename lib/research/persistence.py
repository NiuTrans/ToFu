"""lib/research/persistence.py — durable home for auto-research artifacts.

WHY THIS MODULE EXISTS ()
-------------------------------------------------
Every R1–R4 artifact used to live ONLY in the in-process task dict of
``ProductionRuntime('research', ttl=7200)``. ``cleanup_stale()`` evicts
terminal tasks past TTL, so roughly two hours after a run finished — and
*instantly* on any server restart — the accepted ideas, the rejection audit
with its four-axis rubric scores, the survey markdown and the open-gap map
were gone for good.

That was never the design. ``docs/modules/ingest_media.md`` §5 places
these artifacts in ``paper_reports`` under composite ``lang`` keys, explicitly
noting it needs "not one line of new schema". Both key functions
(:func:`lib.paper.survey.survey_lang_key`, :func:`lib.paper.ideate.ideate_lang_key`)
were written and exported — and had ZERO callers in the entire tree, while the
sibling ``insight_lang_key`` had nineteen. The persistence layer was specified,
half-built, and never wired up. This module is the missing half.

DESIGN NOTES
------------
**One writer, not a second one.** Rows go through versioned Storage Sidecar
operations. The Sidecar owns the same legacy-compatible ``paper_reports``
shape on SQLite and PostgreSQL; this module never opens a database connection.

**Identity: a direction is not a paper.** ``paper_reports.paper_hash`` is
content-addressable for papers. A research *direction* is free text a human
retypes, so its hash is (a) case- and whitespace-normalised, so "  KV Cache "
and "kv cache" hit one row instead of paying for the pipeline twice, and
(b) NAMESPACED with a prefix, so a direction can never collide with the row of
a paper whose text happens to match.

**Where the structure goes.** ``report`` is the human-readable body and
``meta`` the JSON descriptor, per the established convention:

  * ``survey:<lang>`` — ``report`` = the survey markdown (naturally readable);
    ``meta`` carries the ``open_gaps`` map (a machine contract, not prose).
  * ``ideate:<lang>`` — the artifact is inherently structured (scored ideas +
    a rejection audit), so ``meta`` carries it in full. ``report`` gets a short
    human-readable digest so a reader opening the row directly is not met with
    raw JSON.

**Failure posture: never destroy an expensive artifact.** An ideate pass costs
many LLM calls. If the DB write fails, these functions log loudly and return
``False`` — the artifact still flows back to the caller and on to the user.
Persistence failing must never take down a run that already did the work.
"""

from __future__ import annotations

import hashlib
import time
import uuid

from lib.log import get_logger
from lib.identity import require_user_id

logger = get_logger(__name__)

__all__ = ['research_direction_hash', 'persist_survey', 'persist_ideate',
           'load_research_artifacts', 'list_research_directions']

#: Namespace prefix for direction identities. Keeps a direction's hash in a
#: different space from ``lib.paper_identity._paper_hash`` (paper CONTENT), so a
#: research row can never be written onto a real paper's report row.
_DIRECTION_NS = 'tofu-research-direction:v1:'


def research_direction_hash(direction) -> str:
    """Stable ``paper_reports.paper_hash`` identity for a research direction.

    Normalised (casefold + whitespace-collapsed) so the same direction retyped
    slightly differently resolves to one row — a miss here means re-running the
    entire harvest→survey→ideate pipeline at full cost. Namespaced so it never
    collides with a paper content hash. Returns ``''`` for empty input; every
    writer below refuses to write under a blank identity.
    """
    if not isinstance(direction, str):
        return ''
    norm = ' '.join(direction.split()).casefold()
    if not norm:
        return ''
    return hashlib.sha256(
        (_DIRECTION_NS + norm).encode('utf-8')).hexdigest()[:32]


def _upsert_row(phash: str, lang_key: str, report: str, meta: dict,
                model: str, *, user_id: int) -> None:
    """Write one row through the Sidecar's research artifact operation.

    Isolated as its own function so the failure posture above is testable
    (a guard patches this to raise and asserts the artifact still survives).
    """
    _storage(write=True).command('research.artifact.upsert', {
        'user_id': require_user_id(user_id, context='research artifact owner'),
        'paper_hash': phash,
        'lang_key': lang_key,
        'report': report or '',
        'model': model or '',
        'meta': meta,
        'created_at': int(time.time()),
    }, f'research.artifact.upsert:{uuid.uuid4().hex}')


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def persist_survey(direction: str, lang: str, survey_md: str, open_gaps: dict,
                   *, usage: dict | None = None, model: str = '',
                   user_id: int) -> bool:
    """Persist a survey + its open-gap map under ``survey:<lang>``.

    The gap map is R3's frozen input contract, so it is stored verbatim in
    ``meta`` rather than being re-derived from the prose later.

    Returns True on success; False (logged) on refusal or DB failure — never
    raises, so a storage problem cannot fail a completed survey stage.
    """
    from lib.paper.survey import survey_lang_key

    phash = research_direction_hash(direction)
    if not phash:
        logger.warning('[Research:Persist] refusing to persist a survey under '
                       'a blank direction identity')
        return False
    try:
        meta = {'kind': 'survey', 'direction': direction,
                'open_gaps': open_gaps or {}}
        if usage:
            meta['usage'] = usage
        _upsert_row(
            phash, survey_lang_key(lang), survey_md or '', meta, model,
            user_id=user_id)
    except Exception as e:
        logger.error('[Research:Persist] survey persist FAILED for %.60s: %s',
                     direction, e, exc_info=True)
        return False
    logger.info('[Research:Persist] survey stored — dir=%.60s key=%s hash=%s '
                '(%d chars, %d gaps)', direction, survey_lang_key(lang), phash,
                len(survey_md or ''),
                len((open_gaps or {}).get('open_gaps') or []))
    return True


def persist_ideate(direction: str, lang: str, artifact: dict, *,
                   model: str = '', user_id: int) -> bool:
    """Persist a scored-idea pass under ``ideate:<lang>``.

    The whole artifact — accepted ideas, the rejection audit WITH per-axis
    rubric scores, the threshold and ``gate_reached`` — is stored. The
    rejection audit is not incidental: it is the calibration data for
    ``IDEATE_GATE_THRESHOLD`` (currently a guessed 4.0), which the design doc
    names as the next milestone. Dropping it would forfeit that.
    """
    from lib.paper.ideate import ideate_lang_key

    phash = research_direction_hash(direction)
    if not phash:
        logger.warning('[Research:Persist] refusing to persist ideas under a '
                       'blank direction identity')
        return False
    art = artifact or {}
    accepted = art.get('accepted') or []
    rejected = art.get('rejected') or []
    meta = {'kind': 'ideate', 'direction': direction,
            'accepted': accepted, 'rejected': rejected,
            'threshold': art.get('threshold'),
            'gate_reached': art.get('gate_reached')}
    if isinstance(art.get('evaluation'), dict):
        meta['evaluation'] = art['evaluation']
    if art.get('usage'):
        meta['usage'] = art['usage']
    if art.get('degraded'):
        meta['degraded'] = True
        meta['degraded_reason'] = art.get('degraded_reason') or ''
    try:
        _upsert_row(phash, ideate_lang_key(lang), _ideate_digest(art), meta,
                    model, user_id=user_id)
    except Exception as e:
        logger.error('[Research:Persist] ideate persist FAILED for %.60s: %s',
                     direction, e, exc_info=True)
        return False
    logger.info('[Research:Persist] ideas stored — dir=%.60s key=%s hash=%s '
                '(%d accepted / %d rejected, gate=%s)', direction,
                ideate_lang_key(lang), phash, len(accepted), len(rejected),
                art.get('gate_reached'))
    return True


def _ideate_digest(artifact: dict) -> str:
    """A short human-readable body for the ``report`` column.

    The structured truth lives in ``meta``; this exists so anyone opening the
    row directly (psql, the report cache viewer) sees prose rather than raw
    JSON. Deliberately a DIGEST, never a second source of truth — no reader
    parses it back.
    """
    acc = artifact.get('accepted') or []
    rej = artifact.get('rejected') or []
    lines = [f'# Auto-research ideas ({len(acc)} accepted / {len(rej)} rejected)',
             '']
    for idea in acc:
        lines.append(f'## {idea.get("title") or "(untitled)"}')
        if idea.get('core_mechanism'):
            lines.append(f'**Mechanism:** {idea["core_mechanism"]}')
        if idea.get('overall') is not None:
            lines.append(f'**Score:** {idea["overall"]}')
        lines.append('')
    if not acc:
        lines.append('_No idea cleared the gate._')
    return '\n'.join(lines)


def list_research_directions(*, user_id: int, limit: int = 50) -> list:
    """Every direction that has ever been researched, newest first.

    WHY THIS IS NOT OPTIONAL. The persisted rows are keyed by
    ``sha256(normalised direction)[:32]`` — a ONE-WAY hash. Without an index a
    user had to reproduce their exact original wording to reach their own
    artifacts; forget it and the work is unreachable, which is
    indistinguishable from the TTL data loss this module was written to fix.
    The direction TEXT is already stored in ``meta.direction``, so recovering
    it costs nothing — only the read was missing.

    Rows are matched on the composite ``lang`` prefixes (``survey:`` /
    ``ideate:``) so per-paper reports sharing this table are never returned.
    The two rows of one run are folded into a single entry.

    Returns (never raises)::

        [{'direction', 'lang', 'created_at', 'accepted', 'rejected',
          'gate_reached', 'degraded', 'has_survey', 'has_ideas'}, ...]
    """
    try:
        try:
            normalized_limit = max(1, min(1000, int(limit or 50)))
        except (TypeError, ValueError) as e:
            logger.debug('[Research:Persist] invalid direction limit: %s', e)
            normalized_limit = 50
        rows = _storage().query(
            'research.directions.list', {
                'user_id': require_user_id(
                    user_id, context='research direction owner'),
                'limit': normalized_limit,
            })
    except Exception as e:
        logger.warning('[Research:Persist] list failed: %s', e)
        return []
    return rows if isinstance(rows, list) else []


def load_research_artifacts(
    direction: str, lang: str = 'en', *, user_id: int,
) -> dict:
    """Read back everything persisted for a direction.

    This is the re-attach path: it answers "has this direction been researched
    before?" WITHOUT a live task, which is what makes a finished job reachable
    after a TTL eviction or a restart.

    Returns (always a dict, never raises)::

        {'found': bool, 'direction': str, 'lang': str,
         'survey_md': str, 'open_gaps': dict,
         'accepted': list, 'rejected': list,
         'threshold': float|None, 'gate_reached': str,
         'degraded': bool, 'degraded_reason': str}
    """
    out = {'found': False, 'direction': direction, 'lang': lang,
           'survey_md': '', 'open_gaps': {}, 'accepted': [], 'rejected': [],
           'threshold': None, 'gate_reached': '', 'degraded': False,
           'degraded_reason': '', 'evaluation': {}, 'usage': {}}
    phash = research_direction_hash(direction)
    if not phash:
        return out
    try:
        rows = _storage().query('research.artifacts.get', {
            'user_id': require_user_id(
                user_id, context='research artifact owner'),
            'paper_hash': phash, 'lang': lang,
        })
    except Exception as e:
        logger.warning('[Research:Persist] lookup failed for %.60s: %s',
                       direction, e)
        return out

    stage_usage = {'survey': {}, 'ideate': {}, 'evaluate': None}
    for row in rows or []:
        meta = row.get('meta') if isinstance(row, dict) else None
        if not isinstance(meta, dict):
            continue
        out['found'] = True
        if meta.get('kind') == 'survey':
            out['survey_md'] = row.get('report') or ''
            out['open_gaps'] = meta.get('open_gaps') or {}
            stage_usage['survey'] = meta.get('usage') or {}
        elif meta.get('kind') == 'ideate':
            out['accepted'] = meta.get('accepted') or []
            out['rejected'] = meta.get('rejected') or []
            out['threshold'] = meta.get('threshold')
            out['gate_reached'] = meta.get('gate_reached') or ''
            out['degraded'] = bool(meta.get('degraded'))
            out['degraded_reason'] = meta.get('degraded_reason') or ''
            stage_usage['ideate'] = meta.get('usage') or {}
            if isinstance(meta.get('evaluation'), dict):
                out['evaluation'] = meta['evaluation']
                stage_usage['evaluate'] = meta['evaluation'].get('usage') or {}
    if any(stage_usage.values()):
        from lib.research.telemetry import aggregate_research_usage
        out['usage'] = aggregate_research_usage(stage_usage['survey'],
                                                 stage_usage['ideate'],
                                                 stage_usage['evaluate'])
    return out
