"""Paper long-agent arm identity, dedup, and canonical-cache isolation."""

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.unit
TEST_OWNER_USER_ID = 1


def test_request_policy_fingerprint_is_stable_and_output_sensitive():
    from lib.paper.request_policy import paper_request_policy_telemetry

    left = {
        'tools': {'schemaBudgetTokens': 4_000, 'resultEnvelope': 'legacy'},
        'responses': {'promptProfile': 'full'},
    }
    reordered = {
        'responses': {'promptProfile': 'full'},
        'tools': {'resultEnvelope': 'legacy', 'schemaBudgetTokens': 4_000},
    }
    a = paper_request_policy_telemetry(model='kimi-k3', config=left)
    b = paper_request_policy_telemetry(model='kimi-k3', config=reordered)
    other_model = paper_request_policy_telemetry(
        model='other-model', config=left)
    other_arm = paper_request_policy_telemetry(
        model='kimi-k3', config={
            **left, 'tools': {
                **left['tools'], 'schemaBudgetTokens': 0,
            },
        })

    assert a == b
    assert a['cacheMode'] == 'request_local'
    assert a['executionFingerprint'] != other_model['executionFingerprint']
    assert a['executionFingerprint'] != other_arm['executionFingerprint']
    assert paper_request_policy_telemetry(
        model='kimi-k3',
        config={'paperInsightEnabled': False},
    )['cacheMode'] == 'shared'


def test_report_dedup_requires_exact_model_and_config():
    from lib.paper.report_runtime import _new_report_task, _report_index_get

    suffix = uuid.uuid4().hex
    phash = f'policy-dedup-{suffix}'
    control = _new_report_task(
        f'report-control-{suffix}', phash, 'en', 'kimi-k3',
        config={'tools': {
            'schemaBudgetTokens': 0,
            'resultEnvelope': 'legacy',
        }},
        user_id=TEST_OWNER_USER_ID,
    )
    candidate = _new_report_task(
        f'report-candidate-{suffix}', phash, 'en', 'kimi-k3',
        config={'tools': {
            'schemaBudgetTokens': 4_000,
            'resultEnvelope': 'legacy',
        }},
        user_id=TEST_OWNER_USER_ID,
    )
    other_model = _new_report_task(
        f'report-model-{suffix}', phash, 'en', 'other-model',
        config=control['config'], user_id=TEST_OWNER_USER_ID,
    )

    assert len({
        control['execution_fingerprint'],
        candidate['execution_fingerprint'],
        other_model['execution_fingerprint'],
    }) == 3
    for task in (control, candidate, other_model):
        assert _report_index_get(
            phash, 'en', user_id=TEST_OWNER_USER_ID,
            execution_fingerprint=task['execution_fingerprint']) is task
    # The reattach-only compatibility lookup returns the latest registration;
    # joining code always uses the exact form above.
    assert _report_index_get(
        phash, 'en', user_id=TEST_OWNER_USER_ID) is other_model


def test_request_local_report_run_never_mutates_canonical_artifacts(monkeypatch):
    import lib.paper.report_engine.worker as report_engine
    from lib.paper.report_runtime import _new_report_task

    mutations = []

    class ForbiddenRepository:
        def __init__(self, *_args, **_kwargs):
            mutations.append('report-cache')

        def put_report(self, *_args, **_kwargs):
            raise AssertionError('request-local arm wrote canonical report cache')

    def forbidden(name):
        def call(*_args, **_kwargs):
            mutations.append(name)
            raise AssertionError(f'request-local arm executed {name}')
        return call

    def fake_dispatch(messages, on_content=None, **_kwargs):
        del messages
        content = '# Isolated report\n\n## ⚡ TL;DR\nMeasured independently.'
        if on_content:
            on_content(content)
        return (
            {'role': 'assistant', 'content': content, 'tool_calls': []},
            'stop',
            {'_dispatch': {}},
        )

    monkeypatch.setattr(report_engine, 'dispatch_stream', fake_dispatch)
    monkeypatch.setattr(report_engine, 'PaperArtifactRepository',
                        ForbiddenRepository)
    monkeypatch.setattr(report_engine, 'lookup_paper_title',
                        lambda *_args, **_kwargs: '')
    monkeypatch.setattr(report_engine, 'backfill_library_title',
                        forbidden('title-backfill'))
    monkeypatch.setattr(report_engine, '_maybe_run_insight',
                        forbidden('insight'))
    monkeypatch.setattr(report_engine, '_maybe_run_termfill',
                        forbidden('termfill'))

    suffix = uuid.uuid4().hex
    task = _new_report_task(
        f'report-isolated-{suffix}', f'policy-cache-{suffix}', 'en',
        'kimi-k3',
        config={
            'responses': {'promptProfile': 'full'},
            'tools': {
                'schemaBudgetTokens': 0,
                'resultEnvelope': 'legacy',
            },
            'context': {'globalBudgetTokens': 0},
            'compaction': {'strategy': 'fixed'},
            'orchestration': {'policy': 'v1'},
            'paperInsightEnabled': True,
            'paperCheckpointsEnabled': True,
        },
        user_id=TEST_OWNER_USER_ID,
    )
    report_engine.run_report_task(task, [
        {'role': 'system', 'content': 'system'},
        {'role': 'user', 'content': 'paper'},
    ], [])

    assert task['status'] == 'done', task.get('error')
    assert task['requestPolicyV1']['cacheMode'] == 'request_local'
    assert task['toolResultPolicyV1']['resultEnvelope'] == 'legacy'
    assert task['report_meta']['requestPolicyV1'] == task['requestPolicyV1']
    assert mutations == []


@pytest.mark.anyio
async def test_report_start_route_bypasses_cache_for_explicit_arm(monkeypatch):
    from quart import Quart

    import routes.paper_pkg._report as report_route
    from lib.paper.request_policy import paper_execution_fingerprint

    config = {
        'tools': {
            'schemaBudgetTokens': 4_000,
            'resultEnvelope': 'legacy',
        },
    }
    expected_fingerprint = paper_execution_fingerprint(
        model='kimi-k3', config=config)
    observed = {}

    class Repository:
        def __init__(self, owner_user_id):
            assert owner_user_id == TEST_OWNER_USER_ID

        def get_report(self, *_args, **_kwargs):
            raise AssertionError('explicit arm read canonical report cache')

    async def parse_body():
        return {
            'paper_text': 'paper evidence ' * 20,
            'paper_hash': 'a' * 32,
            'lang': 'en',
            'model': 'kimi-k3',
            'config': config,
        }

    def exact_lookup(phash, lang, *, user_id, execution_fingerprint):
        observed.update({
            'phash': phash,
            'lang': lang,
            'user_id': user_id,
            'execution_fingerprint': execution_fingerprint,
        })
        return {
            'task_id': 'existing-exact-arm',
            'status': 'running',
        }

    monkeypatch.setattr(report_route, 'PaperArtifactRepository', Repository)
    monkeypatch.setattr(report_route, 'async_parse_body', parse_body)
    monkeypatch.setattr(report_route, 'request_user_id',
                        lambda: TEST_OWNER_USER_ID)
    monkeypatch.setattr(report_route, 'load_image_manifest',
                        lambda _phash: [])
    monkeypatch.setattr(report_route, '_report_index_get', exact_lookup)

    app = Quart(__name__)
    async with app.test_request_context(
            '/api/v1/paper/report/start', method='POST'):
        response = await report_route.start_report_task()
    if isinstance(response, tuple):
        response = response[0]
    payload = await response.get_json()

    assert payload['ok'] is True
    assert payload['task_id'] == 'existing-exact-arm'
    assert payload['existed'] is True
    assert observed == {
        'phash': 'a' * 32,
        'lang': 'en',
        'user_id': TEST_OWNER_USER_ID,
        'execution_fingerprint': expected_fingerprint,
    }
