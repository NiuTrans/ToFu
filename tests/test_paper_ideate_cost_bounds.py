"""Cost and cache-identity guards for the research ideation stage."""

from __future__ import annotations

import uuid
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _idea(title='Predictive Delta KV Cache'):
    return {
        'title': title,
        'kind': 'methodology',
        'linked_gap_id': 'gap-1',
        'corpus_anchor_id': '2501.00001',
        'corpus_delta': 'replace static allocation',
        'failure_cause': 'allocation ignores future retrieval demand',
        'new_invariant': 'retrieval-conditioned capacity',
        'intervention_level': 'algorithm',
        'core_mechanism': 'predict demand before allocating cache',
        'novelty_claim': 'allocation becomes retrieval-conditioned',
        'falsifiable_prediction': 'lower miss rate at fixed memory',
        'why_not_AB': 'changes the allocation invariant',
        'retrieval_query': 'predictive retrieval cache allocation',
        'prior_art': ['2501.00001'],
    }


def _prior_set():
    return {
        'retrieved': [{
            'arxiv_id': '2501.00002', 'title': 'Static KV Allocation',
            'summary': 'Allocates a fixed rank per layer.',
        }],
        'retrieved_ids': ['2501.00002'],
        'self_reported_ids': ['2501.00001'],
        'retrieval_query': 'q', 'query_source': 'model',
        'query_mode': 'all', 'novelty_basis': 'retrieved',
    }


def test_self_reported_prior_art_is_bounded_deduped_and_memoized(monkeypatch):
    import lib.paper.ideate as ideate

    probes = []
    monkeypatch.setattr(
        ideate, 'fetch_arxiv_title',
        lambda arxiv_id: probes.append(arxiv_id) or f'Title {arxiv_id}')
    ids = [f'2501.{index:05d}' for index in range(25)]
    cache = {}

    first = {'prior_art': ids}
    second = {'prior_art': list(ids)}
    grounded, dropped = ideate._ground_idea_prior_art(
        first, title_cache=cache)
    grounded_again, dropped_again = ideate._ground_idea_prior_art(
        second, title_cache=cache)

    assert len(probes) == 20
    assert grounded == grounded_again == ids[:20]
    assert dropped == dropped_again == 0
    assert first['prior_art_input_count'] == 25
    assert first['prior_art_truncated'] == 5

    duplicate = {'prior_art': ['2501.00000', '2501.00000']}
    ideate._ground_idea_prior_art(duplicate, title_cache={})
    assert duplicate['prior_art'] == ['2501.00000']


def test_exact_retrieval_probe_is_reused_but_failure_is_retryable(monkeypatch):
    import lib.paper.ideate as ideate

    calls = []

    def search(identity, domain=None, *, field='ti', max_results=5):
        calls.append((tuple(identity), tuple(domain or ()), field, max_results))
        return {
            'outcome': 'ok', 'query': 'wire-query',
            'papers': [{
                'arxiv_id': '2501.00002', 'title': 'Neighbour',
                'summary': 'summary',
            }],
        }

    monkeypatch.setattr(ideate, 'ts_search_by_query', search)
    cache = {}
    first = ideate._novelty_prior_set(_idea(), retrieval_cache=cache)
    second = ideate._novelty_prior_set(_idea(), retrieval_cache=cache)
    assert len(calls) == 1
    assert first == second
    assert first['retrieved_ids'] == ['2501.00002']

    failures = []

    def fail(identity, domain=None, **kwargs):
        failures.append(1)
        raise RuntimeError('temporary outage')

    monkeypatch.setattr(ideate, 'ts_search_by_query', fail)
    failed_cache = {}
    ideate._novelty_prior_set(_idea(), retrieval_cache=failed_cache)
    ideate._novelty_prior_set(_idea(), retrieval_cache=failed_cache)
    assert len(failures) == 2
    assert failed_cache == {}


def test_judges_share_static_system_prefix_and_isolate_dynamic_evidence():
    import lib.paper.ideate as ideate

    first = ideate._judge_messages(
        _idea('Idea Alpha'), _prior_set(), {'id': 'gap-1', 'gap': 'Gap A'}, 'en')
    second = ideate._judge_messages(
        _idea('Idea Beta'), _prior_set(), {'id': 'gap-2', 'gap': 'Gap B'}, 'en')

    assert [message['role'] for message in first] == ['system', 'user']
    assert first[0] == second[0]
    assert first[1] != second[1]
    assert 'Idea Alpha' not in first[0]['content']
    assert '2501.00002' not in first[0]['content']
    assert 'Return ONLY one JSON' in first[0]['content']
    assert 'Idea Alpha' in first[1]['content']
    assert '2501.00002' in first[1]['content']


def test_excess_model_ideas_are_dropped_before_retrieval_and_judging(monkeypatch):
    import lib.paper.ideate as ideate

    offered = [_idea(f'Idea {index}') for index in range(1_000)]
    retrievals = []
    judges = []
    monkeypatch.setattr(
        ideate, '_generate_raw_ideas', lambda *args, **kwargs: offered)
    monkeypatch.setattr(
        ideate, 'fetch_arxiv_title', lambda arxiv_id: f'Title {arxiv_id}')

    def retrieve(idea, **kwargs):
        retrievals.append(idea['title'])
        return _prior_set()

    def judge(idea, prior_set, gap, lang, **kwargs):
        judges.append(idea['title'])
        return {
            'scores': {axis: 5 for axis in ideate.RUBRIC_AXES},
            'overall': 5.0, 'mechanism_delta': 'mechanism-level',
            'closest_neighbor': '2501.00002', 'justifications': {},
            'verdict': 'pass', 'novelty_capped': False,
        }

    monkeypatch.setattr(ideate, '_novelty_prior_set', retrieve)
    monkeypatch.setattr(ideate, '_score_idea', judge)
    result = ideate.generate_ideas(
        'bounded', {
            'open_gaps': [{
                'id': 'gap-1', 'gap': 'Gap', 'evidence': ['2501.00001'],
            }],
        }, n_ideas=100_000, user_id=1)

    assert len(retrievals) == len(judges) == 12
    assert len(result['accepted']) == 12
    assert result['generated_truncated'] == 988


def test_rubric_scheduler_warms_once_then_uses_bounded_parallelism(
        monkeypatch):
    import lib.paper.ideate as ideate
    from lib.llm_dispatch.conv_affinity import conv_affinity, get_conv_affinity

    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '2')
    rows = [
        ideate._PreparedIdeaJudge(
            raw_index=index, idea=_idea(f'Idea {index}'),
            prior_set=_prior_set(), gap={'id': 'gap-1'},
            prior_art_dropped=0)
        for index in range(6)
    ]
    lock = threading.Lock()
    active = 0
    peak = 0
    events = []
    affinities = []

    def score(idea, prior_set, gap, lang, **kwargs):
        nonlocal active, peak
        with lock:
            events.append(('start', idea['title']))
            affinities.append(get_conv_affinity())
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
            events.append(('finish', idea['title']))
        return idea['title']

    monkeypatch.setattr(ideate, '_score_idea', score)
    with conv_affinity('research-cost-test'):
        results = ideate._score_prepared_ideas(rows, lang='en')

    assert results == [f'Idea {index}' for index in range(6)]
    assert events[:2] == [('start', 'Idea 0'), ('finish', 'Idea 0')]
    assert peak == 2
    assert set(affinities) == {'research-cost-test'}


def test_rubric_scheduler_stops_admission_after_abort(monkeypatch):
    import lib.paper.ideate as ideate
    from lib.llm_errors import AbortedError

    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '2')
    rows = [
        ideate._PreparedIdeaJudge(
            raw_index=index, idea=_idea(f'Idea {index}'),
            prior_set=_prior_set(), gap={'id': 'gap-1'},
            prior_art_dropped=0)
        for index in range(8)
    ]
    aborted = threading.Event()
    admitted = []
    lock = threading.Lock()

    def score(idea, prior_set, gap, lang, **kwargs):
        with lock:
            admitted.append(idea['title'])
        if idea['title'] == 'Idea 1':
            aborted.set()
        time.sleep(0.03)
        return idea['title']

    monkeypatch.setattr(ideate, '_score_idea', score)
    with pytest.raises(AbortedError, match='aborted'):
        ideate._score_prepared_ideas(
            rows, lang='en', abort=aborted.is_set)
    assert admitted[0] == 'Idea 0'
    assert 2 <= len(admitted) <= 3
    assert set(admitted).issubset({'Idea 0', 'Idea 1', 'Idea 2'})


def test_research_usage_meter_is_exact_under_parallel_records():
    from concurrent.futures import ThreadPoolExecutor
    from lib.research.telemetry import ResearchUsageMeter

    meter = ResearchUsageMeter('parallel-test')
    usage = {
        'prompt_tokens': 7, 'completion_tokens': 3,
        '_dispatch': {'model': 'unknown-test-model'},
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: meter.record(usage), range(400)))
    snapshot = meter.snapshot()
    assert snapshot['calls'] == 400
    assert snapshot['prompt_tokens'] == 2_800
    assert snapshot['completion_tokens'] == 1_200
    assert snapshot['total_tokens'] == 4_000


def test_research_worker_uses_task_scoped_dispatch_affinity(monkeypatch, tmp_path):
    import lib.research.engine as engine
    from lib.llm_dispatch.conv_affinity import get_conv_affinity
    from lib.research.runtime import _research_runtime

    task_id = f'research_affinity_{uuid.uuid4().hex[:12]}'
    task = _research_runtime.create(user_id=1, task_id=task_id)
    task.update({
        'task_id': task_id, 'user_id': 1, 'direction': 'bounded',
        'workdir': str(tmp_path), 'lang': 'en', 'n_ideas': 3,
        'seed_arxiv_ids': [],
    })
    observed = []

    def build(*args, **kwargs):
        observed.append(get_conv_affinity())
        return {
            'accepted': [], 'rejected': [{'reject_stage': 'rubric'}],
            'corpus_size': 3, 'gate_reached': 'rubric',
        }

    monkeypatch.setattr(engine, '_write_manifest', lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, '_emit', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        'lib.research.recipe.build_research_from_direction', build)

    assert get_conv_affinity() is None
    engine.run_research_task(task)
    assert observed == [f'production-research:{task_id}']
    assert get_conv_affinity() is None
    assert task['status'] == 'done'
