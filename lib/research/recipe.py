"""lib/research/recipe.py — direction → harvest → survey → ideate → evaluate.

The auto-research recipe's spine wires the three generative primitives
(harvest / survey / ideate) and an independent LLM evaluation pass into the production stage graph
(docs/AUTO_RESEARCH_SYSTEM_DESIGN.md §4), so a single ``produce_research`` call
runs the whole chain with checkpointed crash-resume — and each stage stays
independently callable.

Stages:  harvest → survey → ideate → evaluate

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

Every seam into R1–R3 is resolved through this module (``_harvest_batch`` /
``_build_survey`` / ``_generate_ideas`` / ``_search_arxiv``) so a test patches
``recipe._build_survey`` etc. and drives the whole graph offline — the graph
wiring and crash-resume are provable with zero network and zero real LLM.
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.production.stages import Stage, run_stages

logger = get_logger(__name__)

__all__ = ['build_research_from_direction', 'research_recipe_stages']

#: How many arXiv papers to seed the harvest with when the caller gives no
#: explicit id list (derived from the direction via search).
_DEFAULT_HARVEST_N = 20
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


def _harvest_batch(arxiv_ids, *, folder_id, user_id, abort_check=None,
                   on_progress=None):
    from lib.paper.harvest import harvest_arxiv_batch
    return harvest_arxiv_batch(arxiv_ids, folder_id=folder_id, user_id=user_id,
                               abort_check=abort_check, on_progress=on_progress)


def _build_survey(direction, arxiv_ids, *, lang, user_id, folder_id, abort=None,
                  on_tool_event=None):
    from lib.paper.survey import build_survey
    return build_survey(direction, arxiv_ids, lang=lang, user_id=user_id,
                        folder_id=folder_id, abort=abort,
                        on_tool_event=on_tool_event)


def _generate_ideas(direction, open_gaps, *, lang, n_ideas, abort=None,
                    on_tool_event=None):
    from lib.paper.ideate import generate_ideas
    return generate_ideas(direction, open_gaps, lang=lang, n_ideas=n_ideas,
                          abort=abort, on_tool_event=on_tool_event)


def _evaluate_result(direction, result, *, model=None, abort=None):
    from lib.research.evaluation import evaluate_research_result
    return evaluate_research_result(direction, result, model=model, abort=abort)


def _persist_survey(direction, lang, survey_md, open_gaps, usage=None):
    from lib.research.persistence import persist_survey
    return persist_survey(direction, lang, survey_md, open_gaps, usage=usage)


def _persist_ideate(direction, lang, artifact):
    from lib.research.persistence import persist_ideate
    return persist_ideate(direction, lang, artifact)


def _aggregate_usage(survey_usage: dict, ideate_usage: dict,
                     evaluate_usage: dict | None = None) -> dict:
    """Fold stage snapshots without reimplementing per-call token pricing."""
    from lib.research.telemetry import aggregate_research_usage
    return aggregate_research_usage(survey_usage, ideate_usage, evaluate_usage)


# ── Stage: harvest ─────────────────────────────────────────────────────────

def _run_harvest(ctx: dict) -> dict:
    """Crawl related papers into the library, parse-once (R1).

    Seed ids come from ctx['seed_arxiv_ids'] when provided (R7's discover stage
    will fill these); otherwise they are derived from the direction via
    search_arxiv so the chain is runnable end-to-end today.
    """
    direction = ctx['direction']
    folder_id = ctx['folder_id']
    user_id = ctx.get('user_id', 1)
    seed = list(ctx.get('seed_arxiv_ids') or [])
    if not seed:
        n = ctx.get('harvest_n', _DEFAULT_HARVEST_N)
        try:
            hits = _search_arxiv(direction, max_results=n) or []
            seed = [h.get('arxiv_id') for h in hits if h.get('arxiv_id')]
        except Exception as e:
            logger.warning('[Research:harvest] seed search failed for %.60s: %s',
                           direction, e)
            seed = []
    abort_check = ctx.get('abort_check')
    out = _harvest_batch(seed, folder_id=folder_id, user_id=user_id,
                         abort_check=abort_check)
    # The ids actually in the shelf after this run (parsed this run OR already
    # cached) — this is what survey fans in.
    shelf_ids = [r['arxivId'] for r in (out.get('results') or [])
                 if r.get('status') in ('parsed', 'cache_hit') and r.get('arxivId')]
    logger.info('[Research:harvest] %.60s → %d in shelf (%d parsed, %d cache-hit) folder=%s',
                direction, len(shelf_ids), out.get('parsed', 0),
                out.get('cache_hits', 0), folder_id)
    return {'folder_id': folder_id, 'arxiv_ids': shelf_ids,
            'harvested': out.get('parsed', 0), 'cache_hits': out.get('cache_hits', 0),
            'errors': out.get('errors', 0)}


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
    kwargs = dict(lang=ctx.get('lang', 'en'), user_id=ctx.get('user_id', 1),
                  folder_id=h['folder_id'], abort=ctx.get('abort'))
    # Keep the monkeypatch seam backward-compatible for offline graph tests;
    # real jobs always carry the stage runner's event sink.
    if ctx.get('emit') is not None:
        kwargs['on_tool_event'] = ctx['emit']
    res = _build_survey(ctx['direction'], h['arxiv_ids'], **kwargs)
    if not res.get('ok'):
        # Surface as an exception so the stage retries / fails loudly.
        raise RuntimeError(f'survey failed: {res.get("error")}')
    art = {'open_gaps': res['open_gaps'], 'survey_md': res['survey_md'],
           'inputs_used': res.get('inputs_used', 0),
           'citation_audit': res.get('citation_audit'),
           'usage': res.get('usage') or {}}
    # Durable BEFORE the checkpoint: the task registry is in-memory with a TTL,
    # so without this the survey vanishes ~2h after the run (and instantly on a
    # restart). Never raises — a storage fault must not discard a finished
    # survey (see lib/research/persistence.py).
    _persist_survey(ctx['direction'], ctx.get('lang', 'en'), art['survey_md'],
                    art['open_gaps'], art['usage'])
    return art


def _gate_survey(ctx: dict, art: dict) -> list:
    gm = art.get('open_gaps') or {}
    if not gm.get('open_gaps'):
        return ['survey produced no open gaps — nothing for ideate to solve']
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
    kwargs = dict(lang=ctx.get('lang', 'en'), n_ideas=ctx.get('n_ideas', 6),
                  abort=ctx.get('abort'))
    if ctx.get('emit') is not None:
        kwargs['on_tool_event'] = ctx['emit']
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
    # A degraded pass produced a VALID artifact from a SICK pipeline. It must
    # cross the stage boundary (and be committed to the checkpoint) or the
    # symptom reaching the user is an indistinguishable 'done, accepted 0' —
    # the flag is only worth anything if it survives to the result body.
    if res.get('degraded'):
        art['degraded'] = True
        art['degraded_reason'] = res.get('degraded_reason') or 'pipeline degraded'
        logger.error('[Research:ideate] DEGRADED — %s', art['degraded_reason'])
    # Durable BEFORE the checkpoint. The rejection audit carried here is the
    # calibration data for IDEATE_GATE_THRESHOLD, so losing it to a TTL sweep
    # forfeits the ability to tune the gate from real runs.
    _persist_ideate(ctx['direction'], ctx.get('lang', 'en'), art)
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

def _run_evaluate(ctx: dict) -> dict:
    """Score the frozen survey/idea artifact without mutating its gate.

    Evaluation is observability, not another source of expensive artifact
    loss. Any unexpected judge/provider failure is committed as an explicit
    degraded evaluation so harvest/survey/ideate remain resumable and visible.
    """
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
    }
    try:
        evaluation = _evaluate_result(
            ctx['direction'], frozen, model=ctx.get('evaluation_model'),
            abort=ctx.get('abort'))
        if not isinstance(evaluation, dict):
            raise TypeError('evaluator returned a non-object result')
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

    # The ideate row is the durable research-decision record. Upsert it again
    # with the evaluation attached; this is idempotent and does not create a
    # third schema/key family.
    _persist_ideate(ctx['direction'], ctx.get('lang', 'en'), {
        **ideate, 'evaluation': evaluation,
    })
    return evaluation


# ── Stage graph + runner ───────────────────────────────────────────────────

def research_recipe_stages() -> list:
    """The ordered research stage graph. Static (unlike longform) — four
    fixed stages; the data-dependent fan-out lives INSIDE harvest/survey."""
    return [
        Stage('harvest', _run_harvest, gate=_gate_harvest, retry=1),
        Stage('survey', _run_survey, gate=_gate_survey, retry=1,
              checkpoint_version='comparison-matrix-v2'),
        Stage('ideate', _run_ideate, gate=_gate_ideate, retry=1,
              checkpoint_version='causal-mechanism-v2'),
        Stage('evaluate', _run_evaluate, checkpoint_version='llm-judge-v1'),
    ]


def build_research_from_direction(direction: str, workdir: str, *, lang: str = 'en',
                                  user_id: int = 1, n_ideas: int = 6,
                                  seed_arxiv_ids=None, harvest_n: int = _DEFAULT_HARVEST_N,
                                  abort_event=None, emit=None,
                                  evaluation_model: str | None = None) -> dict:
    """Run harvest → survey → ideate → evaluate for one direction.

    One pass over a static four-stage graph, checkpointed: a process killed
    mid-graph resumes at the first unfinished stage (harvest papers already in
    the shelf, and a survey/ideate artifact already committed, are never
    redone).

    Args:
        direction: the research direction (free text).
        workdir: job directory; the checkpoint lives at
            ``<workdir>/pipeline_state.json``. The library ``folder_id`` is
            derived from the workdir basename so re-runs share one shelf.
        lang / user_id / n_ideas / seed_arxiv_ids / harvest_n: forwarded.
        abort_event / emit: forwarded to the stage runner.

    Returns the ideate stage artifact ({accepted, rejected, threshold}), plus
    ``survey_md`` / ``open_gaps`` / ``folder_id`` folded in for the caller.
    """
    os.makedirs(workdir, exist_ok=True)
    folder_id = f'research_{os.path.basename(workdir.rstrip("/"))}'
    ctx = {
        'direction': direction, 'workdir': workdir, 'lang': lang,
        'user_id': user_id, 'n_ideas': n_ideas, 'folder_id': folder_id,
        'seed_arxiv_ids': list(seed_arxiv_ids or []), 'harvest_n': harvest_n,
        'evaluation_model': evaluation_model,
        'abort_event': abort_event,
        'emit': emit,
        'abort': (lambda: bool(abort_event is not None and abort_event.is_set())),
        'abort_check': (lambda: bool(abort_event is not None and abort_event.is_set())),
    }
    state_path = os.path.join(workdir, 'pipeline_state.json')
    artifacts = run_stages(
        research_recipe_stages(), ctx, state_path=state_path, emit=emit,
        abort_check=ctx['abort_check'])

    ideate = artifacts.get('ideate') or {}
    survey = artifacts.get('survey') or {}
    harvest = artifacts.get('harvest') or {}
    evaluation = artifacts.get('evaluate') or {}
    out = {
        'direction': direction, 'lang': lang, 'folder_id': folder_id,
        'accepted': ideate.get('accepted', []),
        'rejected': ideate.get('rejected', []),
        'threshold': ideate.get('threshold'),
        'gate_reached': ideate.get('gate_reached'),
        'open_gaps': survey.get('open_gaps', {}),
        'survey_md': survey.get('survey_md', ''),
        'harvested': harvest.get('harvested', 0),
        'cache_hits': harvest.get('cache_hits', 0),
        'corpus_size': len(harvest.get('arxiv_ids') or []),
        'corpus_arxiv_ids': list(harvest.get('arxiv_ids') or []),
        'evaluation': evaluation,
        'usage': _aggregate_usage(survey.get('usage') or {},
                                  ideate.get('usage') or {},
                                  evaluation.get('usage') or {}),
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
