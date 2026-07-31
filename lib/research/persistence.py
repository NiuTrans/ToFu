"""lib/research/persistence.py — durable home for auto-research artifacts.

WHY THIS MODULE EXISTS (epic pt_a40dbd9569194b52)
-------------------------------------------------
Every R1–R4 artifact used to live ONLY in the in-process task dict of
``ProductionRuntime('research', ttl=7200)``. ``cleanup_stale()`` evicts
terminal tasks past TTL, so roughly two hours after a run finished — and
*instantly* on any server restart — the accepted ideas, the rejection audit
with its four-axis rubric scores, the survey markdown and the open-gap map
were gone for good.

That was never the design. ``docs/AUTO_RESEARCH_SYSTEM_DESIGN.md`` §5 places
these artifacts in ``paper_reports`` under composite ``lang`` keys, explicitly
noting it needs "not one line of new schema". Both key functions
(:func:`lib.paper.survey.survey_lang_key`, :func:`lib.paper.ideate.ideate_lang_key`)
were written and exported — and had ZERO callers in the entire tree, while the
sibling ``insight_lang_key`` had nineteen. The persistence layer was specified,
half-built, and never wired up. This module is the missing half.

DESIGN NOTES
------------
**One writer, not a second one.** Rows go through
``lib.database._core_schema.upsert(db, PAPER_REPORTS, …)`` — the exact call
:func:`lib.paper.insight_engine._run._persist_insight` uses. Same schema, same
upsert semantics, same PG/SQLite bridge. No hand-rolled SQL lives here.

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
import json
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['research_direction_hash', 'persist_survey', 'persist_ideate',
           'load_research_artifacts', 'list_research_directions']

#: Namespace prefix for direction identities. Keeps a direction's hash in a
#: different space from ``lib.paper.hashing._paper_hash`` (paper CONTENT), so a
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
                model: str) -> None:
    """Write one row through the report engine's canonical upsert path.

    Isolated as its own function so the failure posture above is testable
    (a guard patches this to raise and asserts the artifact still survives).
    """
    from lib.database import get_thread_db
    from lib.database._core_schema import PAPER_REPORTS, upsert
    upsert(get_thread_db(), PAPER_REPORTS, {
        'paper_hash': phash,
        'lang': lang_key,
        'report': report or '',
        'model': model or '',
        'meta': json.dumps(meta, ensure_ascii=False),
        'created_at': int(time.time()),
    }, retry=True)


def persist_survey(direction: str, lang: str, survey_md: str, open_gaps: dict,
                   *, model: str = '') -> bool:
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
        _upsert_row(phash, survey_lang_key(lang), survey_md or '',
                    {'kind': 'survey', 'direction': direction,
                     'open_gaps': open_gaps or {}}, model)
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
                   model: str = '') -> bool:
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
    if art.get('degraded'):
        meta['degraded'] = True
        meta['degraded_reason'] = art.get('degraded_reason') or ''
    try:
        _upsert_row(phash, ideate_lang_key(lang), _ideate_digest(art), meta,
                    model)
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


def list_research_directions(limit: int = 50) -> list:
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
        from lib.database import get_thread_db
        rows = get_thread_db().execute(
            "SELECT lang, meta, created_at FROM paper_reports "
            "WHERE lang LIKE 'survey:%' OR lang LIKE 'ideate:%' "
            'ORDER BY created_at DESC').fetchall()
    except Exception as e:
        logger.warning('[Research:Persist] list failed: %s', e)
        return []

    folded: dict = {}
    for row in rows or []:
        try:
            meta = json.loads(row['meta'] or '{}')
        except Exception as e:
            logger.warning('[Research:Persist] unparseable meta on %s: %s',
                           row['lang'], e)
            continue
        direction = (meta.get('direction') or '').strip()
        if not direction:
            # Pre-contract row with no recorded text: it cannot be addressed
            # by a human, so listing it would only offer a dead link.
            continue
        lang_key = row['lang'] or ''
        lang = lang_key.split(':', 1)[1] if ':' in lang_key else 'en'
        entry = folded.setdefault((research_direction_hash(direction), lang), {
            'direction': direction, 'lang': lang,
            'created_at': int(row['created_at'] or 0),
            'accepted': 0, 'rejected': 0, 'gate_reached': '',
            'degraded': False, 'has_survey': False, 'has_ideas': False,
        })
        entry['created_at'] = max(entry['created_at'],
                                  int(row['created_at'] or 0))
        if meta.get('kind') == 'survey':
            entry['has_survey'] = True
        elif meta.get('kind') == 'ideate':
            entry['has_ideas'] = True
            entry['accepted'] = len(meta.get('accepted') or [])
            entry['rejected'] = len(meta.get('rejected') or [])
            entry['gate_reached'] = meta.get('gate_reached') or ''
            entry['degraded'] = bool(meta.get('degraded'))

    out = sorted(folded.values(), key=lambda e: e['created_at'], reverse=True)
    return out[:max(1, int(limit or 50))]


def load_research_artifacts(direction: str, lang: str = 'en') -> dict:
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
    from lib.paper.ideate import ideate_lang_key
    from lib.paper.survey import survey_lang_key

    out = {'found': False, 'direction': direction, 'lang': lang,
           'survey_md': '', 'open_gaps': {}, 'accepted': [], 'rejected': [],
           'threshold': None, 'gate_reached': '', 'degraded': False,
           'degraded_reason': ''}
    phash = research_direction_hash(direction)
    if not phash:
        return out
    try:
        from lib.database import get_thread_db
        db = get_thread_db()
        rows = db.execute(
            'SELECT lang, report, meta FROM paper_reports WHERE paper_hash=? '
            'AND lang IN (?, ?)',
            (phash, survey_lang_key(lang), ideate_lang_key(lang))).fetchall()
    except Exception as e:
        logger.warning('[Research:Persist] lookup failed for %.60s: %s',
                       direction, e)
        return out

    for row in rows or []:
        try:
            meta = json.loads(row['meta'] or '{}')
        except Exception as e:
            logger.warning('[Research:Persist] unparseable meta on %s: %s',
                           row['lang'], e)
            continue
        out['found'] = True
        if meta.get('kind') == 'survey':
            out['survey_md'] = row['report'] or ''
            out['open_gaps'] = meta.get('open_gaps') or {}
        elif meta.get('kind') == 'ideate':
            out['accepted'] = meta.get('accepted') or []
            out['rejected'] = meta.get('rejected') or []
            out['threshold'] = meta.get('threshold')
            out['gate_reached'] = meta.get('gate_reached') or ''
            out['degraded'] = bool(meta.get('degraded'))
            out['degraded_reason'] = meta.get('degraded_reason') or ''
    return out
