"""Public application protocol, evidence retention and ambiguous-write tests."""
import copy
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from evaluations.application_agent import run_trial, validate_request

pytestmark = pytest.mark.unit


def request_for(tmp_path):
    return {'model': {'creator_id': 'zhipu', 'model_id': 'glm-5.3'},
            'provider_id': 'benchmark', 'instruction': 'Create the requested artifact.',
            'project_root': str(tmp_path), 'output_root': str(tmp_path / 'evidence'),
            'config': {'maxTokens': 65536}, 'thinking': 'max',
            'timeout_seconds': 30, 'artifact_max_bytes': 1024 * 1024}


class Application:
    def __init__(self, *, mode='success'):
        self.mode, self.calls, self.pages = mode, [], 0
        self.state = {'id': 'task-owned', 'status': 'done', 'content': 'artifact created',
                      'thinking': '', 'finishReason': 'stop', 'model': 'glm-5.3',
                      'usage': {'prompt_tokens': 8, 'completion_tokens': 4}}
        self.cancelled = False

    def call(self, path, body=None, **kwargs):
        self.calls.append((path, copy.deepcopy(body)))
        if path == '/api/health':
            return {'storage': {'backend': 'sqlite' if self.mode == 'wrong_store' else 'tofudb',
                                'ready': True, 'pid': 100, 'port': 30001}}
        if path == '/api/v4/agent/run':
            if self.mode == 'lost_admission':
                raise TimeoutError('Bearer sensitive-fixture')
            return {'task_id': self.state['id'], 'provider_id': 'changed' if self.mode == 'provider_drift' else 'benchmark'}
        if '/by-conv/' in path:
            return {'tasks': [{'id': self.state['id']}]}
        if path.endswith('/abort'):
            self.cancelled = True
            self.state['status'] = 'aborted'
            if self.mode == 'lost_admission':
                raise TimeoutError('lost cancel reply')
            return {'aborted': True}
        if '/events?' in path:
            self.pages += 1
            if self.mode == 'foreign_event':
                return {'events': [{'type': 'done', 'taskId': 'foreign'}],
                        'next_cursor': 1, 'done': True, 'caught_up': True}
            if self.mode == 'stuck_cursor':
                return {'events': [{'type': 'phase'}], 'next_cursor': 0,
                        'done': False, 'caught_up': False}
            # Sparse producer cursors must not be replaced by len(events).
            if self.pages == 1:
                return {'events': [{'type': 'phase', 'taskId': self.state['id']}],
                        'next_cursor': 7, 'done': False, 'caught_up': False}
            assert path.endswith('cursor=7')
            return {'events': [{'type': 'done', 'taskId': self.state['id']}],
                    'next_cursor': 20, 'done': True, 'caught_up': True}
        if path.endswith('?view=durable'):
            if self.mode == 'missing_durable':
                raise HTTPError(path, 404, 'not yet committed', {}, None)
            state = dict(self.state)
            if self.mode != 'old_server':
                state['readSource'] = 'durable'
            if self.mode == 'durable_mismatch':
                state['content'] = 'different committed answer'
            return state
        if path == '/api/v4/tasks/task-owned':
            return dict(self.state, readSource='live')
        raise AssertionError(path)


def test_production_request_and_exact_durable_readback(tmp_path):
    api = Application()
    report = run_trial(request_for(tmp_path), api)
    assert report['trialState'] == 'completed'
    assert report['durableReadbackVerified'] is True
    assert report['formal_score'] is None
    posts = [body for path, body in api.calls if path == '/api/v4/agent/run']
    assert len(posts) == 1
    body = posts[0]
    assert 'tools' not in body  # Application owns the full native tool surface.
    assert body['config'] == {'maxTokens': 65536, 'thinking': 'max',
                              'projectPath': str(tmp_path), 'disableModelFallback': True}
    assert body['routing'] == {'preferred_provider_id': 'benchmark'}
    assert body['model'] == {'creator_id': 'zhipu', 'model_id': 'glm-5.3'}
    files = {p.name for p in (tmp_path / 'evidence').iterdir()}
    assert {'events.jsonl', 'runtime-evidence.json', 'durable-evidence.json', 'result.json'} <= files
    assert not api.cancelled


def test_reused_conversation_retains_explicit_inputs_and_native_tool_declarations(tmp_path):
    request = request_for(tmp_path)
    request.update(conversation_id='owned-facts-context', tool_schemas=[])
    api = Application()
    report = run_trial(request, api)
    assert report['trialState'] == 'completed'
    body = next(body for path, body in api.calls if path == '/api/v4/agent/run')
    assert body['conversation_id'] == 'owned-facts-context'
    assert body['id'] != body['conversation_id']
    assert body['messages'] == [{'role': 'user', 'content': request['instruction']}]
    assert body['config']['_explicitToolSchemas'] == []
    assert 'tools' not in body
    assert json.loads((tmp_path / 'evidence/request.json').read_text()) == request


def test_lost_admission_in_reused_conversation_cannot_cancel_a_preexisting_task(tmp_path):
    request = request_for(tmp_path)
    request['conversation_id'] = 'owned-facts-context'
    api = Application(mode='lost_admission')
    report = run_trial(request, api, settlement_seconds=0)
    assert report['cleanupVerified'] is False and report['taskId'] is None
    assert not api.cancelled and not any('/by-conv/' in path for path, _ in api.calls)


@pytest.mark.parametrize('mode', ['wrong_store', 'old_server', 'provider_drift',
                                 'foreign_event', 'stuck_cursor', 'durable_mismatch', 'missing_durable'])
def test_invalid_authority_or_evidence_cannot_complete(tmp_path, mode):
    api = Application(mode=mode)
    report = run_trial(request_for(tmp_path), api, settlement_seconds=0)
    assert report['trialState'] == 'failed'
    assert report['durableReadbackVerified'] is False
    assert report['formal_score'] is None
    if mode == 'wrong_store':
        assert not any(path == '/api/v4/agent/run' for path, _ in api.calls)
    if mode in {'provider_drift', 'foreign_event', 'stuck_cursor'}:
        assert api.cancelled


def test_lost_admission_recovers_and_cancels_without_resubmitting(tmp_path):
    api = Application(mode='lost_admission')
    report = run_trial(request_for(tmp_path), api)
    assert sum(path == '/api/v4/agent/run' for path, _ in api.calls) == 1
    assert sum(path.endswith('/abort') for path, _ in api.calls) == 1
    assert api.cancelled and report['cleanupVerified']
    assert report['trialState'] == 'failed'
    assert report['taskId'] == 'task-owned'
    assert 'sensitive-fixture' not in (tmp_path / 'evidence' / 'result.json').read_text()


def test_unknown_admission_is_reported_as_unresolved(tmp_path):
    class Lost(Application):
        def call(self, path, body=None, **kwargs):
            if '/by-conv/' in path:
                return {'tasks': []}
            return super().call(path, body, **kwargs)
    report = run_trial(request_for(tmp_path), Lost(mode='lost_admission'), settlement_seconds=0)
    assert report['cleanupVerified'] is False
    assert report['taskId'] is None


def test_completed_model_error_is_not_success(tmp_path):
    api = Application()
    api.state.update(status='error', error={'code': 'provider_error'})
    report = run_trial(request_for(tmp_path), api)
    assert report['durableReadbackVerified']
    assert report['trialState'] == 'agent_failed'


@pytest.mark.parametrize('finish', ['', 'abnormal_stop', 'length', 'tool_calls'])
def test_terminal_transport_does_not_override_native_failure_verdict(tmp_path, finish):
    api = Application()
    api.state['finishReason'] = finish
    report = run_trial(request_for(tmp_path), api)
    assert report['durableReadbackVerified']
    assert report['trialState'] == 'agent_failed'


def test_explicit_budget_and_new_artifact_directory_required(tmp_path):
    request = request_for(tmp_path)
    request['config'] = {}
    with pytest.raises(ValueError, match='output token'):
        validate_request(request)
    request = request_for(tmp_path)
    Path(request['output_root']).mkdir()
    with pytest.raises(ValueError, match='new directory'):
        validate_request(request)


def test_expired_trial_is_cancelled_and_not_resubmitted(tmp_path, monkeypatch):
    import evaluations.application_agent as driver
    clock = [0.0]
    monkeypatch.setattr(driver.time, 'monotonic', lambda: clock[0])
    class Slow(Application):
        def call(self, path, body=None, **kwargs):
            if '/events?' in path:
                assert 0 < kwargs['timeout'] <= 10
                clock[0] = 31.0
                return {'events': [], 'next_cursor': 0, 'done': False, 'caught_up': True}
            return super().call(path, body, **kwargs)
    api = Slow()
    report = run_trial(request_for(tmp_path), api)
    assert report['failureType'] == 'TimeoutError'
    assert api.cancelled and report['cleanupVerified']
    assert sum(path == '/api/v4/agent/run' for path, _ in api.calls) == 1


def test_evidence_overflow_retains_receipt_within_total_budget(tmp_path):
    request = request_for(tmp_path)
    request['artifact_max_bytes'] = 2048
    request['instruction'] = 'x' * 4096
    api = Application()
    report = run_trial(request, api)
    output = Path(request['output_root'])
    assert report['trialState'] == 'failed' and report['cleanupVerified']
    assert (output / 'result.json').is_file()
    assert sum(p.stat().st_size for p in output.iterdir()) <= request['artifact_max_bytes']
    assert not api.calls


@pytest.mark.parametrize('change_tokens', [False, True])
def test_committed_usage_uses_production_projection_without_hiding_token_drift(tmp_path, change_tokens):
    class Projection(Application):
        def call(self, path, body=None, **kwargs):
            value = super().call(path, body, **kwargs)
            if path.endswith('?view=durable'):
                value['usage'] = {k: v for k, v in value['usage'].items() if not k.startswith('_wire_')}
                if change_tokens:
                    value['usage']['completion_tokens'] += 1
            return value
    api = Projection()
    api.state['usage']['_wire_fp'] = 'transient wire evidence'
    report = run_trial(request_for(tmp_path), api, settlement_seconds=0)
    assert report['durableReadbackVerified'] is (not change_tokens)
    assert report['trialState'] == ('failed' if change_tokens else 'completed')
