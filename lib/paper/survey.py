"""lib/paper/survey.py — multi-paper fan-in survey + open-gap map (R2).

The auto-research recipe's stage 3 (docs/modules/ingest_media.md §3 阶段 3):
take the local library that ``harvest`` (R1) built and synthesize it into

  1. a human-readable **survey markdown**, and
  2. a machine-readable **``open_gaps.json``** — the FROZEN contract that R3's
     anti-"A+B" novelty gate consumes (schema_version 1, defined in the design
     doc). ``open_gaps[].id`` is R3's handle on "does this idea solve a REAL
     gap", so the shape is locked here and evolves only by version bump.

Three owner-pinned invariants this module enforces
--------------------------------------------------
**Pin #1 — the open-gap map is machine-checked against the local library.**
A zero-LLM structural gate (:func:`_verify_against_library`) walks every arXiv
id the survey cites in ``clusters[].papers`` / ``method_matrix[].paper`` /
``open_gaps[].evidence`` and confirms each resolves to a row in
``paper_library`` (the shelf R1 built). An id that does NOT resolve is STRIPPED
from its entry — this is the recommend grounding gate run in reverse: not
grounding a *new* citation, but verifying that a paper the survey *claims to
cover* is actually in the shelf. If an ``open_gap``'s evidence is stripped
empty, the whole gap is dropped (default posture: a citation to a paper we
never harvested is treated as a model fabrication, not a crawl miss — a real
crawl miss is surfaced separately as ``missing_ids`` for a follow-up harvest,
never silently folded into the gap map R3 trusts).

**Pin #2 — inputs come from the library/reports, never a re-parse.** The survey
feeds the model each paper's ALREADY-GENERATED report (``paper_reports``, zero
cost when present) or, failing that, a truncated slice of ``paper_library``'s
stored ``parsed_text``. It NEVER calls ``parse_pdf`` and NEVER regenerates a
per-paper report. Each paper is capped at ``_SURVEY_PER_PAPER_CHARS`` so N
papers can't blow the context.

**Pin #3 — citations in the prose are audited by the existing tool.** The
survey markdown's inline ``arXiv:<id>`` identifiers go through
``lib.paper.citation_audit.build_citation_audit`` verbatim; a suspicious
(unresolvable) identifier surfaces a citation-integrity card in the result meta.

The dispatcher and tool executor are module dependencies, so tests can replace
the exact consumer bindings without a package facade or import-time registry.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

from lib.identity import require_user_id
from lib.llm.llm_errors import AbortedError
from lib.log import get_logger
from lib.paper.contracts import (
    PAPER_FANIN_MAX_PAPERS,
    PAPER_FANIN_MAX_TEXT_CHARS,
    PAPER_SURVEY_MAX_PAPERS,
    PAPER_SURVEY_WORKING_SET_PAPERS,
    PAPER_SURVEY_CONTEXT_CHARS,
    PAPER_SURVEY_EVIDENCE_SPAN_CHARS,
    PAPER_SURVEY_MAX_CLUSTERS,
    PAPER_SURVEY_MAX_GAPS,
    PAPER_SURVEY_MAX_GROUND_PROBES,
    PAPER_SURVEY_MAX_IDS_PER_ENTRY,
    PAPER_SURVEY_MAX_MATRIX_ROWS,
)

logger = get_logger(__name__)

__all__ = [
    'build_survey', 'survey_lang_key', 'OPEN_GAPS_SCHEMA_VERSION',
    '_verify_against_library', '_load_paper_inputs', '_extract_survey_ids',
    '_ensure_open_gap_ids',
]

# The frozen open_gaps.json schema version. R3 reads this; bump (never silently
# reshape) when the contract changes.
OPEN_GAPS_SCHEMA_VERSION = 1

# Per-paper input cap (chars) — keeps N papers inside the context window. A
# report is already distilled, so this is generous; raw parsed_text is the
# fallback and gets the same ceiling.
_SURVEY_PER_PAPER_CHARS = PAPER_FANIN_MAX_TEXT_CHARS

# How many library papers to feed the synthesis at most (a survey of hundreds
# is summarized from the most relevant slice, not the entire shelf).
_SURVEY_MAX_PAPERS = PAPER_SURVEY_MAX_PAPERS

_SURVEY_TEMPERATURE = 0.3      # below insight's 0.45 — survey is descriptive, not divergent
_SURVEY_MAX_TOKENS = 8000
_SURVEY_LANG_PREFIX = 'survey'
# Agentic survey rounds repeatedly carry a large, already-distilled corpus.
# This is a token envelope, not a round ceiling: after it is reached the next
# dispatch gets no tools and must synthesize from evidence already collected.
_SURVEY_AGENT_TOKEN_BUDGET = 1_000_000


def survey_lang_key(lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted survey.

    ``survey:<lang>`` — a separate row from plain reports / insights, mirroring
    Review Mode's ``review:<venue>:<uilang>`` and insight's ``insight:<lang>``.
    Lets a survey persist without ever overwriting a per-paper report.
    """
    return f'{_SURVEY_LANG_PREFIX}:{lang or "en"}'


# ── id normalization (shared discipline with recommend/_ground) ────────────

def _norm_id(arxiv_id) -> str:
    """Strip a version suffix so ``2502.09992v3`` and ``2502.09992`` compare equal.

    Mirrors ``recommend_engine._ground._norm_id`` exactly (kept local to avoid a
    cross-engine import on the survey cold path).

    TOTAL function (e2e fix, research_f12ab5e8): a real model emits dict-shaped
    entries in gap-map id lists (``{'id': …}`` / ``{'arxiv_id': …}`` /
    ``{'paper': …}``) — salvage the id from those keys and return '' for
    anything unparseable instead of crashing on ``.split``.
    """
    from lib.paper.arxiv import normalize_arxiv_id
    return normalize_arxiv_id(arxiv_id)


def _ensure_open_gap_ids(gap_map: dict) -> dict:
    """Return a copy whose retained gaps have unique deterministic handles.

    ``id`` is a cross-stage reference, not model-authored research content.
    Real providers occasionally return every substantive gap field but omit
    this mechanical handle. Repair missing or duplicate handles locally so a
    grounded survey cannot pass R2 and then fail immediately at R3.
    """
    if not isinstance(gap_map, dict):
        return gap_map
    out = dict(gap_map)
    raw_rows = gap_map.get('open_gaps')
    rows = raw_rows if isinstance(raw_rows, list) else []
    claimed = {
        row.get('id').strip()
        for row in rows
        if isinstance(row, dict) and isinstance(row.get('id'), str)
        and row.get('id').strip()
    }
    used = set()
    repaired = []
    generated = []
    next_id = 1
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        gap_id = item.get('id')
        gap_id = gap_id.strip() if isinstance(gap_id, str) else ''
        if not gap_id or gap_id in used:
            while f'gap_{next_id}' in claimed or f'gap_{next_id}' in used:
                next_id += 1
            gap_id = f'gap_{next_id}'
            next_id += 1
            generated.append(gap_id)
        item['id'] = gap_id
        used.add(gap_id)
        repaired.append(item)
    out['open_gaps'] = repaired
    if generated:
        out['generated_gap_ids'] = generated
        logger.info('[Paper:Survey] synthesized %d missing/duplicate gap handle(s): %s',
                    len(generated), ','.join(generated))
    return out


# ── Input loading (pin #2: reports/library only, never a re-parse) ─────────

def _load_paper_inputs(
    arxiv_ids,
    *,
    lang: str = 'en',
    user_id: int,
    per_paper_chars: int = _SURVEY_PER_PAPER_CHARS,
    max_papers: int = _SURVEY_MAX_PAPERS,
    abort=None,
) -> list:
    """Load existing reports or parsed text without reparsing any PDF."""
    from lib.paper.library_repository import PaperLibraryRepository

    user_id = require_user_id(user_id, context='paper survey input load')
    try:
        paper_limit = max(1, min(int(max_papers), _SURVEY_MAX_PAPERS))
        content_limit = max(1, min(int(per_paper_chars),
                                   _SURVEY_PER_PAPER_CHARS))
    except (TypeError, ValueError, OverflowError) as error:
        logger.warning('[Paper:Survey] invalid input budget: %s', error)
        return []
    requested_ids = []
    requested_seen = set()
    for index, raw in enumerate(arxiv_ids or ()):
        if index >= paper_limit:
            break
        normalized = _norm_id(raw)
        if not normalized or normalized in requested_seen:
            continue
        requested_seen.add(normalized)
        requested_ids.append(normalized)
        if len(requested_ids) >= paper_limit:
            break
    try:
        repository = PaperLibraryRepository(user_id)
        entries = []
        for start in range(0, len(requested_ids), PAPER_FANIN_MAX_PAPERS):
            if abort and abort():
                raise AbortedError('paper survey input load aborted')
            entries.extend(repository.by_arxiv_ids(
                requested_ids[start:start + PAPER_FANIN_MAX_PAPERS],
                max_text_chars=content_limit))
    except AbortedError:
        raise
    except Exception as error:
        logger.error(
            '[Paper:Survey] library input load failed: %s', error,
            exc_info=True,
        )
        return []

    entries_by_arxiv_id = {}
    for entry in entries:
        normalized = _norm_id(entry.arxiv_id)
        if normalized and entry.parsed_text:
            entries_by_arxiv_id.setdefault(normalized, entry)

    output = []
    seen = set()
    for raw in requested_ids:
        normalized = _norm_id(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if len(output) >= paper_limit:
            logger.info(
                '[Paper:Survey] input cap %d reached', paper_limit)
            break
        entry = entries_by_arxiv_id.get(normalized)
        if entry is None:
            logger.debug(
                '[Paper:Survey] %s not in library — skipped', normalized)
            continue

        # Reports are secondary model claims, never original-source evidence.
        content = entry.parsed_text
        source_kind = 'parsed_text'
        if not content.strip():
            continue
        output.append({
            'arxiv_id': normalized,
            'paper_hash': entry.paper_hash,
            'source_version': entry.arxiv_id,
            'parser_version': entry.parser_version,
            'source_total_chars': entry.parsed_text_length,
            'title': entry.title or f'arXiv:{normalized}',
            'source': source_kind,
            'content': content[:content_limit],
        })

    logger.info(
        '[Paper:Survey] loaded %d paper input(s) — %d reports, %d parsed text',
        len(output),
        sum(1 for paper in output if paper['source'] == 'report'),
        sum(1 for paper in output if paper['source'] == 'parsed_text'),
    )
    return output


# ── The library-verifiable structural gate (pin #1) ────────────────────────

def _library_id_set(candidate_ids=(), *, user_id: int,
                    folder_id: str = '') -> set:
    """Resolve only referenced arXiv ids against one owner's bookshelf."""
    from lib.paper.library_repository import PaperLibraryRepository

    user_id = require_user_id(user_id, context='paper survey library lookup')
    requested_ids = []
    requested_seen = set()
    for index, raw in enumerate(candidate_ids or ()):
        if index >= _SURVEY_MAX_PAPERS:
            break
        normalized = _norm_id(raw)
        if not normalized or normalized in requested_seen:
            continue
        requested_seen.add(normalized)
        requested_ids.append(normalized)
        if len(requested_ids) >= _SURVEY_MAX_PAPERS:
            break
    try:
        repository = PaperLibraryRepository(user_id)
        entries = []
        for start in range(0, len(requested_ids), PAPER_FANIN_MAX_PAPERS):
            entries.extend(repository.by_arxiv_ids(
                requested_ids[start:start + PAPER_FANIN_MAX_PAPERS]))
    except Exception as error:
        logger.error(
            '[Paper:Survey] library id-set query failed: %s',
            error,
            exc_info=True,
        )
        return set()

    ids = set()
    for entry in entries:
        if folder_id and entry.folder_id != folder_id:
            continue
        normalized = _norm_id(entry.arxiv_id)
        if normalized:
            ids.add(normalized)
    return ids


def _tier_ids(id_list, lib_ids: set, stripped: list, tiers: dict,
              ground_fn, ground_cache: dict, ground_state: dict,
              abort_check=None) -> list:
    """Classify each id into library / grounded / hallucinated (R2/R3 seam v2).

    Returns the kept ids (library + grounded). Records:
      * ``stripped`` (list): ids that are hallucinations (not in library, and
        ``ground_fn`` could not confirm them) — removed from the entry.
      * ``tiers`` (dict): id → 'library' | 'grounded' for every KEPT id.
      * ``ground_cache`` (dict): id → bool memo so a repeated id isn't
        re-grounded (one network probe per distinct id per verify pass).

    ``library`` (a pure DB-set hit) is checked first and is free; ``grounded``
    (``ground_fn`` confirms the paper exists) costs one title lookup and is the
    fallback that keeps a real-but-not-yet-harvested citation alive; anything
    else is a fabrication.
    """
    if not isinstance(id_list, (list, tuple)):
        return []
    if len(id_list) > PAPER_SURVEY_MAX_IDS_PER_ENTRY:
        ground_state['references_truncated'] += (
            len(id_list) - PAPER_SURVEY_MAX_IDS_PER_ENTRY)
    kept = []
    for raw in id_list[:PAPER_SURVEY_MAX_IDS_PER_ENTRY]:
        nid = _norm_id(raw)
        if not nid:
            # A dict entry with no salvageable id is an unverifiable CLAIM —
            # record it as stripped rather than silently skipping it.
            if isinstance(raw, dict):
                stripped.append(str(raw)[:80])
            continue
        if nid in lib_ids:
            kept.append(nid)
            tiers[nid] = 'library'
            continue
        # Not in the shelf — is it a real paper (grounded) or a hallucination?
        if nid not in ground_cache:
            if abort_check is not None and abort_check():
                raise AbortedError('paper survey grounding aborted')
            if ground_state['probes'] >= PAPER_SURVEY_MAX_GROUND_PROBES:
                ground_state['probes_suppressed'] += 1
                ground_cache[nid] = False
            else:
                ground_state['probes'] += 1
                try:
                    ground_cache[nid] = (
                        bool(ground_fn(nid)) if ground_fn else False)
                except Exception as error:
                    logger.debug(
                        '[Paper:Survey] grounding probe failed for %s: %s',
                        nid, error)
                    ground_cache[nid] = False
        if ground_cache[nid]:
            kept.append(nid)
            tiers[nid] = 'grounded'
        else:
            stripped.append(nid)
    return kept


def _verify_against_library(gap_map: dict, *, user_id: int,
                            folder_id: str = '', lib_ids: Optional[set] = None,
                            ground_fn=None, abort_check=None) -> dict:
    """Grade every arXiv id in an open-gap map into three tiers (R2/R3 seam v2).

    Walks ``clusters[].papers``, ``method_matrix[].paper``, and
    ``open_gaps[].evidence`` and classifies each id:
      * **library** — in ``paper_library`` (scoped to ``folder_id`` when given):
        the strongest evidence, a paper we actually harvested and read;
      * **grounded** — not in the shelf but ``ground_fn`` (arXiv title lookup)
        confirms it exists: kept, but the id is added to ``missing_ids`` as a
        follow-up-harvest signal;
      * **hallucination** — neither: stripped from the entry.

    Rules:
      * a ``cluster``'s ``papers`` keeps library+grounded ids;
      * a ``method_matrix`` row is dropped only if its paper is a hallucination
        (a real-but-unharvested paper row is kept);
      * an ``open_gap`` is DROPPED only when ALL its evidence is hallucination —
        a gap with any library OR grounded evidence survives. A gap whose kept
        evidence has ZERO ``library`` ids (grounded-only) is flagged
        ``low_confidence=True`` — it rests on papers we have not actually read,
        so R3 must discount it (see ideate).

    Returns a NEW dict with added meta:
      * ``stripped_ids`` — hallucinations removed (replay/debug);
      * ``missing_ids`` — grounded-but-unharvested ids to harvest next;
      * ``verification_budget`` — applied structure/probe ceilings and any
        truncation, so degraded model output remains observable;
      * per-gap: ``evidence_tiers`` / ``library_evidence_count`` /
        ``grounded_evidence_count`` / ``low_confidence``.

    With no ``ground_fn``, all candidates use one bounded batch request. Passing
    a custom function retains the per-id seam used by deterministic tests.
    """
    user_id = require_user_id(user_id, context='paper survey verification')
    if abort_check is not None and abort_check():
        raise AbortedError('paper survey verification aborted')
    if not isinstance(gap_map, dict):
        return {'schema_version': OPEN_GAPS_SCHEMA_VERSION, 'clusters': [],
                'method_matrix': [], 'open_gaps': [], 'stripped_ids': [],
                'missing_ids': []}
    if lib_ids is None:
        lib_ids = _library_id_set(
            _extract_survey_ids(gap_map), user_id=user_id,
            folder_id=folder_id)
    use_batch_grounding = ground_fn is None
    if use_batch_grounding:
        ground_fn = _fetch_arxiv_title

    out = dict(gap_map)
    out['schema_version'] = OPEN_GAPS_SCHEMA_VERSION
    stripped: list = []
    all_tiers: dict = {}      # id → 'library'|'grounded' across the whole map
    ground_cache: dict = {}   # id → bool, one probe per distinct id; bounded by construction (function-local per-run memo)
    ground_state = {
        'probes': 0,
        'probes_suppressed': 0,
        'references_truncated': 0,
        'batches': 0,
    }
    if use_batch_grounding:
        candidates = sorted(
            _extract_survey_ids(gap_map) - set(lib_ids)
        )[:PAPER_SURVEY_MAX_GROUND_PROBES]
        if candidates:
            if abort_check is not None and abort_check():
                raise AbortedError('paper survey grounding aborted')
            ground_state['probes'] = len(candidates)
            ground_state['batches'] = 1
            try:
                batch_titles = _fetch_arxiv_titles(candidates)
            except Exception as error:
                logger.warning(
                    '[Paper:Survey] batch grounding failed for %d id(s): %s',
                    len(candidates), error)
                batch_titles = {}
            if not isinstance(batch_titles, dict):
                batch_titles = {}
            ground_cache.update({
                arxiv_id: bool(batch_titles.get(arxiv_id))
                for arxiv_id in candidates
            })

    # clusters
    raw_clusters = gap_map.get('clusters')
    cluster_rows = raw_clusters if isinstance(raw_clusters, list) else []
    clusters = []
    for c in cluster_rows[:PAPER_SURVEY_MAX_CLUSTERS]:
        if not isinstance(c, dict):
            continue
        c2 = dict(c)
        c2['papers'] = _tier_ids(c.get('papers'), lib_ids, stripped, all_tiers,
                                 ground_fn, ground_cache, ground_state,
                                 abort_check)
        clusters.append(c2)
    out['clusters'] = clusters

    # method_matrix — a row IS a paper; drop only a hallucinated-paper row
    raw_matrix = gap_map.get('method_matrix')
    matrix_rows = raw_matrix if isinstance(raw_matrix, list) else []
    matrix = []
    for m in matrix_rows[:PAPER_SURVEY_MAX_MATRIX_ROWS]:
        if not isinstance(m, dict):
            continue
        kept = _tier_ids([m.get('paper')], lib_ids, stripped, all_tiers,
                         ground_fn, ground_cache, ground_state, abort_check)
        if kept:
            m2 = dict(m)
            m2['paper'] = kept[0]   # normalized bare id (salvaged if dict-shaped)
            matrix.append(m2)
    out['method_matrix'] = matrix

    # open_gaps — drop only when ALL evidence is hallucination; flag low_confidence
    raw_gaps = gap_map.get('open_gaps')
    gap_rows = raw_gaps if isinstance(raw_gaps, list) else []
    gaps = []
    dropped_gaps = 0
    for g in gap_rows[:PAPER_SURVEY_MAX_GAPS]:
        if not isinstance(g, dict):
            continue
        g2 = _normalize_gap(g)
        gap_tiers: dict = {}
        g2['evidence'] = _tier_ids(g.get('evidence'), lib_ids, stripped, gap_tiers,
                                   ground_fn, ground_cache, ground_state,
                                   abort_check)
        if not g2['evidence']:
            dropped_gaps += 1
            logger.info('[Paper:Survey] dropped hallucinated open_gap %s (all evidence '
                        'unresolvable): %.80s', g.get('id', '?'), g.get('gap', ''))
            continue
        lib_n = sum(1 for t in gap_tiers.values() if t == 'library')
        gnd_n = sum(1 for t in gap_tiers.values() if t == 'grounded')
        g2['evidence_tiers'] = {k: gap_tiers[k] for k in
                                {_norm_id(e) for e in g2['evidence']} if k in gap_tiers}
        g2['library_evidence_count'] = lib_n
        g2['grounded_evidence_count'] = gnd_n
        g2['reference_verification'] = {
            'support': 'identity_only',
            'closest_work': 'unverified',
            'counterevidence': 'unverified',
            'note': 'Preserved model claims; source existence does not establish entailment.',
        }
        g2['low_confidence'] = True  # identity verification is not entailment
        all_tiers.update(gap_tiers)
        if g2['low_confidence']:
            logger.info('[Paper:Survey] open_gap %s has unverified entailment '
                        '(%d grounded / %d library): %.60s', g.get('id', '?'),
                        gnd_n, lib_n, g.get('gap', ''))
        gaps.append(g2)
    out['open_gaps'] = gaps
    out = _ensure_open_gap_ids(out)

    # missing_ids = grounded (real but not yet in the shelf) → next harvest;
    # stripped_ids = hallucinations removed.
    missing = sorted({nid for nid, t in all_tiers.items() if t == 'grounded'})
    uniq_stripped = sorted(set(stripped))
    out['stripped_ids'] = uniq_stripped
    out['missing_ids'] = missing
    out['verification_budget'] = {
        'max_clusters': PAPER_SURVEY_MAX_CLUSTERS,
        'max_method_matrix_rows': PAPER_SURVEY_MAX_MATRIX_ROWS,
        'max_open_gaps': PAPER_SURVEY_MAX_GAPS,
        'max_ids_per_entry': PAPER_SURVEY_MAX_IDS_PER_ENTRY,
        'max_ground_probes': PAPER_SURVEY_MAX_GROUND_PROBES,
        'clusters_truncated': max(
            0, len(cluster_rows) - PAPER_SURVEY_MAX_CLUSTERS),
        'method_matrix_rows_truncated': max(
            0, len(matrix_rows) - PAPER_SURVEY_MAX_MATRIX_ROWS),
        'open_gaps_truncated': max(
            0, len(gap_rows) - PAPER_SURVEY_MAX_GAPS),
        'references_truncated': ground_state['references_truncated'],
        'ground_probes': ground_state['probes'],
        'ground_batches': ground_state['batches'],
        'ground_probes_suppressed': ground_state['probes_suppressed'],
    }
    if uniq_stripped or missing:
        logger.warning('[Paper:Survey] library gate — %d hallucination(s) stripped, '
                       '%d grounded-not-harvested → missing_ids, %d gap(s) dropped',
                       len(uniq_stripped), len(missing), dropped_gaps)
    return out


# ── Citation audit passthrough (pin #3) ────────────────────────────────────

def _audit_citations(survey_md: str) -> Optional[dict]:
    """Run the existing citation-hallucination audit on the survey prose.

    Verbatim reuse of ``lib.paper.citation_audit.build_citation_audit`` — returns
    a card payload only when a cited identifier is suspicious, else None."""
    try:
        from lib.paper.citation_audit import build_citation_audit
        return build_citation_audit(survey_md)
    except Exception as e:
        logger.warning('[Paper:Survey] citation audit failed: %s', e)
        return None


# ── LLM synthesis ──────────────────────────────────────────────────────────

def _parse_llm_json(content):
    from lib.llm.json_extract import extract_first_json_object
    return extract_first_json_object(content, log_prefix='[Paper:Survey]', log=logger)


def dispatch_stream(*args, **kwargs):
    """Dispatch one survey round; replace this consumer binding in tests."""
    from lib.llm_dispatch import dispatch_stream as _ds
    return _ds(*args, **kwargs)


def execute_paper_tool(*args, **kwargs):
    """Execute one survey research tool; replace this binding in tests."""
    from lib.paper.tools import execute_paper_tool as _ert
    return _ert(*args, **kwargs)


def _fetch_arxiv_title(arxiv_id):
    """Verify an arXiv title for the grounded-evidence tier.

    Patched by tests as ``survey._fetch_arxiv_title``. Returns '' when the id
    cannot be confirmed to exist (→ that id is a hallucination, stripped)."""
    try:
        from lib.paper.arxiv import fetch_arxiv_title as _ft
        return _ft(arxiv_id) or ''
    except Exception as e:
        logger.debug('[Paper:Survey] grounding title lookup failed for %s: %s', arxiv_id, e)
        return ''


def _fetch_arxiv_titles(arxiv_ids):
    """One-request grounding seam for the default survey verifier."""
    from lib.paper.arxiv import fetch_arxiv_titles_batch
    return fetch_arxiv_titles_batch(arxiv_ids)


def _survey_working_set(paper_inputs, direction, abort=None):
    """Rank bounded stored excerpts locally; never equate selection with full reading.

    Offsets and SHA256 refer to the exact loaded report/parsed-text snapshot,
    not to PDF bytes. Two spans preserve both relevance and limitation signals.
    """
    import re

    def terms_for(value):
        tokens = set(re.findall(r'[a-z0-9_-]{3,}', value.lower()))
        for segment in re.findall(r'[\u4e00-\u9fff]+', value):
            tokens.update(segment[i:i + 2] for i in range(len(segment) - 1))
        return tokens

    terms = terms_for(direction)
    ranked = []
    for paper in paper_inputs[:PAPER_SURVEY_MAX_PAPERS]:
        if abort and abort():
            raise AbortedError('paper survey retrieval aborted')
        text = paper.get('content', '')[:_SURVEY_PER_PAPER_CHARS]
        spans = []
        for start in range(0, len(text), PAPER_SURVEY_EVIDENCE_SPAN_CHARS):
            quote = text[start:start + PAPER_SURVEY_EVIDENCE_SPAN_CHARS]
            words = terms_for(quote)
            score = len(terms & words)
            caution = bool(words & {'however', 'limitation', 'limitations', 'fails',
                                    'failure', 'contrary', 'unlike', 'not',
                                    '然而', '局限', '失败', '不足', '限制', '相反'})
            spans.append((score, caution, start, quote))
        if not spans:
            continue
        relevant = max(spans, key=lambda s: (s[0], -s[2]))
        contrary = max(spans, key=lambda s: (s[1], s[0], -s[2]))
        chosen = sorted({s[2]: s for s in (relevant, contrary)}.values(),
                        key=lambda s: s[2])
        snapshot = hashlib.sha256(text.encode('utf-8')).hexdigest()
        cards = [{'arxiv_id': paper['arxiv_id'], 'source': paper.get('source', ''),
                  'paper_hash': paper.get('paper_hash', ''),
                  'source_version': paper.get('source_version', ''),
                  'snapshot_sha256': snapshot, 'start': s[2],
                  'end': s[2] + len(s[3]), 'quote': s[3],
                  'verification': 'snapshot_span_verified',
                  'entailment': 'unverified'} for s in chosen]
        item = dict(paper, evidence_cards=cards,
                    content='\n\n'.join(s[3] for s in chosen))
        title_score = len(terms & terms_for(paper.get('title', '')))
        ranked.append((relevant[0] + title_score, paper['arxiv_id'], item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    import json
    selected = []
    remaining = PAPER_SURVEY_CONTEXT_CHARS - 3000
    for _, _, item in ranked:
        cost = len(json.dumps(item['evidence_cards'], ensure_ascii=False)) + 400
        if cost > remaining:
            continue
        remaining -= cost
        selected.append(item)
        if len(selected) >= PAPER_SURVEY_WORKING_SET_PAPERS:
            break
    return selected


def _normalize_gap(row):
    """One compatibility seam for model field synonyms; uncertainty survives R3."""
    out = dict(row)
    aliases = {'gap': ('scoped_claim', 'claim', 'description', 'title'),
               'why_open': ('rationale', 'reason', 'explanation'),
               'closest_work': ('closest_prior_work', 'related_work'),
               'counterevidence': ('counter_evidence', 'contradictions'),
               'falsifier': ('falsification_test', 'disconfirming_test'),
               'unknowns': ('uncertainties',)}
    for field, alternatives in aliases.items():
        if not out.get(field):
            out[field] = next((out[key] for key in alternatives if out.get(key)),
                              [] if field in ('closest_work', 'counterevidence', 'unknowns') else '')
    out.setdefault('scoped_claim', out['gap'])
    # A library/title hit only establishes identity, never scientific entailment.
    out['entailment'] = 'unverified'
    out['confidence'] = 'low'
    out['low_confidence'] = True
    return out


def _synthesize_survey(paper_inputs, direction, lang, *, user_id, model=None,
                       abort=None, on_tool_event=None, usage_meter=None):
    """Agentic fan-in synthesis: research the frontier, then emit the survey.

    Returns ``(survey_md, gap_map)`` where ``gap_map`` is the RAW (un-gated)
    open_gaps dict the model produced; the caller runs the library gate on it.

    Mirrors ``insight_engine._synthesize._research_and_synthesize``: shared
    ``run_agent_loop`` with the narrow research profile at a low temperature.
    The model is asked to
    emit the markdown survey followed by a fenced ```json``` block carrying the
    open_gaps map; we split on the last JSON object.
    """
    from lib.agent_loop.agent_loop import AbortSignal
    from lib.paper.agent_loop_policy import (
        PAPER_AGENT_ROUTE_MAX_RETRIES,
        run_guarded_paper_agent_loop,
    )
    from lib.paper.agent_usage import PaperAgentUsageMeter
    from lib.paper.prompts import date_anchor_clause
    from lib.paper.tools import (
        PaperToolResultBudgetV2,
        build_research_tool_schemas,
        freeze_paper_tool_epoch,
        make_paper_exec_shim,
        make_research_tool_executor,
    )

    usage_meter = usage_meter or PaperAgentUsageMeter.for_stage(
        'survey', fallback_model=model or '')
    system = date_anchor_clause(lang) + _survey_system_prompt(lang)
    system += ('\nScope all conclusions to the supplied excerpts, never claim exhaustive reading. '
               'Each gap MUST contain gap, why_open, scoped_claim, closest_work, evidence, '
               'counterevidence, falsifier, unknowns, confidence. Preserve counterevidence even '
               'when it undermines the gap. Cite original arxiv_id and snapshot span cards. '
               'Source existence and a matching quote do NOT verify entailment or novelty. '
               'Use low confidence for missing source support; identify what would close the gap.')
    import json
    parts = [f'## RESEARCH DIRECTION\n\n{direction[:2000]}\n',
             '## SELECTED EXCERPTS ONLY (not full papers; not exhaustive coverage)\n']
    for i, p in enumerate(paper_inputs[:PAPER_SURVEY_WORKING_SET_PAPERS], 1):
        parts.append(f'### [{i}] {p["title"][:200]} (arXiv:{p["arxiv_id"]})\n'
                     + json.dumps(p.get('evidence_cards', []), ensure_ascii=False)
                     + ('\n' + p['content'][:1400] if not p.get('evidence_cards') else ''))
    user_content = '\n\n---\n\n'.join(parts)[:PAPER_SURVEY_CONTEXT_CHARS]

    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user_content}]
    abort_signal = AbortSignal.from_callback(abort)
    paper_tools, paper_contracts = freeze_paper_tool_epoch(
        build_research_tool_schemas(), owner_user_id=user_id)
    _exec_shim = make_paper_exec_shim(
        task_id=('paper-survey-' + hashlib.sha256(
            direction.encode('utf-8')).hexdigest()[:16]),
        abort=abort_signal.is_set, owner_user_id=user_id,
        tool_contract_documents_by_name=paper_contracts)
    _result_budget = PaperToolResultBudgetV2(
        owner_user_id=user_id, model=model or '')
    contracts_by_round = {}
    _round = {'content': ''}
    _last = {'msg': None}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            _round['content'] += text

        effective_tools, contracts_by_round[rnd] = freeze_paper_tool_epoch(
            tools, owner_user_id=user_id)
        logger.info('[Paper:Survey] round %d — msgs=%d tools=%s papers=%d',
                    rnd + 1, len(messages), 'yes' if effective_tools else 'no',
                    len(paper_inputs))
        from lib.llm.stream_result import ensure_provider_stream_result
        return ensure_provider_stream_result(dispatch_stream(
            messages, on_content=_on_content, abort_check=abort_signal.is_set,
            prefer_model=model or None, strict_model=bool(model), capability='text',
            tools=effective_tools, max_tokens=_SURVEY_MAX_TOKENS,
            temperature=_SURVEY_TEMPERATURE,
            thinking_enabled=False,
            max_retries=PAPER_AGENT_ROUTE_MAX_RETRIES,
            log_prefix='[Paper:Survey]'))

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg

    def _begin_tool_round(rnd, msg):
        _round['content'] = ''
        messages.append(msg)

    _execute_tool = make_research_tool_executor(
        messages, user_question=direction[:300], abort_signal=abort_signal,
        result_budget=_result_budget, exec_shim=_exec_shim,
        paper_tool_executor=execute_paper_tool, on_tool_event=on_tool_event,
        log_prefix='[Paper:Survey]',
        contract_documents_for_round=contracts_by_round.get)

    run_guarded_paper_agent_loop(
        context='Paper Survey agent',
        usage_meter=usage_meter,
        abort=abort_signal,
        round_tools=paper_tools, dispatch=_dispatch, execute_tool=_execute_tool,
        on_round_result=_on_round_result, on_tool_round=_begin_tool_round,
        on_round_end=_result_budget.finish_round)

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''

    gap_map = _parse_llm_json(content) or {}
    survey_md = _strip_trailing_json(content)
    return survey_md, gap_map


def _strip_trailing_json(content: str) -> str:
    """Return the markdown prefix, dropping a trailing fenced/bare JSON block.

    The model emits ``<survey markdown>`` then the open_gaps JSON; the human
    survey is everything before that final object. Falls back to the whole
    content if no JSON tail is found."""
    if not content:
        return ''
    # Prefer a fenced ```json ... ``` block boundary.
    fence = content.rfind('```json')
    if fence > 0:
        return content[:fence].rstrip()
    # else cut at the last top-level '{' that starts the trailing object
    brace = content.rfind('\n{')
    if brace > 0:
        return content[:brace].rstrip()
    return content.strip()


def _survey_system_prompt(lang: str) -> str:
    """System prompt for the fan-in survey (kept inline; R7 may move to a pack)."""
    zh = (lang or 'en').startswith('zh')
    if zh:
        return (
            '你是一位资深研究员,正在为一个方向撰写**相关工作综述 + 空白地图**。\n'
            '基于给定的论文片段完成两件事；片段不等于全文，必要时用工具核对原文和反证:\n'
            '1) 一份结构化的中文综述 markdown:按主题聚类描述已有工作、它们的共同假设与局限;'
            '引用具体论文时用 `arXiv:<id>` 内联标注(id 必须来自给定库内论文);\n'
            '2) 综述之后,追加一个 ```json 代码块,严格符合 open_gaps schema(schema_version=1):'
            'clusters / method_matrix / open_gaps 三部分。**所有 papers/paper/evidence 里的 arxiv_id '
            '必须是上面给定库内论文的 id,不得编造库外论文。**\n'
            'method_matrix 是必填比较表：上面每篇库内论文至少一行，字段为 '
            '`paper/method/assumptions/evaluation_tasks/budget/limitation`，并按研究方向补充比较维度；'
            '未知字段标记 unknown，不得猜测；不能用空数组。用这张表逐论文'
            '说明为何每个 gap 仍然存在，而不是只做串行摘要。\n'
            'open_gaps 是待证伪的局部研究判断，不是学界无人研究的证明；每条必须有唯一字符串 '
            'id(如 gap_1)、gap、why_open、scoped_claim、closest_work、counterevidence、'
            'falsifier、unknowns 和 evidence(相关论文 id)。检索未命中不能证明新颖性。'
            '可用 web 工具交叉核对,但空白地图的 id 只能来自库内。')
    return (
        'You are a senior researcher writing a **related-work survey + open-gap map** '
        'for a direction. The supplied excerpts are NOT full papers; use tools to inspect '
        'original passages and counterevidence when needed. Produce:\n'
        '1) a structured survey markdown: cluster prior work by theme, describe shared '
        'assumptions and limitations; cite specific papers inline as `arXiv:<id>` '
        '(ids MUST come from the given library papers);\n'
        '2) AFTER the survey, append one ```json code block strictly matching the open_gaps '
        'schema (schema_version=1): clusters / method_matrix / open_gaps. **Every arxiv_id in '
        'papers/paper/evidence MUST be an id of a given library paper — never invent papers '
        'outside the library.**\n'
        'method_matrix is a REQUIRED comparison table, with at least one row for EVERY given '
        'selected paper and these fields: `paper/method/assumptions/evaluation_tasks/budget/'
        'limitation`, plus domain-relevant comparison dimensions. Mark unknown values unknown. '
        'It may not be empty. Use it to explain, paper by paper, why each gap remains open '
        'rather than writing serial summaries.\n'
        'open_gaps are scoped falsifiable research claims, NOT proof that nobody has studied '
        'a topic. Each needs a unique string id (for example gap_1), gap, why_open, '
        'scoped_claim, closest_work, counterevidence, falsifier, unknowns and evidence '
        '(related library paper ids). Search absence does not prove novelty. '
        'Cross-check with web tools; the gap '
        "map's ids may only come from the library.")


# ── Public entry ───────────────────────────────────────────────────────────

def build_survey(direction: str, arxiv_ids, *, lang: str = 'en', user_id: int,
                 folder_id: str = '', model: Optional[str] = None,
                 abort: Optional[Callable[[], bool]] = None,
                 on_tool_event: Optional[Callable[[dict], None]] = None,
                 workdir=None, model_budget_admit=None) -> dict:
    """Produce a fan-in survey + library-verified open-gap map for a direction.

    Args:
        direction: the research direction being surveyed (free text).
        arxiv_ids: the library papers to synthesize (ids harvested in R1).
        lang: 'en' | 'zh' — survey language + the ``paper_reports`` lang used to
            find already-generated per-paper reports.
        user_id: owner scope.
        folder_id: the research task's library folder; scopes the verifiable-id
            set to this shelf when given (else the whole library).
        model / abort / on_tool_event: forwarded to the synthesis loop.

    Returns:
        {
          'ok': bool,
          'direction': str,
          'lang': str,
          'survey_md': str,                 # human-readable survey
          'open_gaps': { ...schema v1... }, # library-gated gap map (R3 input)
          'citation_audit': dict | None,    # suspicious-citation card, if any
          'inputs_used': int,               # papers actually fed to synthesis
          'error': str,                     # set when ok is False
        }
    """
    from lib.paper.agent_usage import resolve_paper_agent_budget
    from lib.research.telemetry import ResearchUsageMeter, research_token_budget

    from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
    if model_budget_admit is not None and not callable(model_budget_admit):
        raise ValueError('model_budget_admit must be callable')
    user_id = require_user_id(user_id, context='paper survey')
    direction = (direction or '').strip()
    usage_meter = ResearchUsageMeter(
        'survey', fallback_model=model or '',
        token_budget=research_token_budget(
            'TOFU_RESEARCH_SURVEY_TOKEN_BUDGET', _SURVEY_AGENT_TOKEN_BUDGET),
        dispatch_budget=resolve_paper_agent_budget(
            'TOFU_RESEARCH_SURVEY_DISPATCH_BUDGET', 32, minimum=2, maximum=32), repeat_limit=0)
    if not direction:
        return {'ok': False, 'error': 'empty direction', 'direction': '',
                'lang': lang, 'survey_md': '', 'open_gaps': {}, 'citation_audit': None,
                'inputs_used': 0, 'usage': usage_meter.snapshot()}

    try:
        inputs = _load_paper_inputs(arxiv_ids, lang=lang, user_id=user_id, abort=abort)
    except AbortedError:
        return {'ok': False, 'error': 'aborted', 'inputs_used': 0,
                'open_gaps': {}, 'survey_md': '', 'usage': usage_meter.snapshot()}
    if abort and abort():
        return {'ok': False, 'error': 'aborted', 'inputs_used': 0,
                'open_gaps': {}, 'survey_md': '', 'usage': usage_meter.snapshot()}
    if not inputs:
        logger.warning('[Paper:Survey] no library inputs for direction=%.80s (harvest first?)',
                       direction)
        return {'ok': False, 'error': 'no library papers to survey (run harvest first)',
                'direction': direction, 'lang': lang, 'survey_md': '',
                'open_gaps': {}, 'citation_audit': None, 'inputs_used': 0,
                'usage': usage_meter.snapshot()}

    corpus_inputs = inputs
    from lib.research.survey_hierarchy import BoundedCalls, map_reduce
    from lib.research.entailment import assess_gaps
    calls = BoundedCalls(dispatch_stream, _parse_llm_json, usage_meter,
                         direction=direction + '\n' + lang, user_id=user_id,
                         model=model, abort=abort, workdir=workdir,
                         model_budget_admit=model_budget_admit,
                         sources=[(p['arxiv_id'], p.get('source_version'),
                                   p.get('parser_version'),
                                   hashlib.sha256(p['content'].encode()).hexdigest())
                                  for p in corpus_inputs])
    try:
        survey_md, raw_gap_map, inputs, evidence_cards, batch_failures = map_reduce(
            corpus_inputs, direction, calls,
            lambda papers, query: _survey_working_set(papers, query, abort))
    except ModelBudgetAdmissionDenied:
        raise
    except Exception as e:
        if isinstance(e, AbortedError):
            logger.info('[Paper:Survey] aborted during synthesis')
            return {'ok': False, 'error': 'aborted', 'direction': direction, 'lang': lang,
                    'survey_md': '', 'open_gaps': {}, 'citation_audit': None,
                    'inputs_used': len(inputs), 'usage': usage_meter.snapshot()}
        logger.error('[Paper:Survey] synthesis failed: %s', e, exc_info=True)
        return {'ok': False, 'error': f'synthesis failed: {e}', 'direction': direction,
                'lang': lang, 'survey_md': '', 'open_gaps': {}, 'citation_audit': None,
                'inputs_used': len(inputs), 'usage': usage_meter.snapshot(),
                'batch_failures': getattr(e, 'failures', [])}

    # Pin #1 — library-verifiable structural gate (zero LLM).
    # The stage input list is the authoritative corpus. ``paper_library`` has
    # one mutable folder_id per paper, so concurrent/repeated research runs can
    # legitimately move/cache the same row under a different folder between
    # harvest and survey. Re-querying by folder here downgraded papers we had
    # just read to merely "grounded" (and made most gaps low-confidence).
    # Passing the exact loaded ids makes evidence provenance stable under that
    # race and matches the harvest→survey data contract.
    surveyed_ids = {_norm_id(p.get('arxiv_id')) for p in inputs if isinstance(p, dict)}
    surveyed_ids.discard('')
    try:
        gap_map = _verify_against_library(
            raw_gap_map, user_id=user_id, folder_id=folder_id,
            lib_ids=surveyed_ids, abort_check=abort)
    except AbortedError:
        logger.info('[Paper:Survey] aborted during evidence verification')
        return {
            'ok': False, 'error': 'aborted', 'direction': direction,
            'lang': lang, 'survey_md': '', 'open_gaps': {},
            'citation_audit': None, 'inputs_used': len(inputs),
            'usage': usage_meter.snapshot(),
        }
    gap_map.setdefault('schema_version', OPEN_GAPS_SCHEMA_VERSION)
    gap_map['direction'] = direction
    gap_map['lang'] = lang
    gap_map['library_folder_id'] = folder_id
    gap_map['surveyed_count'] = len(inputs)
    gap_map['surveyed_arxiv_ids'] = sorted(surveyed_ids)
    corpus_ids = {_norm_id(p['arxiv_id']) for p in corpus_inputs}
    gap_map['coverage'] = {
        'mode': 'hierarchical_all_admitted_excerpts', 'corpus_count': len(corpus_inputs),
        'complete': len(inputs) == len(corpus_inputs),
        'batch_failures': batch_failures, 'cache_hits': calls.cache_hits,
        'selected_count': len(inputs), 'selected_arxiv_ids': sorted(surveyed_ids),
        'unread_arxiv_ids': sorted(corpus_ids - surveyed_ids),
        'full_papers_read': 0, 'source_prefix_chars': _SURVEY_PER_PAPER_CHARS,
        'max_corpus_papers': PAPER_SURVEY_MAX_PAPERS,
        'max_working_set_papers': PAPER_SURVEY_WORKING_SET_PAPERS,
        'max_context_chars': PAPER_SURVEY_CONTEXT_CHARS,
    }
    gap_map['evidence_cards'] = evidence_cards
    try:
        assess_gaps(gap_map.get('open_gaps', []), evidence_cards,
                    {p['arxiv_id']: p for p in corpus_inputs}, calls)
    except AbortedError:
        return {'ok': False, 'error': 'aborted', 'inputs_used': len(inputs),
                'open_gaps': gap_map, 'survey_md': survey_md, 'usage': usage_meter.snapshot()}
    if batch_failures:
        for gap in gap_map.get('open_gaps', []):
            gap['assessment_incomplete'] = True
            gap['low_confidence'] = True
            if gap.get('status') != 'closed':
                gap['status'] = 'uncertain'
    matrix_ids = {_norm_id(row.get('paper')) for row in
                  (gap_map.get('method_matrix') or []) if isinstance(row, dict)}
    matrix_ids.discard('')
    covered = surveyed_ids & matrix_ids
    gap_map['method_matrix_coverage'] = {
        'covered': len(covered), 'total': len(surveyed_ids),
        'ratio': round(len(covered) / len(surveyed_ids), 3) if surveyed_ids else 0.0,
        'missing_arxiv_ids': sorted(surveyed_ids - covered),
    }

    # Pin #3 — citation audit on the prose.
    audit = _audit_citations(survey_md)

    logger.info('[Paper:Survey] done — direction=%.60s inputs=%d clusters=%d gaps=%d '
                'stripped=%d citation_suspicious=%s', direction, len(inputs),
                len(gap_map.get('clusters', [])), len(gap_map.get('open_gaps', [])),
                len(gap_map.get('stripped_ids', [])),
                bool(audit and audit.get('suspicious')))

    return {'ok': True, 'direction': direction, 'lang': lang, 'survey_md': survey_md,
            'open_gaps': gap_map, 'citation_audit': audit, 'inputs_used': len(inputs),
            'usage': usage_meter.snapshot(), 'error': ''}


def _extract_survey_ids(gap_map: dict) -> set:
    """All version-normalized arXiv ids referenced anywhere in a gap map.

    Convenience for callers/tests that want to assert every surfaced id is
    library-verifiable."""
    ids = set()
    if not isinstance(gap_map, dict):
        return ids
    raw_clusters = gap_map.get('clusters')
    clusters = raw_clusters if isinstance(raw_clusters, list) else []
    for c in clusters[:PAPER_SURVEY_MAX_CLUSTERS]:
        papers = c.get('papers') if isinstance(c, dict) else []
        papers = papers if isinstance(papers, list) else []
        for p in papers[:PAPER_SURVEY_MAX_IDS_PER_ENTRY]:
            ids.add(_norm_id(p))
    raw_matrix = gap_map.get('method_matrix')
    matrix = raw_matrix if isinstance(raw_matrix, list) else []
    for m in matrix[:PAPER_SURVEY_MAX_MATRIX_ROWS]:
        if isinstance(m, dict):
            ids.add(_norm_id(m.get('paper')))
    raw_gaps = gap_map.get('open_gaps')
    gaps = raw_gaps if isinstance(raw_gaps, list) else []
    for g in gaps[:PAPER_SURVEY_MAX_GAPS]:
        evidence = g.get('evidence') if isinstance(g, dict) else []
        evidence = evidence if isinstance(evidence, list) else []
        for e in evidence[:PAPER_SURVEY_MAX_IDS_PER_ENTRY]:
            ids.add(_norm_id(e))
    ids.discard('')
    return ids
