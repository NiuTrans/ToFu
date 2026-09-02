#!/usr/bin/env python3
"""R4 — auto-research recipe on the production stage graph (lib/research/).

Proven here (pure — the R1–R3 seams are monkeypatched, so no DB / network /
LLM; only a temp workdir is touched):

  1. FOUR-STAGE GRAPH — build_research_from_direction runs harvest → survey →
     ideate → evaluate in order, threading the pinned data contract: harvest's folder_id +
     arxiv_ids reach survey, survey's open_gaps reaches ideate, and the final
     result carries accepted/rejected/open_gaps/corpus_size.

  2. STAGE DATA CONTRACT — survey receives EXACTLY the folder_id + id list
     harvest produced (asserted via captured call args); ideate receives
     EXACTLY survey's open_gaps object. Boundaries carry real schema, not an
     in-memory global.

  3. CRASH-RESUME — a first pass that dies inside ideate leaves harvest+survey
     committed to the checkpoint; the second pass re-runs ONLY ideate (harvest
     and survey seams are asserted called zero times on resume).
       ↳ NEUTER: delete the survey checkpoint entry between passes → survey
         re-runs (its seam is called again) while harvest still does not.

  4. NO-REDO — a fully completed job re-invoked does zero work (all four seams
     called zero times; result served from the checkpoint).

Run standalone:  python tests/test_research_recipe.py
Under pytest:    pytest tests/test_research_recipe.py -m unit
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


# ── Fake R1–R3 seams (record calls) ────────────────────────────────────────

class _Seams:
    """Monkeypatchable stand-ins for harvest/survey/ideate, counting calls and
    capturing args so the graph wiring + data contract are assertable."""

    def __init__(self):
        self.harvest_calls = []
        self.survey_calls = []
        self.ideate_calls = []
        self.evaluate_calls = []
        self.harvest_ids = ['2305.11111', '2401.22222', '2402.33333', '2403.44444']
        self.open_gaps = {
            'schema_version': 1, 'open_gaps': [
                {'id': 'gap_1', 'gap': 'no exact recall', 'evidence': ['2305.11111']}],
            'clusters': [],
            'method_matrix': [{'paper': aid, 'method': 'm'}
                              for aid in self.harvest_ids],
            'surveyed_arxiv_ids': list(self.harvest_ids),
            'stripped_ids': [], 'missing_ids': []}

    def harvest(self, arxiv_ids, *, folder_id, user_id, abort_check=None, on_progress=None):
        self.harvest_calls.append({'ids': list(arxiv_ids), 'folder_id': folder_id})
        return {'total': len(self.harvest_ids), 'parsed': len(self.harvest_ids),
                'cache_hits': 0, 'errors': 0,
                'results': [{'arxivId': a, 'status': 'parsed'} for a in self.harvest_ids]}

    def build_survey(self, direction, arxiv_ids, *, lang, user_id, folder_id, abort=None):
        self.survey_calls.append({'direction': direction, 'arxiv_ids': list(arxiv_ids),
                                  'folder_id': folder_id})
        gm = dict(self.open_gaps)
        gm['library_folder_id'] = folder_id
        return {'ok': True, 'open_gaps': gm, 'survey_md': '# Survey\narXiv:2305.11111',
                'inputs_used': len(arxiv_ids), 'citation_audit': None}

    def generate_ideas(self, direction, open_gaps, *, lang, n_ideas, user_id,
                       abort=None):
        self.ideate_calls.append({
            'direction': direction, 'open_gaps': open_gaps,
            'user_id': user_id,
        })
        return {'ok': True, 'threshold': 4.0,
                'accepted': [{'id': 'idea_1', 'title': 'Good', 'overall': 4.6}],
                'rejected': [{'id': 'idea_2', 'title': 'Stitch',
                              'reject_stage': 'structural', 'reject_reason': 'no gap'}]}

    def evaluate(self, direction, result, *, model=None, abort=None):
        self.evaluate_calls.append({'direction': direction, 'result': result})
        return {
            'ok': True, 'schema_version': 1, 'judge_count': 2,
            'attempted_judges': 2, 'scores': {'survey_coverage': 4.0},
            'overall_score': 4.0, 'worth_following_up': True,
            'consensus': 'unanimous', 'degraded': False,
            'failure_modes': [], 'recommended_changes': [],
            'usage': {'calls': 2, 'prompt_tokens': 100},
        }

    def search_arxiv(self, query, max_results=20):
        return [{'arxiv_id': a, 'title': f't{a}'} for a in self.harvest_ids]


def _install(seams, *, fail_ideate=False):
    """Patch recipe seams; return restore(). fail_ideate makes ideate raise
    (to simulate a crash inside the last stage)."""
    import lib.research.recipe as rc
    saved = {k: getattr(rc, k) for k in
             ('_harvest_batch', '_build_survey', '_generate_ideas',
              '_evaluate_result', '_search_arxiv')}
    rc._harvest_batch = seams.harvest
    rc._build_survey = seams.build_survey
    rc._search_arxiv = seams.search_arxiv
    rc._evaluate_result = seams.evaluate
    if fail_ideate:
        def _boom(direction, open_gaps, *, lang, n_ideas, user_id, abort=None):
            seams.ideate_calls.append({'direction': direction, 'open_gaps': open_gaps})
            raise RuntimeError('simulated crash inside ideate')
        rc._generate_ideas = _boom
    else:
        rc._generate_ideas = seams.generate_ideas
    return lambda: [setattr(rc, k, v) for k, v in saved.items()]


# ── Test 1 + 2: graph + data contract ──────────────────────────────────────

def test_four_stage_graph_and_data_contract():
    import lib.research.recipe as rc
    seams = _Seams()
    restore = _install(seams)
    wd = tempfile.mkdtemp(prefix='research_test_')
    try:
        res = rc.build_research_from_direction('long-context KV compression', wd,
                                               lang='en', user_id=1, harvest_n=20)
        # graph ran all four, once each
        assert len(seams.harvest_calls) == 1, seams.harvest_calls
        assert len(seams.survey_calls) == 1
        assert len(seams.ideate_calls) == 1
        assert len(seams.evaluate_calls) == 1
        # data contract: survey got harvest's folder_id + id list
        folder = seams.harvest_calls[0]['folder_id']
        assert seams.survey_calls[0]['folder_id'] == folder, 'folder_id not threaded'
        assert seams.survey_calls[0]['arxiv_ids'] == seams.harvest_ids, 'id list not threaded'
        # data contract: ideate got survey's open_gaps object
        assert seams.ideate_calls[0]['open_gaps']['library_folder_id'] == folder, \
            'ideate did not receive survey open_gaps'
        assert seams.ideate_calls[0]['user_id'] == 1, \
            'ideate artifact owner was not threaded'
        # final result shape
        assert len(res['accepted']) == 1 and len(res['rejected']) == 1
        assert res['open_gaps']['open_gaps'][0]['id'] == 'gap_1'
        assert res['corpus_size'] == 4 and res['folder_id'] == folder
        assert res['evaluation']['overall_score'] == 4.0
        assert res['usage']['stages']['evaluate']['calls'] == 2
    finally:
        restore()
        shutil.rmtree(wd, ignore_errors=True)
    _ok('4-stage graph runs harvest→survey→ideate→evaluate with frozen artifacts')


# ── Test 3: crash-resume from first unfinished stage + NEUTER ──────────────

def _state(wd):
    with open(os.path.join(wd, 'pipeline_state.json')) as f:
        return json.load(f)


def test_crash_resume_reruns_only_unfinished_stage():
    import lib.research.recipe as rc
    seams = _Seams()
    wd = tempfile.mkdtemp(prefix='research_resume_')
    try:
        # Pass 1: ideate crashes → harvest+survey committed, ideate not.
        restore = _install(seams, fail_ideate=True)
        try:
            crashed = False
            try:
                rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
            except Exception:
                crashed = True
            assert crashed, 'pass 1 should have raised from ideate'
        finally:
            restore()
        st = _state(wd)
        assert st['stages'].get('harvest', {}).get('ok'), 'harvest not checkpointed'
        assert st['stages'].get('survey', {}).get('ok'), 'survey not checkpointed'
        assert 'ideate' not in st['stages'], 'ideate should NOT be committed after crash'
        assert len(seams.harvest_calls) == 1 and len(seams.survey_calls) == 1

        # Pass 2: healthy → ONLY ideate re-runs; harvest+survey resumed from disk.
        seams2 = _Seams()
        restore2 = _install(seams2)
        try:
            res = rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        finally:
            restore2()
        assert len(seams2.harvest_calls) == 0, 'harvest MUST NOT re-run on resume'
        assert len(seams2.survey_calls) == 0, 'survey MUST NOT re-run on resume'
        assert len(seams2.ideate_calls) == 1, 'ideate must re-run (it never finished)'
        assert len(seams2.evaluate_calls) == 1, 'evaluation follows resumed ideate'
        assert len(res['accepted']) == 1, 'resumed run should complete ideate'
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    _ok('crash-resume: harvest+survey checkpointed, only ideate re-runs on resume')


def test_delete_mid_checkpoint_reruns_that_stage_NEUTER():
    """NEUTER of the checkpoint contract: delete survey's committed entry
    between passes → survey re-runs, but harvest (still committed) does not."""
    import lib.research.recipe as rc
    seams = _Seams()
    wd = tempfile.mkdtemp(prefix='research_neuter_')
    try:
        # Full successful pass.
        restore = _install(seams)
        try:
            rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        finally:
            restore()
        assert len(seams.harvest_calls) == 1 and len(seams.survey_calls) == 1

        # Surgically delete the survey checkpoint entry (simulate a lost mid-artifact).
        path = os.path.join(wd, 'pipeline_state.json')
        st = _state(wd)
        del st['stages']['survey']
        with open(path, 'w') as f:
            json.dump(st, f)

        # Re-run: survey must re-run (checkpoint gone), harvest must NOT.
        seams2 = _Seams()
        restore2 = _install(seams2)
        try:
            rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        finally:
            restore2()
        assert len(seams2.harvest_calls) == 0, 'harvest still committed → must NOT re-run'
        assert len(seams2.survey_calls) == 1, \
            'NEUTER FAILED: deleted survey checkpoint did not force a survey re-run'
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    _ok('NEUTER: deleting the survey checkpoint forces exactly survey to re-run')


# ── Test 4: no-redo when fully complete ────────────────────────────────────

def test_fully_complete_job_redoes_nothing():
    import lib.research.recipe as rc
    seams = _Seams()
    wd = tempfile.mkdtemp(prefix='research_noredo_')
    try:
        restore = _install(seams)
        try:
            rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        finally:
            restore()
        # Re-invoke on the completed checkpoint — zero seam calls.
        seams2 = _Seams()
        restore2 = _install(seams2)
        try:
            res = rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        finally:
            restore2()
        assert len(seams2.harvest_calls) == 0 and len(seams2.survey_calls) == 0 \
            and len(seams2.ideate_calls) == 0 and len(seams2.evaluate_calls) == 0, \
            'completed job must redo nothing'
        assert len(res['accepted']) == 1, 'result still served from checkpoint'
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    _ok('fully-complete job re-invoked redoes nothing (served from checkpoint)')


# ── Test 5: harvest gate fails on thin corpus ──────────────────────────────

def test_harvest_gate_fails_on_thin_corpus():
    import lib.research.recipe as rc
    seams = _Seams()
    seams.harvest_ids = ['2305.11111']  # only 1 < _MIN_HARVEST_PAPERS
    wd = tempfile.mkdtemp(prefix='research_thin_')
    try:
        restore = _install(seams)
        from lib.production.stages import StageFailed
        try:
            failed = False
            try:
                rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
            except StageFailed as e:
                failed = (e.stage == 'harvest')
            assert failed, 'thin corpus should fail the harvest gate'
            # survey never reached
            assert len(seams.survey_calls) == 0
        finally:
            restore()
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    _ok('harvest gate fails on a corpus below the fan-in minimum (survey never runs)')


def test_survey_gate_rejects_an_empty_comparison_matrix():
    import lib.research.recipe as rc

    artifact = {'open_gaps': {
        'open_gaps': [{'id': 'g1'}],
        'surveyed_arxiv_ids': [f'2301.0000{n}' for n in range(1, 6)],
        'method_matrix': [],
    }}
    errors = rc._gate_survey({}, artifact)
    assert errors and 'method_matrix' in errors[0]
    artifact['open_gaps']['method_matrix'] = [
        {'paper': f'2301.0000{n}', 'method': 'm'} for n in range(1, 5)]
    assert rc._gate_survey({}, artifact) == []


def test_evaluation_failure_is_committed_as_degraded_without_losing_ideas(monkeypatch):
    import lib.research.recipe as rc

    persisted = []
    monkeypatch.setattr(
        rc, '_evaluate_result',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('judge offline')))
    monkeypatch.setattr(rc, '_persist_ideate',
                        lambda direction, lang, art, *, user_id:
                        persisted.append((user_id, art)) or True)
    ctx = {
        'direction': 'd', 'lang': 'en', 'user_id': 1,
        'abort': lambda: False,
        'artifacts': {
            'harvest': {'arxiv_ids': ['2301.00001']},
            'survey': {'survey_md': '# S', 'open_gaps': {'open_gaps': []}},
            'ideate': {'accepted': [{'title': 'kept'}], 'rejected': [],
                        'threshold': 4.0, 'gate_reached': 'accepted'},
        },
    }
    result = rc._run_evaluate(ctx)
    assert result['degraded'] is True and result['judge_count'] == 0
    assert persisted[0][0] == 1
    assert persisted[0][1]['accepted'][0]['title'] == 'kept'
    assert persisted[0][1]['evaluation']['degraded'] is True


# ── Test 7-10: pipeline-pathology propagation (END-TO-END, not per-function) ──
#
# The R3 gate flags a run whose structural gate killed EVERY idea as degraded —
# a pipeline defect, not 宁缺毋滥. That flag is worthless if it dies at the stage
# boundary: the production symptom was `state=done, accepted 0`, indistinguishable
# from a working gate. These tests drive the WHOLE graph
# (build_research_from_direction) so the charter judgement holds: delete the
# degraded logic in ideate.py today and these go red.

def _seams_returning(ideate_result):
    """A _Seams whose generate_ideas returns a fixed ideate result body."""
    seams = _Seams()

    def _gen(direction, open_gaps, *, lang, n_ideas, user_id, abort=None):
        seams.ideate_calls.append({'direction': direction, 'open_gaps': open_gaps})
        return dict(ideate_result)
    seams.generate_ideas = _gen
    return seams


#: A total structural wipe as generate_ideas really reports it.
_WIPE = {
    'ok': True, 'threshold': 4.0, 'accepted': [],
    'rejected': [{'title': 'a', 'reject_stage': 'structural', 'reject_reason': 'no gap'},
                 {'title': 'b', 'reject_stage': 'structural', 'reject_reason': 'no gap'}],
    'degraded': True, 'gate_reached': 'structural',
    'degraded_reason': 'structural gate rejected ALL 2 generated idea(s) — the novelty '
                       'retrieval and rubric never ran; dominant reason (2/2): no gap',
}
#: An honest zero — the rubric actually ran and the ideas genuinely lost.
_HONEST_ZERO = {
    'ok': True, 'threshold': 4.0, 'accepted': [],
    'rejected': [{'title': 'a', 'reject_stage': 'rubric', 'reject_reason': 'overall 2.0 < 4.0'}],
    'gate_reached': 'rubric',
}


def test_degraded_reaches_the_final_result_body():
    """A total structural wipe must be visible in what the caller receives —
    build_research_from_direction's return body IS task['result']."""
    import lib.research.recipe as rc
    seams = _seams_returning(_WIPE)
    restore = _install(seams)
    rc._generate_ideas = seams.generate_ideas
    wd = tempfile.mkdtemp(prefix='research_degraded_')
    try:
        res = rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        assert res.get('degraded') is True, \
            f'degraded must reach the final result body, got keys {sorted(res)}'
        assert res.get('degraded_reason'), 'degraded_reason must reach the caller'
        assert 'structural' in res['degraded_reason']
        assert res.get('gate_reached') == 'structural', \
            f"gate_reached must say how deep the gate got, got {res.get('gate_reached')!r}"
        assert res['accepted'] == [] and len(res['rejected']) == 2, \
            'the audit trail must survive alongside the flag'
    finally:
        restore()
        shutil.rmtree(wd, ignore_errors=True)
    _ok('degraded + degraded_reason + gate_reached reach the final result body')


def test_degraded_is_committed_to_the_checkpoint():
    """The flag must be in the ideate stage ARTIFACT on disk — a crashed/resumed
    process (and any later reader of pipeline_state.json) must still see it."""
    import lib.research.recipe as rc
    seams = _seams_returning(_WIPE)
    restore = _install(seams)
    rc._generate_ideas = seams.generate_ideas
    wd = tempfile.mkdtemp(prefix='research_degraded_ckpt_')
    try:
        rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        art = _state(wd)['stages']['ideate']['artifact']
        assert art.get('degraded') is True, \
            f'degraded missing from the committed ideate artifact: {sorted(art)}'
        assert art.get('gate_reached') == 'structural'
        # …and a resumed run (served from checkpoint) still reports it.
        seams2 = _Seams()
        restore2 = _install(seams2)
        try:
            res2 = rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        finally:
            restore2()
        assert len(seams2.ideate_calls) == 0, 'completed job must not redo ideate'
        assert res2.get('degraded') is True, \
            'a run served from the checkpoint must still report degraded'
    finally:
        restore()
        shutil.rmtree(wd, ignore_errors=True)
    _ok('degraded is committed to the checkpoint and survives a resumed/served run')


def test_honest_zero_is_not_degraded_but_reports_gate_depth():
    """宁缺毋滥 counter-case: ideas that REACHED the rubric and lost are an
    honest zero — NOT degraded — but the result must still say how deep the
    gate got, so a frontend never shows a bare unexplained 0."""
    import lib.research.recipe as rc
    seams = _seams_returning(_HONEST_ZERO)
    restore = _install(seams)
    rc._generate_ideas = seams.generate_ideas
    wd = tempfile.mkdtemp(prefix='research_honest_')
    try:
        res = rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        assert not res.get('degraded'), \
            'a rubric-based zero must NOT be flagged degraded (宁缺毋滥 preserved)'
        assert res.get('gate_reached') == 'rubric', \
            f"honest zero must report gate_reached='rubric', got {res.get('gate_reached')!r}"
        assert res['accepted'] == []
    finally:
        restore()
        shutil.rmtree(wd, ignore_errors=True)
    _ok('honest zero: not degraded, but gate_reached=rubric distinguishes it from a wipe')


def test_degraded_propagation_NEUTER():
    """NEUTER: strip the pass-through out of _run_ideate (exactly the shape the
    stage had before this fix) → the flag must vanish from the final body.

    This is what makes the guard bite the PRODUCT and not just the function:
    if the propagation is ever dropped again, the tests above go red."""
    import lib.research.recipe as rc
    seams = _seams_returning(_WIPE)
    restore = _install(seams)
    rc._generate_ideas = seams.generate_ideas
    orig_run_ideate = rc._run_ideate

    def _no_passthrough(ctx):
        art = orig_run_ideate(ctx)
        return {'accepted': art['accepted'], 'rejected': art['rejected'],
                'threshold': art['threshold']}

    rc._run_ideate = _no_passthrough
    # rebuild the graph so the neutered stage fn is the one wired in
    orig_stages = rc.research_recipe_stages

    def _stages():
        from lib.production.stages import Stage
        return [Stage('harvest', rc._run_harvest, gate=rc._gate_harvest, retry=1),
                Stage('survey', rc._run_survey, gate=rc._gate_survey, retry=1),
                Stage('ideate', _no_passthrough, gate=rc._gate_ideate, retry=1)]
    rc.research_recipe_stages = _stages
    wd = tempfile.mkdtemp(prefix='research_neuter_deg_')
    try:
        res = rc.build_research_from_direction('dir', wd, lang='en', user_id=1)
        leaked = not res.get('degraded')
    finally:
        rc._run_ideate = orig_run_ideate
        rc.research_recipe_stages = orig_stages
        restore()
        shutil.rmtree(wd, ignore_errors=True)
    assert leaked, 'NEUTER FAILED: removing the pass-through still surfaced degraded'
    _ok('NEUTER: dropping the _run_ideate pass-through makes degraded vanish (guard bites)')


# ── Test 6: runtime rides ProductionRuntime (no bespoke runtime) ───────────

def test_runtime_is_production_substrate_not_bespoke():
    from lib.production.runtime import ProductionRuntime
    import lib.research.runtime as rt
    assert isinstance(rt._production, ProductionRuntime), 'research must ride ProductionRuntime'
    assert rt._production.kind == 'research', rt._production.kind
    # the discovered TaskRuntime is the substrate's, not a hand-rolled one
    assert rt._research_runtime is rt._production.runtime
    tid = rt._research_task_id()
    assert tid.startswith('research_'), tid
    _ok('research runtime is a thin ProductionRuntime instance (kind=research), not a 4th copy')


def main():
    print()
    print(_color('═══ R4 Research Recipe / Stage-Graph Tests ═══', '36'))
    print()
    tests = [
        test_four_stage_graph_and_data_contract,
        test_crash_resume_reruns_only_unfinished_stage,
        test_delete_mid_checkpoint_reruns_that_stage_NEUTER,
        test_fully_complete_job_redoes_nothing,
        test_harvest_gate_fails_on_thin_corpus,
        test_degraded_reaches_the_final_result_body,
        test_degraded_is_committed_to_the_checkpoint,
        test_honest_zero_is_not_degraded_but_reports_gate_depth,
        test_degraded_propagation_NEUTER,
        test_runtime_is_production_substrate_not_bespoke,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
