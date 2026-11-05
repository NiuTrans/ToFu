"""Terminal task faults never certify answers or keep spending a failed route."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.programmatic_pilot.native_api import HTTPRuntime
from evaluations.search_quality import runner
from evaluations.search_quality.contracts import DDGS_VERSION
from tests.evaluations.test_search_quality import ReplayAPI, evidence

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


class ErrorAPI(ReplayAPI):
    def __init__(self, terminal_error):
        super().__init__()
        self.terminal_error = terminal_error

    def call(self, path, body=None):
        if path.endswith('/capabilities'):
            return {'version': 'test'}
        result = super().call(path, body)
        if path.endswith('/owned-task'):
            result['error'] = deepcopy(self.terminal_error)
        return result


class EvidenceWorker:
    proxy = ''

    def __init__(self, source=''):
        self.source = source

    def call(self, arm, operation, args=None, **kwargs):
        if operation == 'identity':
            return {'worker_settled': True, 'identity': {
                'tofu_source_sha256': self.source, 'ddgs': DDGS_VERSION}}
        return deepcopy(evidence()[1][0 if operation == 'search' else 1]['output'])


@pytest.mark.parametrize('terminal_error', [
    {'kind': 'quota', 'retryable': False, 'statusCode': 402, 'detail': 'PRIVATE_CREDENTIAL'},
    {'kind': 'transport', 'retryable': True, 'raw': 'PRIVATE_CREDENTIAL'},
    {'unknown': 'PRIVATE_CREDENTIAL'}, 'PRIVATE_CREDENTIAL',
])
def test_done_with_error_cannot_certify_a_correct_answer_after_a_successful_round(tmp_path, terminal_error):
    runtime = HTTPRuntime(ErrorAPI(terminal_error), {'creator_id': 'test', 'model_id': 'model'}, 'test')
    try:
        report = asyncio.run(runner.run_one(runtime, EvidenceWorker(), evidence()[0],
                                           'tofu', 0, tmp_path/'run', 30))
        assert report['score']['passed']  # Source/quote checks alone would pass.
        assert report['status'] == 'done' and report['rounds']
        assert report['terminal_error'] is True
        assert not report['provenance_valid'] and not report['passed']
        assert 'PRIVATE_CREDENTIAL' not in json.dumps(report)
        assert report['cleanup_failure'] is None and runtime.in_flight == 0
        summary = runner.summarize([report, {**report, 'arm': 'ddgs'}], 2)
        assert summary['complete'] and not summary['pairs'][0]['comparable']
        assert summary['arms']['tofu']['terminal_errors'] == 1
    finally:
        runtime.close()


@pytest.mark.parametrize('retryable', [False, True])
@pytest.mark.parametrize('kind', ['quota', 'permission', 'oauth_reauth'])
def test_declared_nonretryable_fault_stops_further_dispatch_and_keeps_incomplete_summary(
        tmp_path, monkeypatch, retryable, kind):
    error = {'kind': kind, 'retryable': retryable, 'statusCode': 402,
             'detail': 'PRIVATE_CREDENTIAL'}
    api = ErrorAPI(error)
    monkeypatch.setattr(runner, 'JsonAPI', lambda *a, **kw: api)
    tasks = [evidence()[0], {**evidence()[0], 'id': 'second-task'}]
    registration = runner.manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'},
                                   'test', 1, 30, task_pack=tasks)
    args = SimpleNamespace(output=tmp_path/'cohort', origin='http://127.0.0.1:15000',
                           api_token_env=None, provider='test', trials=1, timeout=30)
    workers = EvidenceWorker(registration['tofu_source_sha256'])
    if retryable:
        asyncio.run(runner.live(args, ROOT, registration, workers))
    else:
        with pytest.raises(RuntimeError, match='Non-retryable task error'):
            asyncio.run(runner.live(args, ROOT, registration, workers))
    summary = json.loads((args.output/'summary.json').read_text())
    admissions = sum(path == '/api/v4/agent/run' for path, _ in api.calls)
    assert admissions == (4 if retryable else 1)
    assert summary['complete'] is retryable
    assert all(not pair['comparable'] for pair in summary['pairs'])
    if not retryable:
        assert summary['stop_reason']['kind'] == 'non_retryable_task_error'
        assert summary['stop_reason']['task_error'] == {
            'kind': kind, 'retryable': False, 'statusCode': 402}
    assert 'PRIVATE_CREDENTIAL' not in json.dumps(summary)


def test_summary_rejects_inconsistent_imported_error_record():
    records = [{'task': 'case', 'trial': 0, 'arm': arm, 'passed': True,
                'provenance_valid': True, 'schema_fingerprints': ['same'],
                'seconds': 0, 'task_error': {'kind': 'quota'}, 'terminal_error': True}
               for arm in ('tofu', 'ddgs')]
    summary = runner.summarize(records, 2)
    assert not summary['pairs'][0]['comparable']
    assert summary['arms']['tofu']['passes'] == 0


def test_budget_exhaustion_is_comparable_but_cannot_certify_a_partial_answer(tmp_path):
    runtime = HTTPRuntime(ErrorAPI({'kind': 'budget_exceeded', 'retryable': False}),
                          {'creator_id': 'test', 'model_id': 'model'}, 'test')
    try:
        report = asyncio.run(runner.run_one(runtime, EvidenceWorker(), evidence()[0],
                                           'tofu', 0, tmp_path/'run', 30))
        assert report['score']['passed'] and report['terminal_error']
        assert report['provenance_valid'] and not report['passed']
        summary = runner.summarize([report, {**report, 'arm': 'ddgs'}], 2)
        assert summary['complete'] and summary['pairs'][0]['comparable']
        assert summary['arms']['tofu']['budget_exhaustions'] == 1
        assert summary['arms']['tofu']['passes'] == 0
    finally:
        runtime.close()


@pytest.mark.parametrize('kind', ['budget_exceeded', 'content_refused', 'bad_request'])
def test_task_local_errors_do_not_prevent_the_other_arm_or_later_questions(tmp_path, monkeypatch, kind):
    api = ErrorAPI({'kind': kind, 'retryable': False})
    monkeypatch.setattr(runner, 'JsonAPI', lambda *a, **kw: api)
    tasks = [evidence()[0], {**evidence()[0], 'id': 'second-task'}]
    registration = runner.manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'},
                                   'test', 1, 30, task_pack=tasks)
    args = SimpleNamespace(output=tmp_path/'cohort', origin='http://127.0.0.1:15000',
                           api_token_env=None, provider='test', trials=1, timeout=30)
    asyncio.run(runner.live(args, ROOT, registration,
                           EvidenceWorker(registration['tofu_source_sha256'])))
    summary = json.loads((args.output/'summary.json').read_text())
    assert sum(path == '/api/v4/agent/run' for path, _ in api.calls) == 4
    assert summary['complete'] and summary['stop_reason'] is None
    assert all(pair['comparable'] == (kind == 'budget_exceeded') and not any(pair['passes'].values())
               for pair in summary['pairs'])
    assert all(arm['budget_exhaustions'] == arm['provenance_valid'] == (2 if kind == 'budget_exceeded' else 0)
               for arm in summary['arms'].values())
