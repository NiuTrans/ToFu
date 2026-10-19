"""lib/research/recipe.py — checkpointed research generation and publication.

The auto-research recipe's spine wires the three generative primitives
(harvest / survey / ideate) and an independent LLM evaluation pass into the production stage graph
(docs/modules/ingest_media.md §4), so a single ``produce_research`` call
runs the whole chain with checkpointed crash-resume — and each stage stays
independently callable.

Stages:  harvest → survey → ideate → evaluate → publish

This is the FOURTH capability on the production substrate (after motion-video /
paper-podcast / longform-report). It deliberately owns NO lifecycle machinery:
the stage-graph checkpoint (lib.production.stages), the crash-resume manifest
(lib.production.jobs) and the dedup/runtime layer (lib.production.runtime) are
all reused verbatim — the owner directive was "don't build a 4th duplicate
runtime". This file is the only place that knows anything about research.

Stage data contract (pinned — the owner's R4 requirement that stage boundaries
carry real schema, not in-memory globals):

  * harvest → ``{folder_id, arxiv_ids, harvested, cache_hits}``
      the library folder the papers landed in + the id list actually in the
      shelf. survey reads ``folder_id`` + ``arxiv_ids`` from here.
  * survey  → ``{open_gaps, survey_md, inputs_used}``
      the library-verified open-gap map (schema_version 1). ideate reads
      ``open_gaps`` from here — the exact frozen R2 contract.
  * ideate  → ``{accepted, rejected, threshold, gate_reached[, degraded,
      degraded_reason]}``
      scored ideas + the full rejection audit for threshold calibration.
      ``gate_reached`` records how deep the gate got; ``degraded`` marks a valid
      artifact produced by a SICK pipeline (e.g. the free structural gate wiped
      every idea, so nothing was ever judged) — it rides to the result body and
      makes the job settle as ``degraded`` instead of a clean ``done``.
  * evaluate → independent rubric scores from two judges, with a third
      tiebreaker only on material disagreement. This replaces subjective
      manual usefulness scoring with versioned, costed experiment evidence.
  * publish → one cheap, retryable terminal boundary that copies the validated
      survey and final evaluated idea artifact into their durable repository.
      Publication retries never re-run an upstream model stage.

Every seam into R1–R3 is resolved through this module (``_harvest_batch`` /
``_build_survey`` / ``_generate_ideas`` / ``_search_arxiv`` /
``_search_arxiv_via_web``) so a test patches
``recipe._build_survey`` etc. and drives the whole graph offline — the graph
wiring and crash-resume are provable with zero network and zero real LLM.
"""

from __future__ import annotations

import os
import time

from lib.identity import require_user_id
from lib.log import get_logger
from lib.llm.llm_errors import AbortedError
from lib.paper.contracts import PAPER_TITLE_HINT_MAX_CHARS
from lib.production.stages import Stage, StageAborted, run_stages
from lib.research.contracts import (
    DEFAULT_RESEARCH_HARVEST_PAPERS,
    normalize_discovered_arxiv_ids,
    normalize_research_harvest_count,
    normalize_research_idea_count,
    normalize_research_request,
    normalize_research_seed_arxiv_ids,
)

logger = get_logger(__name__)

__all__ = ['build_research_from_direction', 'research_recipe_stages']

#: How many arXiv papers to seed the harvest with when the caller gives no
#: explicit id list (derived from the direction via search).
_DEFAULT_HARVEST_N = DEFAULT_RESEARCH_HARVEST_PAPERS
#: A harvest must land at least this many papers or the survey has nothing to
#: fan in — the gate fails and the stage retries.
_MIN_HARVEST_PAPERS = 3
# A comparison matrix is the survey's cross-paper mechanism, not decoration.
# Permit one miss in a medium corpus, but never accept an empty/token matrix.
_MIN_METHOD_MATRIX_COVERAGE = 0.8


# ── Seams into R1–R3 (monkeypatchable, facade discipline) ──────────────────

def _search_arxiv(query, max_results=20):
    from lib.paper.arxiv import search_arxiv
    return search_arxiv(query, max_results=max_results)


_BUILTIN_ARXIV_SEARCH = _search_arxiv


def _discovery_search(ctx):
    """Explicit adapter seam; injected search functions retain their own I/O scope."""
    if _search_arxiv is not _BUILTIN_ARXIV_SEARCH:
        return _search_arxiv
    from lib.research.literature import default_discovery_adapter
    return default_discovery_adapter(
        _search_arxiv, abort_check=ctx.get('abort') or ctx.get('abort_check')).search


def _translate_direction_for_search(direction, *, abort_check=None, model_budget_admit=None):
    """Return an English arXiv query while preserving the user's direction.

    arXiv metadata is overwhelmingly English, whereas the research workbench
    invites directions in the active UI language.  Translate only the
    discovery alias; every downstream prompt and persisted identity continues
    to use the original direction.
    """
    from lib.text_lang import guess_language

    if guess_language(direction) not in ('zh', 'mixed'):
        return direction, {}

    from lib.translate import _build_translate_prompt, _translate_freetext

    translated, usage = _translate_freetext(
        direction,
        _build_translate_prompt('English'),
        chunk_label=':research-discovery',
        source='',
        target='English',
        overall_deadline=45,
        abort_check=abort_check,
        **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}),
    )
    query = ' '.join(str(translated or '').split())
    if not query:
        raise RuntimeError('direction translation returned an empty query')
    return query, usage or {}


#: Wall-clock budget for discovery translation to outlast a saturated
#: upstream rate window. UI-facing translation fails fast by design; a
#: background production stage must not die on a transient 429 storm.
_DISCOVERY_TRANSLATE_DEADLINE_S = 600.0


def _discovery_translate_deadline_s() -> float:
    raw = os.environ.get('TOFU_RESEARCH_DISCOVERY_TRANSLATE_DEADLINE_S', '')
    try:
        value = float(raw) if raw.strip() else _DISCOVERY_TRANSLATE_DEADLINE_S
    except ValueError:
        return _DISCOVERY_TRANSLATE_DEADLINE_S
    return max(30.0, min(value, 3600.0))


def _discovery_sleep(seconds: float, abort_check=None) -> None:
    """Sleep in 1s slices so an owner abort lands promptly."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if abort_check and abort_check():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _translate_direction_for_search_persistent(direction, *, abort_check=None, model_budget_admit=None):
    """429-resilient discovery translation — the agent-turn policy, ported.

    A chat turn survives a saturated provider window because turn_retry
    treats 'ratelimit' as transient: back off, wait out the window, re-run.
    The translate engine is UI-facing and instead dies once its small 429
    budget is spent. Discovery is a background stage with no user staring at
    a spinner, so it adopts the agent policy: budget exhaustion sleeps on the
    SAME schedule (turn_retry.auto_turn_backoff_seconds) and retries until
    the stage deadline. Non-429 failures propagate immediately.
    """
    from lib.llm_dispatch import DispatchRateLimitBudgetExceeded
    from lib.tasks_pkg.turn_retry import auto_turn_backoff_seconds

    budget = _discovery_translate_deadline_s()
    started = time.monotonic()
    attempt = 0
    while True:
        try:
            return _translate_direction_for_search(
                direction, abort_check=abort_check,
                **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}))
        except DispatchRateLimitBudgetExceeded:
            attempt += 1
            wait = auto_turn_backoff_seconds(attempt)
            if time.monotonic() - started + wait > budget:
                raise
            logger.info(
                '[Research:harvest] discovery translation 429-saturated; '
                'retry %d in %.0fs (budget %.0fs)', attempt + 1, wait, budget)
            _discovery_sleep(wait, abort_check)


def _search_arxiv_via_web(query, *, max_results, abort_check=None):
    """Discover arXiv ids through Tofu's built-in web search (tofu-search).

    The arXiv vertical indexes English metadata only; web engines accept the
    direction in its own language and surface arXiv links whose URLs
    normalize to bare ids for harvesting.
    """
    from lib.search.search_bridge import install_search_bridge
    install_search_bridge()
    from tofu_search.search.orchestrator import perform_web_search
    rows = perform_web_search(
        f'{query} arxiv', max_results=max_results,
        fetch_pages=False, filter_pages=False, rerank=False) or []
    from lib.paper.arxiv import normalize_arxiv_id
    ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        arxiv_id = normalize_arxiv_id(row.get('url'))
        if arxiv_id and arxiv_id not in ids:
            ids.append(arxiv_id)
    return ids


def _discover_search_query(ctx: dict) -> tuple:
    """Return ``(arxiv_query, usage)`` for the direction; ``''`` means skip.

    The arXiv vertical indexes English metadata, so a zh/mixed direction must
    be translated first — feeding the raw direction to ``all:`` matches
    nothing and zeroed the harvest (2026-09-04 incident: 16 translation 429s
    → raw-CJK query → 0 papers → gate rejected). When translation is
    unavailable there is NO vertical query; the caller falls through to web
    discovery. A failed translation stays uncached so a stage retry
    re-attempts it.
    """
    from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
    direction = ctx['direction']
    if '_research_search_query' in ctx:
        return (ctx['_research_search_query'],
                ctx.get('_research_search_usage') or {})
    from lib.text_lang import guess_language
    if guess_language(direction) not in ('zh', 'mixed'):
        return direction, {}
    try:
        query, raw_usage = _translate_direction_for_search_persistent(
            direction, abort_check=ctx.get('abort_check'),
            **({'model_budget_admit': ctx['model_budget_admit']} if ctx.get('model_budget_admit') is not None else {}))
    except ModelBudgetAdmissionDenied:
        raise
    except Exception as e:
        logger.warning(
            '[Research:harvest] discovery translation unavailable for %.60s; '
            'skipping the arXiv vertical, web search takes over: %s',
            direction, e)
        return '', {}
    from lib.research.telemetry import ResearchUsageMeter
    meter = ResearchUsageMeter(
        'harvest', token_budget=8_000, dispatch_budget=1, repeat_limit=0)
    if raw_usage:
        meter.record(raw_usage)
    usage = meter.snapshot() if raw_usage else {}
    ctx['_research_search_query'] = query
    ctx['_research_search_usage'] = usage
    if query != direction:
        logger.info(
            '[Research:harvest] translated discovery query %.60s → %.80s',
            direction, query)
    return query, usage


def _top_up_seed_via_web(seed, web_query, *, n, ctx):
    """Top up (or rescue) the seed list through Tofu's built-in web search.

    A thin or failed arXiv-vertical search must not zero the harvest — the
    shelf needs at least _MIN_HARVEST_PAPERS for the survey to fan in.
    """
    try:
        web_ids = _search_arxiv_via_web(
            web_query, max_results=max(n, _MIN_HARVEST_PAPERS * 3),
            abort_check=ctx.get('abort_check'))
    except Exception as e:
        logger.warning('[Research:harvest] web discovery failed for %.60s: %s',
                       ctx.get('direction', ''), e)
        return seed
    known = set(seed)
    added = [arxiv_id for arxiv_id in web_ids if arxiv_id not in known]
    if added:
        seed = list(seed) + added[:max(0, n - len(seed))]
        logger.info('[Research:harvest] web discovery added %d seed id(s) '
                    'via %.60s', len(added), web_query)
    return seed


def _harvest_batch(arxiv_ids, *, folder_id, user_id, abort_check=None,
                   on_progress=None, titles_by_arxiv_id=None):
    from lib.paper.harvest import harvest_arxiv_batch
    from lib.paper.contracts import PAPER_FANIN_MAX_PAPERS
    ids = list(normalize_research_seed_arxiv_ids(arxiv_ids))
    total = {'total': len(ids), 'parsed': 0, 'cache_hits': 0, 'errors': 0,
             'reparse_count': 0, 'degraded': 0, 'results': [], 'aborted': False}
    for offset in range(0, len(ids), PAPER_FANIN_MAX_PAPERS):
        if abort_check and abort_check():
            total['aborted'] = True
            break
        def progress(event, offset=offset):
            if on_progress:
                on_progress({**event, 'index': offset + event.get('index', 0),
                             'total': len(ids)})
        batch = harvest_arxiv_batch(
            ids[offset:offset + PAPER_FANIN_MAX_PAPERS], folder_id=folder_id,
            user_id=user_id, abort_check=abort_check, on_progress=progress,
            titles_by_arxiv_id=titles_by_arxiv_id)
        for key in ('parsed', 'cache_hits', 'errors', 'reparse_count', 'degraded'):
            total[key] += int(batch.get(key) or 0)
        total['results'].extend(batch.get('results') or [])
        if batch.get('aborted'):
            total['aborted'] = True
            break
    return total


def _build_survey(direction, arxiv_ids, *, lang, user_id, folder_id, model=None,
                  abort=None, on_tool_event=None, workdir=None, model_budget_admit=None):
    from lib.paper.survey import build_survey
    return build_survey(direction, arxiv_ids, lang=lang, user_id=user_id,
                        folder_id=folder_id, model=model, abort=abort,
                        on_tool_event=on_tool_event, workdir=workdir,
                        **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}))


def _generate_ideas(direction, open_gaps, *, lang, n_ideas, user_id,
                    model=None, abort=None, on_tool_event=None, revision_context=None, model_budget_admit=None):
    from lib.paper.ideate import generate_ideas
    return generate_ideas(direction, open_gaps, lang=lang, n_ideas=n_ideas,
                          user_id=user_id, model=model,
                          abort=abort, on_tool_event=on_tool_event,
                          revision_context=revision_context,
                          **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}))


def _evaluate_result(direction, result, *, model=None, abort=None, model_budget_admit=None):
    from lib.research.evaluation import evaluate_research_result
    return evaluate_research_result(direction, result, model=model, abort=abort,
        **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}))


def _persist_survey(
    direction, lang, survey_md, open_gaps, usage=None, *,
    harvest_usage=None, user_id: int,
):
    from lib.research.persistence import persist_survey
    return persist_survey(
        direction, lang, survey_md, open_gaps, usage=usage,
        harvest_usage=harvest_usage, user_id=user_id)


def _persist_ideate(direction, lang, artifact, *, user_id: int):
    from lib.research.persistence import persist_ideate
    return persist_ideate(direction, lang, artifact, user_id=user_id)


def _aggregate_usage(survey_usage: dict, ideate_usage: dict,
                     evaluate_usage: dict | None = None, *,
                     harvest_usage: dict | None = None) -> dict:
    """Fold stage snapshots without reimplementing per-call token pricing."""
    from lib.research.telemetry import aggregate_research_usage
    return aggregate_research_usage(
        survey_usage, ideate_usage, evaluate_usage,
        harvest_usage=harvest_usage)


# ── Stage: harvest ─────────────────────────────────────────────────────────

def _run_harvest(ctx: dict) -> dict:
    """Crawl related papers into the library, parse-once (R1).

    Seed ids come from ctx['seed_arxiv_ids'] when provided (R7's discover stage
    will fill these); otherwise they are derived from the direction via
    search_arxiv so the chain is runnable end-to-end today.
    """
    direction = ctx['direction']
    folder_id = ctx['folder_id']
    user_id = require_user_id(ctx.get('user_id'), context='research harvest stage')
    seed = list(normalize_research_seed_arxiv_ids(
        ctx.get('seed_arxiv_ids')))
    titles_by_arxiv_id = {}
    search_usage = {}
    discovery = {'stop_reason': 'explicit_seeds', 'discovered_count': len(seed),
                 'coverage_complete': False, 'rounds': []}
    if not seed:
        n = normalize_research_harvest_count(
            ctx.get('harvest_n', _DEFAULT_HARVEST_N))
        query, search_usage = _discover_search_query(ctx)
        if query:
            try:
                from lib.research.discovery import discover_corpus
                discovery = discover_corpus(
                    query, target=n, search=_discovery_search(ctx),
                    abort_check=ctx.get('abort_check'), on_progress=ctx.get('emit'))
                hits = discovery['papers']
                seed = list(normalize_discovered_arxiv_ids(
                    h.get('arxiv_id') for h in hits
                    if isinstance(h, dict) and h.get('arxiv_id')))
                seed_set = set(seed)
                from lib.paper.arxiv import normalize_arxiv_id
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    arxiv_id = normalize_arxiv_id(hit.get('arxiv_id'))
                    title = hit.get('title')
                    if (
                        arxiv_id in seed_set
                        and isinstance(title, str)
                        and title.strip()
                    ):
                        titles_by_arxiv_id.setdefault(
                            arxiv_id,
                            title.strip()[:PAPER_TITLE_HINT_MAX_CHARS],
                        )
            except Exception as e:
                logger.warning(
                    '[Research:harvest] seed search failed for %.60s: %s',
                    query, e)
                seed = []
        if len(seed) < _MIN_HARVEST_PAPERS:
            # The vertical came back thin/empty (or was skipped): Tofu's own
            # web search rescues the shelf before the gate ever sees it.
            seed = _top_up_seed_via_web(seed, query or direction, n=n, ctx=ctx)
    abort_check = ctx.get('abort_check')
    if abort_check and abort_check():
        from lib.llm.llm_errors import AbortedError
        raise AbortedError('research discovery aborted')
    from lib.research.records import literature_projection, merge_paper
    literature = literature_projection(discovery)
    if ctx.get('emit'):
        ctx['emit']({'type': 'literature_discovered', 'stage': 'harvest', **literature})
    metadata_by_arxiv = {p['arxivId']: p for p in literature['papers'] if p.get('arxivId')}
    counts = dict(discovered=discovery.get('discovered_count', len(seed)),
                  admitted=len(seed), parsed=0, cache_hits=0, failed=0)
    def progress(event):
        status = (event.get('result') or {}).get('status')
        if event.get('type') == 'paper_done':
            raw = event.get('result') or {}
            merged = [dict(metadata_by_arxiv[raw['arxivId']])] if raw.get('arxivId') in metadata_by_arxiv else []
            merge_paper(merged, raw)
            if merged:
                event = {**event, 'result': merged[0]}
        key = {'parsed': 'parsed', 'cache_hit': 'cache_hits', 'error': 'failed'}.get(status)
        if key:
            counts[key] += 1
        if ctx.get('emit'):
            ctx['emit']({**event, 'stage': 'harvest', 'counts': dict(counts)})
    progress({'type': 'corpus_admitted'})
    out = _harvest_batch(seed, folder_id=folder_id, user_id=user_id,
                         abort_check=abort_check,
                         titles_by_arxiv_id=titles_by_arxiv_id,
                         **({'on_progress': progress} if ctx.get('emit') else {}))
    # The ids actually in the shelf after this run (parsed this run OR already
    # cached) — this is what survey fans in.
    shelf_ids = [r['arxivId'] for r in (out.get('results') or [])
                 if r.get('status') in ('parsed', 'cache_hit') and r.get('arxivId')]
    logger.info('[Research:harvest] %.60s → %d in shelf (%d parsed, %d cache-hit) folder=%s',
                direction, len(shelf_ids), out.get('parsed', 0),
                out.get('cache_hits', 0), folder_id)
    return {'folder_id': folder_id, 'arxiv_ids': shelf_ids, 'admitted': len(seed),
            'harvested': out.get('parsed', 0), 'cache_hits': out.get('cache_hits', 0),
            'errors': out.get('errors', 0), 'usage': search_usage,
            'discovery': discovery}


def _gate_harvest(ctx: dict, art: dict) -> list:
    if len(art.get('arxiv_ids') or []) < _MIN_HARVEST_PAPERS:
        return [f'harvest landed only {len(art.get("arxiv_ids") or [])} paper(s) '
                f'(< {_MIN_HARVEST_PAPERS}); the survey needs a corpus to fan in']
    return []


# ── Stage: survey ──────────────────────────────────────────────────────────

def _run_survey(ctx: dict) -> dict:
    """Fan-in survey + library-verified open-gap map (R2).

    Reads the harvest stage's folder_id + arxiv_ids from the checkpointed
    artifact — the stage data contract, not an in-memory global.
    """
    h = ctx['artifacts']['harvest']
    user_id = require_user_id(ctx.get('user_id'), context='research survey stage')
    kwargs = dict(lang=ctx.get('lang', 'en'), user_id=user_id,
                  folder_id=h['folder_id'], abort=ctx.get('abort'))
    if ctx.get('model'):
        kwargs['model'] = ctx['model']
    # Keep the monkeypatch seam backward-compatible for offline graph tests;
    # real jobs always carry the stage runner's event sink.
    if ctx.get('emit') is not None:
        kwargs['on_tool_event'] = ctx['emit']
    if ctx.get('workdir'):
        kwargs['workdir'] = ctx['workdir']
    if ctx.get('model_budget_admit') is not None:
        kwargs['model_budget_admit'] = ctx['model_budget_admit']
    res = _build_survey(ctx['direction'], h['arxiv_ids'], **kwargs)

    if isinstance(res.get('open_gaps'), dict) and h.get('discovery'):
        res['open_gaps']['discovery'] = h['discovery']
    if not res.get('ok'):
        # Surface as an exception so the stage retries / fails loudly.

        if res.get('batch_failures') and ctx.get('emit'):
            from lib.research.records import survey_failure_projection
            ctx['emit']({'type': 'survey_map_failed', 'stage': 'survey',
                         'survey_batch_failures': survey_failure_projection(res['batch_failures'])})
        raise RuntimeError(f'survey failed: {res.get("error")}')
    art = {'open_gaps': res['open_gaps'], 'survey_md': res['survey_md'],
           'inputs_used': res.get('inputs_used', 0),
           'citation_audit': res.get('citation_audit'),
           'usage': res.get('usage') or {}}
    return art


def _gate_survey(ctx: dict, art: dict) -> list:
    gm = art.get('open_gaps') or {}
    if not gm.get('open_gaps'):
        return ['survey produced no open gaps — nothing for ideate to solve']
    usable_gap_ids = {
        row.get('id').strip()
        for row in (gm.get('open_gaps') or [])
        if isinstance(row, dict) and isinstance(row.get('id'), str)
        and row.get('id').strip()
    }
    if not usable_gap_ids:
        return ['survey produced no usable open-gap ids — ideate cannot link '
                'ideas to verified gaps']
    surveyed = {str(value).strip() for value in
                (gm.get('surveyed_arxiv_ids') or []) if str(value).strip()}
    if surveyed:
        import math
        from lib.paper.arxiv import normalize_arxiv_id
        surveyed = {normalize_arxiv_id(value) for value in surveyed}
        covered = {normalize_arxiv_id(row.get('paper')) for row in
                   (gm.get('method_matrix') or []) if isinstance(row, dict)}
        covered.discard('')
        count = len(surveyed & covered)
        required = max(1, math.ceil(len(surveyed) * _MIN_METHOD_MATRIX_COVERAGE))
        if count < required:
            return [f'method_matrix covers only {count}/{len(surveyed)} surveyed '
                    f'paper(s); at least {required} are required for cross-paper synthesis']
    return []


# ── Stage: ideate ──────────────────────────────────────────────────────────

def _run_ideate(ctx: dict) -> dict:
    """Anti-A+B idea generation + gate (R3).

    Reads the survey stage's open_gaps (the frozen R2 contract) from the
    checkpointed artifact.
    """
    s = ctx['artifacts']['survey']
    user_id = require_user_id(ctx.get('user_id'), context='research ideate stage')
    kwargs = dict(lang=ctx.get('lang', 'en'),
                  n_ideas=normalize_research_idea_count(ctx.get('n_ideas')),
                  user_id=user_id, abort=ctx.get('abort'))
    if ctx.get('model'):
        kwargs['model'] = ctx['model']
    if ctx.get('emit') is not None:
        kwargs['on_tool_event'] = ctx['emit']
    if ctx.get('model_budget_admit') is not None:
        kwargs['model_budget_admit'] = ctx['model_budget_admit']
    res = _generate_ideas(ctx['direction'], s['open_gaps'], **kwargs)
    if not res.get('ok'):
        raise RuntimeError(f'ideate failed: {res.get("error")}')
    art = {'accepted': res['accepted'], 'rejected': res['rejected'],
           'threshold': res['threshold'],
           'usage': res.get('usage') or {},
           # How deep the gate actually got ('accepted' | 'rubric' | 'structural' |
           # 'none') — a bare accepted-count cannot tell an honest 宁缺毋滥 zero
           # from a pipeline that killed everything before judging.
           'gate_reached': res.get('gate_reached')}

    for key in ('candidate_audit', 'repair_records'):
        if key in res:
            art[key] = res[key]
    # A degraded pass produced a VALID artifact from a SICK pipeline. It must
    # cross the stage boundary (and be committed to the checkpoint) or the
    # symptom reaching the user is an indistinguishable 'done, accepted 0' —
    # the flag is only worth anything if it survives to the result body.
    if res.get('degraded'):
        art['degraded'] = True
        art['degraded_reason'] = res.get('degraded_reason') or 'pipeline degraded'
        logger.error('[Research:ideate] DEGRADED — %s', art['degraded_reason'])
    return art


def _gate_ideate(ctx: dict, art: dict) -> list:
    # 宁缺毋滥 — accepting ZERO ideas is a valid (honest) outcome, NOT a gate
    # failure. But the pass must have RUN the gate (produced a rejection audit
    # or an acceptance); a run that produced neither is a real failure.
    if not art.get('accepted') and not art.get('rejected'):
        return ['ideate produced neither accepted nor rejected ideas — the gate '
                'never ran']
    return []


# ── Stage: evaluate ────────────────────────────────────────────────────────

def _review_once(ctx: dict) -> dict:
    """Score the frozen survey/idea artifact without mutating its gate.

    Evaluation is observability, not another source of expensive artifact
    loss. Any unexpected judge/provider failure is committed as an explicit
    degraded evaluation so harvest/survey/ideate remain resumable and visible.
    """
    from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
    harvest = ctx['artifacts']['harvest']
    survey = ctx['artifacts']['survey']
    ideate = ctx['artifacts']['ideate']
    frozen = {
        'corpus_arxiv_ids': list(harvest.get('arxiv_ids') or []),
        'survey_md': survey.get('survey_md') or '',
        'open_gaps': survey.get('open_gaps') or {},
        'accepted': list(ideate.get('accepted') or []),
        'rejected': list(ideate.get('rejected') or []),
        'threshold': ideate.get('threshold'),
        'gate_reached': ideate.get('gate_reached'),
        'revision_issues': ctx.get('revision_issues') or [],
        'retired': list(ideate.get('retired') or []),
        'survey_version': survey.get('survey_version', 1),
    }
    try:

        evaluation = _evaluate_result(
            ctx['direction'], frozen,
            model=ctx.get('evaluation_model') or ctx.get('model'),
            abort=ctx.get('abort'),
            **({'model_budget_admit': ctx['model_budget_admit']}
               if ctx.get('model_budget_admit') is not None else {}))
        if not isinstance(evaluation, dict):
            raise TypeError('evaluator returned a non-object result')
    except (StageAborted, ModelBudgetAdmissionDenied):
        raise
    except Exception as exc:
        logger.error('[Research:evaluate] evaluator unavailable: %s', exc,
                     exc_info=True)
        evaluation = {
            'ok': False, 'schema_version': 1, 'judge_count': 0,
            'attempted_judges': 0, 'scores': {}, 'overall_score': None,
            'worth_following_up': False, 'consensus': 'unavailable',
            'strengths': [], 'failure_modes': [], 'recommended_changes': [],
            'verdict': '', 'judges': [],
            'errors': [f'{type(exc).__name__}: {exc}'],
            'degraded': True,
            'degraded_reason': 'LLM evaluation unavailable',
            'usage': {}, 'tiebreaker_used': False,
        }

    return evaluation


def _targeted_retrieval(ctx, state, index):
    """Bounded critique-directed search/harvest; no global rediscovery loop.

    Root may replace this seam with citation-aware retrieval. Return
    {ok, harvest, usage, error?}; harvest must contain only verified shelf IDs.
    """
    from lib.research.revision import actionable
    issues = [i for i in state['research_cycle']['issues']
              if actionable(i) and i['target'] == 'retrieval']
    ids = []
    search = _discovery_search(ctx)
    for issue in issues[:3]:

        if ctx.get('abort') and ctx['abort']():
            raise StageAborted('targeted retrieval aborted')
        query = f"{ctx['direction']} {issue['change']} {issue['evidence']}"[:1400]
        hits = search(query, max_results=6) or []
        ids.extend(h.get('arxiv_id') for h in hits if isinstance(h, dict))
    ids = list(normalize_discovered_arxiv_ids(ids))[:12]
    prior = state['harvest']
    from lib.paper.contracts import PAPER_SURVEY_MAX_PAPERS
    remaining = max(0, PAPER_SURVEY_MAX_PAPERS - len(prior.get('arxiv_ids', [])))
    ids = [i for i in ids if i not in prior.get('arxiv_ids', [])][:remaining]
    if not ids:
        return {'ok': False, 'error': 'targeted retrieval found no new verifiable evidence', 'usage': {}}
    result = _harvest_batch(ids, folder_id=prior['folder_id'],
                            user_id=ctx['user_id'], abort_check=ctx.get('abort'))
    if result.get('aborted'):
        raise StageAborted('targeted harvest aborted')
    verified = [r['arxivId'] for r in result.get('results', [])
                if isinstance(r, dict) and r.get('status') in ('parsed', 'cache_hit')
                and r.get('arxivId') in ids]
    if not verified:
        return {'ok': False, 'error': 'targeted papers could not be verified',
                'usage': result.get('usage') or {}}
    harvest = {**prior, 'arxiv_ids': list(dict.fromkeys(prior['arxiv_ids'] + verified)),
               'harvested': prior.get('harvested', 0) + result.get('parsed', 0),
               'cache_hits': prior.get('cache_hits', 0) + result.get('cache_hits', 0)}
    return {'ok': True, 'harvest': harvest, 'usage': result.get('usage') or {}}


def _run_evaluate(ctx: dict) -> dict:
    """Critique, gated revision, and re-review with durable paid-phase resume."""
    from copy import deepcopy
    from lib.research.revision import run_revision_cycle

    def review(ideas, issues, survey, harvest):
        local = {**ctx, 'artifacts': {**ctx['artifacts'], 'ideate': ideas,
                                     'survey': survey, 'harvest': harvest},
                 'revision_issues': issues}
        return _review_once(local)

    def revise(context):
        kwargs = dict(lang=ctx.get('lang', 'en'),
                      n_ideas=normalize_research_idea_count(ctx.get('n_ideas')),
                      user_id=require_user_id(ctx.get('user_id'), context='research revision'),
                      abort=ctx.get('abort'), revision_context=context)
        if ctx.get('model'):
            kwargs['model'] = ctx['model']
        if ctx.get('emit') is not None:
            kwargs['on_tool_event'] = ctx['emit']
        if ctx.get('model_budget_admit') is not None:
            kwargs['model_budget_admit'] = ctx['model_budget_admit']
        return _generate_ideas(ctx['direction'],
                               context.get('current_survey', ctx['artifacts']['survey'])['open_gaps'], **kwargs)

    def retrieve(state, index):
        return _targeted_retrieval(ctx, state, index)

    def rebuild(state, index):
        local = {**ctx, 'artifacts': {**ctx['artifacts'], 'harvest': state['harvest']}}
        survey = _run_survey(local)
        # Zero open gaps is a legitimate closure, not a reason to invent ideas.
        errors = _gate_survey(local, survey) if survey['open_gaps'].get('open_gaps') else []
        return {'ok': not errors, 'error': '; '.join(errors),
                'survey': survey, 'usage': survey.get('usage') or {}}

    state = run_revision_cycle(
        ctx['artifacts']['ideate'], review=review, revise=revise,
        survey=ctx['artifacts']['survey'], harvest=ctx['artifacts']['harvest'],
        retrieve=retrieve, rebuild=rebuild,
        gate=lambda art: _gate_ideate(ctx, art), abort=ctx.get('abort'),
        workdir=ctx.get('workdir'),
        identity=[ctx['direction'], ctx['artifacts']['survey'],
                  ctx.get('model'), ctx.get('evaluation_model')])
    evaluation = deepcopy(state['evaluation'])
    evaluation['research_cycle'] = state['research_cycle']
    evaluation['final_ideas'] = state['current']
    evaluation['final_survey'] = state['survey']
    evaluation['final_harvest'] = state['harvest']
    evaluation['survey_revision_usage'] = state['survey_revision_usage']
    evaluation['retrieval_usage'] = state['retrieval_usage']
    evaluation['final_survey']['usage'] = _aggregate_usage(
        ctx['artifacts']['survey'].get('usage') or {}, state['survey_revision_usage'])['total']
    evaluation['final_harvest']['usage'] = _aggregate_usage(
        {}, state['retrieval_usage'], harvest_usage=ctx['artifacts']['harvest'].get('usage') or {})['total']
    evaluation['revision_usage'] = state['revision_usage']
    evaluation['review_usage'] = state['review_usage']
    evaluation['raw_evaluation'] = deepcopy(state['evaluation'])
    evaluation['usage'] = _aggregate_usage(
        state['survey_revision_usage'], state['revision_usage'], state['review_usage'],
        harvest_usage=state['retrieval_usage'])['total']
    if state['research_cycle']['status'] == 'error':
        evaluation['degraded'] = True
        evaluation['degraded_reason'] = 'research revision or re-review unavailable'
    return evaluation


def _final_idea_artifact(ideate, evaluation):
    artifact = {**ideate, **(evaluation.get('final_ideas') or {}),
            'usage': _aggregate_usage({}, ideate.get('usage') or {},
                                      evaluation.get('revision_usage') or {})['total'],
            'evaluation_usage': evaluation.get('review_usage', evaluation.get('usage') or {}),
            'evaluation': evaluation.get('raw_evaluation', evaluation),
            'research_cycle': evaluation.get('research_cycle') or {}}
    # The raw review can succeed even when the subsequent revision failed.
    # Persist the outer cycle's failure without rewriting that raw evidence.
    if evaluation.get('degraded'):
        artifact['degraded'] = True
        artifact['degraded_reason'] = evaluation.get('degraded_reason') or 'research cycle degraded'
    return artifact


# ── Stage: publish ─────────────────────────────────────────────────────────

def _run_publish(ctx: dict) -> dict:
    """Publish the complete research artifact after every quality stage.

    Expensive generation is already checkpointed at this boundary. A transient
    Sidecar failure therefore retries only these idempotent writes; an
    unconfirmed write can never be projected as a clean terminal success.
    """
    survey = ctx['artifacts']['survey']
    ideate = ctx['artifacts']['ideate']
    evaluation = ctx['artifacts']['evaluate']
    survey = evaluation.get('final_survey') or survey
    user_id = require_user_id(
        ctx.get('user_id'), context='research publication stage')
    receipts = ctx.setdefault('_research_publication_receipts', {})

    def check_cancelled():
        if ctx.get('abort_check') and ctx['abort_check']():
            raise StageAborted('research publication cancelled')
    check_cancelled()
    if not receipts.get('survey'):
        # Historical checkpoints and isolated publication tests predate the
        # optional discovery-translation usage snapshot.
        harvest = evaluation.get('final_harvest') or ctx['artifacts'].get('harvest') or {}
        survey_saved = _persist_survey(
            ctx['direction'], ctx.get('lang', 'en'),
            survey.get('survey_md') or '', survey.get('open_gaps') or {},
            survey.get('usage') or {},
            harvest_usage=harvest.get('usage') or {}, user_id=user_id)
        if not survey_saved:
            raise RuntimeError(
                'research repository did not confirm survey publication')
        receipts['survey'] = True

    check_cancelled()
    if not receipts.get('ideas'):
        ideas_saved = _persist_ideate(
            ctx['direction'], ctx.get('lang', 'en'),
            _final_idea_artifact(ideate, evaluation), user_id=user_id)
        if not ideas_saved:
            raise RuntimeError(
                'research repository did not confirm evaluated-ideas publication')
        receipts['ideas'] = True

    return {'confirmed': True, 'rows': 2}


# ── Stage graph + runner ───────────────────────────────────────────────────

def research_recipe_stages() -> list:
    """The ordered research graph; data-dependent fan-out stays inside stages."""
    return [
        Stage('harvest', _run_harvest, gate=_gate_harvest, retry=1),
        Stage('survey', _run_survey, gate=_gate_survey, retry=1,
              checkpoint_version='comparison-matrix-v2'),
        Stage('ideate', _run_ideate, gate=_gate_ideate, retry=1,
              checkpoint_version='causal-mechanism-v2'),
        Stage('evaluate', _run_evaluate, checkpoint_version='cross-stage-revision-v2'),
        Stage('publish', _run_publish, retry=2,
              checkpoint_version='research-artifacts-v1'),
    ]


def build_research_from_direction(direction: str, workdir: str, *, lang: str = 'en',
                                  user_id: int, n_ideas: int = 6,
                                  seed_arxiv_ids=None, harvest_n: int = _DEFAULT_HARVEST_N,
                                  abort_event=None, emit=None,
                                  model: str | None = None,
                                  evaluation_model: str | None = None,
                                  model_budget_admit=None) -> dict:
    """Run harvest → survey → ideate → evaluate → publish for one direction.

    One pass over a static five-stage graph, checkpointed: a process killed
    mid-graph resumes at the first unfinished stage. Publication is terminal,
    so storage retries never redo harvested papers or model work.

    Args:
        direction: the research direction (free text).
        workdir: job directory; the checkpoint lives at
            ``<workdir>/pipeline_state.json``. The library ``folder_id`` is
            derived from the workdir basename so re-runs share one shelf.
        lang / user_id / n_ideas / seed_arxiv_ids / harvest_n: forwarded.
        abort_event / emit: forwarded to the stage runner.
        model_budget_admit: shared admission callback for translation, survey,
            ideation, auxiliary search filtering and independent review. The
            caller owns durable account/claim setup and recovery permission.

    Returns the ideate stage artifact ({accepted, rejected, threshold}), plus
    ``survey_md`` / ``open_gaps`` / ``folder_id`` folded in for the caller.
    """
    if model_budget_admit is not None and not callable(model_budget_admit):
        raise TypeError('research model budget admission must be callable')
    user_id = require_user_id(user_id, context='research recipe')
    request = normalize_research_request(
        direction, lang=lang, n_ideas=n_ideas,
        seed_arxiv_ids=seed_arxiv_ids, model=model)
    harvest_n = normalize_research_harvest_count(harvest_n)
    os.makedirs(workdir, exist_ok=True)
    folder_id = f'research_{os.path.basename(workdir.rstrip("/"))}'
    ctx = {
        'direction': request.direction, 'workdir': workdir, 'lang': request.lang,
        'user_id': user_id, 'n_ideas': request.n_ideas, 'folder_id': folder_id,
        'seed_arxiv_ids': list(request.seed_arxiv_ids), 'harvest_n': harvest_n,
        'model': request.model,
        'evaluation_model': evaluation_model,
        'model_budget_admit': model_budget_admit,
        'abort_event': abort_event,
        'emit': emit,
        'abort': (lambda: bool(abort_event is not None and abort_event.is_set())),
        'abort_check': (lambda: bool(abort_event is not None and abort_event.is_set())),
    }
    state_path = os.path.join(workdir, 'pipeline_state.json')
    def checkpoint_event(event):
        # Stage runner emits done/skipped only after commit/recovery validation.
        # Never expose model drafts as ready artifacts.
        counts = {}
        if event.get('type') in ('stage_done', 'stage_skipped'):
            stage = event.get('stage')
            artifact = ctx.get('artifacts', {}).get(stage) or {}
            if stage == 'harvest':
                counts = {'discovered': (artifact.get('discovery') or {}).get('discovered_count', 0),
                          'admitted': artifact.get('admitted', len(artifact.get('arxiv_ids') or []) + artifact.get('errors', 0)),
                          'parsed': artifact.get('harvested', 0),
                          'cache_hits': artifact.get('cache_hits', 0),
                          'failed': artifact.get('errors', 0)}
            elif stage == 'ideate':
                counts = {'accepted': len(artifact.get('accepted') or []),
                          'rejected': len(artifact.get('rejected') or [])}
                counts['candidates'] = counts['accepted'] + counts['rejected']
        if emit:
            emit({**event, 'counts': counts})
    artifacts = run_stages(
        research_recipe_stages(), ctx, state_path=state_path, emit=checkpoint_event,
        abort_check=ctx['abort_check'], abort_exceptions=(AbortedError,))

    ideate = artifacts.get('ideate') or {}
    survey = artifacts.get('survey') or {}
    harvest = artifacts.get('harvest') or {}
    evaluation = artifacts.get('evaluate') or {}
    survey = evaluation.get('final_survey') or survey
    harvest = evaluation.get('final_harvest') or harvest
    original_usage = ideate.get('usage') or {}
    ideate = _final_idea_artifact(ideate, evaluation)
    out = {
        'direction': request.direction, 'lang': request.lang,
        'folder_id': folder_id,
        'accepted': ideate.get('accepted', []),
        'rejected': ideate.get('rejected', []),
        'retired': ideate.get('retired', []),
        'threshold': ideate.get('threshold'),
        'gate_reached': ideate.get('gate_reached'),
        'open_gaps': survey.get('open_gaps', {}),
        'survey_md': survey.get('survey_md', ''),
        'harvested': harvest.get('harvested', 0),
        'cache_hits': harvest.get('cache_hits', 0),
        'corpus_size': len(harvest.get('arxiv_ids') or []),
        'corpus_arxiv_ids': list(harvest.get('arxiv_ids') or []),
        'evaluation': evaluation.get('raw_evaluation', evaluation),
        'research_cycle': evaluation.get('research_cycle') or {},
        'usage': _aggregate_usage(survey.get('usage') or {},
                                  original_usage,
                                  _aggregate_usage({}, evaluation.get('revision_usage') or {},
                                                   evaluation.get('review_usage', evaluation.get('usage') or {}))['total'],
                                  harvest_usage=harvest.get('usage') or {}),
    }
    if ideate.get('degraded'):
        out['degraded'] = True
        out['degraded_reason'] = ideate.get('degraded_reason') or 'pipeline degraded'
    if evaluation.get('degraded'):
        reason = evaluation.get('degraded_reason') or 'LLM evaluation degraded'
        out['degraded'] = True
        if out.get('degraded_reason'):
            out['degraded_reason'] += f'; {reason}'
        else:
            out['degraded_reason'] = reason
    return out
