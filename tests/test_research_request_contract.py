"""Bounded request, task identity, and recovery contracts for research jobs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_request_normalizes_identity_and_rejects_unbounded_seed_lists():
    from lib.research.contracts import normalize_research_request

    request = normalize_research_request(
        '  KV cache compression  ', lang='EN', n_ideas=99,
        seed_arxiv_ids=[
            'arXiv:2312.00752v2',
            'https://arxiv.org/abs/2312.00752',
            '2401.12345',
        ])
    assert request.direction == 'KV cache compression'
    assert request.lang == 'en'
    assert request.n_ideas == 12
    assert request.seed_arxiv_ids == ('2312.00752', '2401.12345')
    assert request.dedup_key(7) == (
        7, 'KV cache compression', 'en', 12,
        ('2312.00752', '2401.12345'))

    with pytest.raises(ValueError, match='at most 20'):
        normalize_research_request(
            'bounded', seed_arxiv_ids=[f'2501.{i:05d}' for i in range(21)])
    with pytest.raises(ValueError, match=r'\[0\].*valid arXiv'):
        normalize_research_request('bounded', seed_arxiv_ids=['not-a-paper'])


def test_engine_claim_uses_complete_normalized_identity_and_atomic_fields(
        monkeypatch, tmp_path):
    import lib.research.engine as engine
    import lib.research.runtime as runtime

    claimed = []
    spawned = []
    ids = iter(('research_contract_1', 'research_contract_2'))

    def claim(key, task_id, **fields):
        claimed.append((key, fields))
        return ({'task_id': task_id, **fields}, None)

    fake_runtime = SimpleNamespace(
        spawn=lambda task_id, fn, task: spawned.append((task_id, task)),
        finish=lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(runtime, '_cleanup_stale_research_tasks', lambda: 0)
    monkeypatch.setattr(runtime, '_research_task_id', lambda: next(ids))
    monkeypatch.setattr(runtime, '_claim_research_task', claim)
    monkeypatch.setattr(runtime, '_research_runtime', fake_runtime)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))

    first = engine.produce_research(
        '  bounded direction ', lang='EN', n_ideas=99,
        seed_arxiv_ids=['arXiv:2312.00752v3'], user_id=4)
    second = engine.produce_research(
        'bounded direction', lang='en', n_ideas=3,
        seed_arxiv_ids=['2401.12345'], user_id=4)

    assert first['deduped'] is False and second['deduped'] is False
    assert claimed[0][0] == (
        4, 'bounded direction', 'en', 12, ('2312.00752',))
    assert claimed[1][0] == (
        4, 'bounded direction', 'en', 3, ('2401.12345',))
    assert claimed[0][1]['seed_arxiv_ids'] == ('2312.00752',)
    assert spawned[0][1]['seed_arxiv_ids'] == ('2312.00752',)


def test_engine_start_failure_settles_claimed_task(monkeypatch, tmp_path):
    import lib.research.engine as engine
    import lib.research.runtime as runtime

    settled = []
    fake_runtime = SimpleNamespace(
        spawn=lambda *args: (_ for _ in ()).throw(
            AssertionError('spawn must not run after mkdir failure')),
        finish=lambda task_id, **kwargs: settled.append((task_id, kwargs)),
    )
    monkeypatch.setattr(runtime, '_cleanup_stale_research_tasks', lambda: 0)
    monkeypatch.setattr(runtime, '_research_task_id', lambda: 'research_mkdir_fail')
    monkeypatch.setattr(
        runtime, '_claim_research_task',
        lambda key, task_id, **fields: ({'task_id': task_id, **fields}, None))
    monkeypatch.setattr(runtime, '_research_runtime', fake_runtime)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))
    monkeypatch.setattr(
        engine.os, 'makedirs',
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))

    with pytest.raises(OSError, match='disk full'):
        engine.produce_research('bounded', user_id=4)
    assert settled[0][0] == 'research_mkdir_fail'
    assert settled[0][1]['error_context'] == 'research:start'


def test_manifest_roundtrip_and_resume_preserve_seed_corpus(
        monkeypatch, tmp_path):
    import lib.production.jobs as jobs
    import lib.research.engine as engine
    import lib.research.runtime as runtime

    task = {
        'task_id': 'research_resume_seed', 'user_id': 9,
        'direction': 'seeded', 'lang': 'en', 'n_ideas': 4,
        'seed_arxiv_ids': ['2312.00752'], 'conv_id': 'conv-seed',
        'workdir': str(tmp_path),
    }
    engine._write_manifest(task, 'running')
    manifest = jobs.read_manifest(str(tmp_path))
    assert manifest['seed_arxiv_ids'] == ['2312.00752']

    recreated = []
    spawned = []

    def recreate(task_id, **fields):
        recreated.append((task_id, fields))
        return {'task_id': task_id, **fields}

    monkeypatch.setattr(runtime, '_new_research_task', recreate)
    monkeypatch.setattr(
        runtime, '_research_runtime',
        SimpleNamespace(
            get=lambda task_id: None,
            spawn=lambda task_id, fn, resumed: spawned.append(resumed)))
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path.parent))
    monkeypatch.setattr(
        jobs, 'resume_running_jobs',
        lambda path, is_live, respawn, log_label: (
            respawn('research_resume_seed', str(tmp_path), manifest), 1)[1])

    assert engine.resume_interrupted_research() == 1
    assert recreated[0][1]['seed_arxiv_ids'] == ('2312.00752',)
    assert spawned[0]['seed_arxiv_ids'] == ('2312.00752',)


def test_recipe_caps_misbehaving_search_iterator_and_idea_fanout(monkeypatch):
    import lib.research.recipe as recipe

    offered = 0
    searched = []
    harvested = []
    harvest_kwargs = []

    def results():
        nonlocal offered
        for index in range(1_000):
            offered += 1
            yield {
                'arxiv_id': f'2501.{index:05d}',
                'title': f'  Search title {index}  ',
            }

    def search(query, max_results=20):
        searched.append(max_results)
        return results()

    def harvest(ids, **kwargs):
        harvested.extend(ids)
        harvest_kwargs.append(kwargs)
        return {
            'parsed': len(ids), 'cache_hits': 0, 'errors': 0,
            'results': [
                {'arxivId': arxiv_id, 'status': 'parsed'} for arxiv_id in ids],
        }

    monkeypatch.setattr(recipe, '_search_arxiv', search)
    monkeypatch.setattr(recipe, '_harvest_batch', harvest)
    recipe._run_harvest({
        'direction': 'bounded', 'folder_id': 'folder', 'user_id': 1,
        'harvest_n': 100_000,
    })
    assert searched == [20]
    assert offered == 20
    assert len(harvested) == 20
    assert harvest_kwargs[0]['titles_by_arxiv_id'] == {
        f'2501.{index:05d}': f'Search title {index}'
        for index in range(20)
    }

    generated = []
    monkeypatch.setattr(
        recipe, '_generate_ideas',
        lambda *args, **kwargs: (
            generated.append(kwargs['n_ideas']) or {
                'ok': True, 'accepted': [], 'rejected': [{'id': 'r'}],
                'threshold': 4.0,
            }))
    recipe._run_ideate({
        'direction': 'bounded', 'lang': 'en', 'n_ideas': 100_000,
        'user_id': 1, 'artifacts': {'survey': {'open_gaps': {'open_gaps': []}}},
    })
    assert generated == [12]


def test_chinese_direction_uses_one_english_discovery_alias(monkeypatch):
    import lib.research.recipe as recipe

    translated = []
    searched = []
    harvested = []

    def translate(direction, *, abort_check=None):
        translated.append(direction)
        return (
            'efficient knowledge injection methods for large language models',
            {'prompt_tokens': 12, 'completion_tokens': 8,
             '_dispatch': {'model': 'translator'}},
        )

    def search(query, max_results=20):
        searched.append(query)
        return [
            {'arxiv_id': f'2501.0000{index}', 'title': f'Paper {index}'}
            for index in range(3)
        ]

    def harvest(ids, **kwargs):
        harvested.append(list(ids))
        return {
            'parsed': len(ids), 'cache_hits': 0, 'errors': 0,
            'results': [
                {'arxivId': arxiv_id, 'status': 'parsed'} for arxiv_id in ids
            ],
        }

    monkeypatch.setattr(recipe, '_translate_direction_for_search', translate)
    monkeypatch.setattr(recipe, '_search_arxiv', search)
    monkeypatch.setattr(recipe, '_harvest_batch', harvest)
    ctx = {
        'direction': '大模型高效知识注入方法',
        'folder_id': 'folder', 'user_id': 1, 'harvest_n': 3,
    }

    first = recipe._run_harvest(ctx)
    second = recipe._run_harvest(ctx)

    assert translated == ['大模型高效知识注入方法']
    assert searched == [
        'efficient knowledge injection methods for large language models',
        'efficient knowledge injection methods for large language models',
    ]
    assert len(harvested) == 2
    assert first['usage']['calls'] == 1
    assert first['usage']['prompt_tokens'] == 12
    assert second['usage'] == first['usage']
