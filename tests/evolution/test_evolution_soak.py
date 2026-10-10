"""The headless probe must detect broken handoffs and preserve bounded receipts."""
from __future__ import annotations

import json
from io import BytesIO

import pytest

from evaluations.evolution import soak
from evaluations.evolution.soak import observe_run

pytestmark = pytest.mark.unit


def _run(status, *, task='execute', tokens=0, receipts=None):
    return {'run_id': 'run-probe', 'status': status, 'task_id': task,
            'episode_id': 'ep-probe', 'actuals': {'tokens': tokens, 'tool_calls': 0},
            'receipts': receipts or [], 'result': 'Usable result' if status == 'completed' else '',
            'review': {'outcome': 'success', 'criterion_results': [
                {'outcome': 'pass'} for _ in range(3)]} if status == 'completed' else None}


def test_headless_probe_follows_real_stage_handoff_and_reads_durable_result():
    snapshots = [_run('executing'), _run('reviewing', task='review', tokens=11),
                 _run('completed', task='review', tokens=23,
                      receipts=[
                          {'task_id': 'execute', 'stage': 'executing',
                           'model': {'creator_id': 'test', 'model_id': 'fixed'},
                           'actuals': {'tokens': 11, 'tool_calls': 1}},
                          {'task_id': 'review', 'stage': 'reviewing',
                           'model': {'creator_id': 'test', 'model_id': 'fixed'},
                           'actuals': {'tokens': 12, 'tool_calls': 0}}])]
    calls = []
    clock = [0.0]
    model = {'creator_id': 'test', 'model_id': 'fixed'}
    def request(method, path, body, key):
        calls.append((method, path, key))
        if method == 'POST':
            assert body == {'objective': 'Explain', 'model': model}
            return {'run': snapshots[0]}
        if len([call for call in calls if call[0] == 'GET']) == 1:
            return {'run': snapshots[1]}
        return {'run': snapshots[2]}
    def sleep(seconds):
        clock[0] += seconds
    result = observe_run(request, objective='Explain', key='stable-key',
                         poll_seconds=1, timeout_seconds=10,
                         model=model, now=lambda: clock[0], sleep=sleep)
    assert result['complete'] is True
    assert result['stage_started_seconds']['reviewing'] == 1
    assert result['task_count'] == 2 and result['receipt_count'] == 2
    assert result['tokens'] == 23
    assert result['stage_usage'] == [
        {'stage': 'executing', 'model': model, 'tokens': 11, 'tool_calls': 1},
        {'stage': 'reviewing', 'model': model, 'tokens': 12, 'tool_calls': 0}]
    assert len([call for call in calls if call[0] == 'POST']) == 2
    assert all(call[2] == 'stable-key' for call in calls if call[0] == 'POST')


def test_headless_probe_rejects_fabricated_success_and_duplicate_receipts():
    complete = _run('completed', receipts=[{'task_id': 'execute'}])
    complete['review']['criterion_results'][1]['outcome'] = 'unknown'
    def request(method, path, body, key):
        return {'run': complete}
    result = observe_run(request, objective='Explain', key='same',
                         poll_seconds=1, timeout_seconds=2)
    assert result['complete'] is False
    complete['receipts'] = [{'task_id': 'execute'}, {'task_id': 'execute'}]
    with pytest.raises(RuntimeError, match='Duplicate stage receipts'):
        observe_run(request, objective='Explain', key='same',
                    poll_seconds=1, timeout_seconds=2)


def test_headless_probe_detects_budget_counter_regression():
    initial = _run('executing', tokens=0)
    first = _run('reviewing', task='review', tokens=11)
    broken = _run('reviewing', task='review', tokens=8)
    calls = [0]
    def request(method, path, body, key):
        calls[0] += 1
        return {'run': initial if calls[0] <= 2 else first if calls[0] == 3 else broken}
    clock = [0.0]
    with pytest.raises(RuntimeError, match='tokens counter decreased'):
        observe_run(request, objective='Explain', key='same',poll_seconds=1,
                    timeout_seconds=5,now=lambda:clock[0],
                    sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))


def test_timed_out_probe_cancels_the_bound_run():
    clock = [0.0]
    calls = []
    cancelled = False
    def request(method, path, body, key):
        nonlocal cancelled
        calls.append((method, path))
        if path.endswith('/cancel'):
            cancelled = True
        return {'run': _run('cancelled' if cancelled else 'settling', tokens=17,
                            receipts=[{'task_id': 'execute'}, {'task_id': 'review'}])}
    result = observe_run(request, objective='Explain', key='stable',
                         poll_seconds=1, timeout_seconds=2,
                         now=lambda: clock[0],
                         sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert ('POST', '/api/v4/evolution/runs/run-probe/cancel') in calls
    assert result['status'] == 'cancelled' and result['timed_out'] is True
    assert result['complete'] is False and result['tokens'] == 17
    assert result['timeout_stage'] == 'settling' and not result['provider_usage_unknown']


def test_timed_out_active_model_marks_unreceipted_provider_usage_unknown():
    clock = [0.0]
    cancelled = False
    def request(method, path, body, key):
        nonlocal cancelled
        if path.endswith('/cancel'):
            cancelled = True
        return {'run': _run('cancelled' if cancelled else 'executing')}
    result = observe_run(request, objective='Explain', key='stable',
                         poll_seconds=1, timeout_seconds=2,
                         now=lambda: clock[0],
                         sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result['timed_out'] and result['timeout_stage'] == 'executing'
    assert result['provider_usage_unknown'] is True


def test_probe_recovers_from_short_server_outage_and_reads_terminal_state():
    clock = [0.0]
    reads = [0]
    terminal = _run('needs_attention')
    terminal['error'] = 'task_unavailable; execution was not replayed'
    def request(method, path, body, key):
        if method == 'GET':
            reads[0] += 1
            if reads[0] == 1:
                raise soak.urllib.error.URLError('connection refused')
            return {'run': terminal}
        return {'run': _run('executing')}
    result = observe_run(request, objective='Explain', key='stable',
                         poll_seconds=1, timeout_seconds=10,
                         now=lambda: clock[0],
                         sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result['status'] == 'needs_attention' and result['complete'] is False
    assert result['outage_count'] == 1 and result['outage_seconds'] == 1
    assert result['error'].startswith('task_unavailable')
    assert result['provider_usage_unknown'] is True


def test_live_probe_cli_writes_bounded_receipt_without_network(monkeypatch, tmp_path, capsys):
    completed = _run('completed', tokens=23,
                     receipts=[{'task_id': 'execute'}, {'task_id': 'review'}])
    requests = []

    def request(base_url, token, method, path, body=None, *, idempotency_key=''):
        requests.append((method, path, body, idempotency_key))
        return {'run': completed}

    monkeypatch.setattr(soak, '_request', request)
    monkeypatch.delenv('TOFU_API_KEY', raising=False)
    output = tmp_path / 'receipts.jsonl'
    model = {'creator_id': 'tofu-test', 'model_id': 'fixed'}
    assert soak.main(['--live', '--runs', '2', '--max-total-tokens', '20',
                      '--reserve-tokens-per-run', '0',
                      '--objective', 'Explain',
                      '--model-ref', json.dumps(model), '--output', str(output)]) == 0
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    summary = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['complete'] is True
    assert summary['runs'] == 1 and summary['tokens'] == 23
    assert summary['p50_tokens_per_completed_goal'] == 23
    assert rows[0]['harness_version'] == 7 and len(rows[0]['objective_sha256']) == 64
    assert rows[0]['requested_model_ref'] == model
    assert len([call for call in requests if call[0] == 'POST']) == 2
    assert requests[0][2] == {'objective': 'Explain',
                              'model': model, 'budget': {'max_tokens': 20}}
    assert rows[0]['admitted_max_tokens'] == 20
    assert requests[0][3] == requests[1][3]


def test_http_client_unwraps_deployed_v4_envelope(monkeypatch):
    class Response(BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(soak.urllib.request, 'urlopen',
                        lambda *_args, **_kwargs: Response(
                            b'{"data":{"run":{"run_id":"run-live"}},"meta":{}}'))
    assert soak._request('http://127.0.0.1:15000', '', 'GET', '/api/v4/evolution/runs/run-live') == {
        'run': {'run_id': 'run-live'}}


def test_live_probe_spaces_starts_across_duration(monkeypatch, tmp_path, capsys):
    clock = [0.0]
    started = []
    def request(_base, _token, method, path, _body=None, *, idempotency_key=''):
        if method == 'POST':
            started.append((idempotency_key, clock[0]))
        run = _run('completed', tokens=23, receipts=[{'task_id': idempotency_key}])
        run['run_id'] = idempotency_key if method == 'POST' else path.rsplit('/', 1)[-1]
        return {'run': run}
    monkeypatch.setattr(soak, '_request', request)
    output = tmp_path / 'paced.jsonl'
    assert soak.main(['--live', '--runs', '3', '--duration-seconds', '11',
                      '--min-start-interval-seconds', '10',
                      '--max-total-tokens', '100', '--reserve-tokens-per-run', '0',
                      '--output', str(output)], now=lambda: clock[0],
                     sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)) == 0
    assert [stamp for _key, stamp in started] == [0, 0, 10, 10]
    assert started[0][0] == started[1][0] != started[2][0]
    assert json.loads(capsys.readouterr().out)['completed'] == 2


@pytest.mark.parametrize('last_stage', ['executing','settling'])
@pytest.mark.parametrize('failure_type', [soak.urllib.error.URLError, TimeoutError])
def test_poll_outage_timeout_cancels_only_known_run_and_keeps_usage_unknown(last_stage, failure_type):
    clock = [0.0]; calls = []
    def request(method,path,body,key):
        calls.append((method,path))
        if path.endswith('/cancel'): return {'run':_run('cancelled',tokens=17)}
        if method == 'GET': raise failure_type('observation unavailable')
        return {'run':_run(last_stage,tokens=17)}
    # Readback recovers after the owned cancellation; the outage did not prove
    # the producer stopped or that its unreceipted model usage was zero.
    original = request
    def restored(method,path,body,key):
        if method == 'GET' and any(p.endswith('/cancel') for _,p in calls):
            return {'run':_run('cancelled',tokens=17)}
        return original(method,path,body,key)
    result = observe_run(restored,objective='Observe stable work',key='same',poll_seconds=1,
        timeout_seconds=2,now=lambda:clock[0],sleep=lambda sec:clock.__setitem__(0,clock[0]+sec))
    assert calls.count(('POST','/api/v4/evolution/runs/run-probe/cancel')) == 1
    assert result['status'] == 'cancelled' and result['timed_out']
    assert result['timeout_stage'] == last_stage and result['provider_usage_unknown']
    assert result['outage_count'] == 1 and result['outage_seconds'] == 1
    assert not result['complete']


def test_retry_outage_after_creation_still_cancels_the_known_run():
    clock = [0.0]; creations = [];cancelled = []
    def request(method,path,body,key):
        if path.endswith('/cancel'):
            cancelled.append(path);return {'run':_run('cancelled')}
        if method == 'POST':
            creations.append(key)
            if len(creations) > 1: raise soak.urllib.error.URLError('retry unavailable')
            return {'run':_run('executing')}
        return {'run':_run('cancelled')}
    result = observe_run(request,objective='Track this admitted run',key='stable',poll_seconds=1,
        timeout_seconds=2,now=lambda:clock[0],sleep=lambda sec:clock.__setitem__(0,clock[0]+sec))
    assert set(creations) == {'stable'}
    assert cancelled == ['/api/v4/evolution/runs/run-probe/cancel']
    assert result['timed_out'] and result['provider_usage_unknown'] and not result['complete']


@pytest.mark.parametrize('cancel_result', ['unavailable','foreign'])
def test_outage_cancel_without_confirmation_never_claims_producer_stopped(cancel_result):
    clock = [0.0];cancelled = []
    def request(method,path,body,key):
        if path.endswith('/cancel'):
            cancelled.append(path)
            if cancel_result == 'unavailable': raise soak.urllib.error.URLError('cancel unavailable')
            return {'run':dict(_run('cancelled'),run_id='foreign-run')}
        if method == 'GET': raise soak.urllib.error.URLError('poll unavailable')
        return {'run':_run('executing')}
    with pytest.raises(TimeoutError,match='run-probe.*cancel'):
        observe_run(request,objective='Observe known run',key='same',poll_seconds=1,timeout_seconds=2,
            now=lambda:clock[0],sleep=lambda sec:clock.__setitem__(0,clock[0]+sec))
    assert cancelled == ['/api/v4/evolution/runs/run-probe/cancel']


@pytest.mark.parametrize('failure_point', ['submission', 'poll', 'readback'])
def test_socket_timeout_recovers_with_same_submission_identity(failure_point):
    clock = [0.0]
    calls = []
    failed = [False]
    def request(method, path, body, key):
        calls.append((method, path, key))
        should_fail = ((failure_point == 'submission' and method == 'POST')
                       or (failure_point == 'poll' and method == 'GET')
                       or (failure_point == 'readback' and method == 'GET'))
        if should_fail and not failed[0]:
            failed[0] = True
            raise TimeoutError('socket read timed out')
        terminal = failure_point == 'readback' or method == 'GET'
        return {'run': _run('needs_attention' if terminal else 'executing')}
    result = observe_run(request, objective='Keep this run identity', key='stable',
        poll_seconds=1, timeout_seconds=10, now=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result['status'] == 'needs_attention'
    assert result['outage_count'] == 1 and result['outage_seconds'] == 1
    assert {key for method, _, key in calls if method == 'POST'} == {'stable'}
    assert not any(path.endswith('/cancel') for _, path, _ in calls)


@pytest.mark.parametrize('case', ['missing', 'unknown', 'failed', 'foreign', 'duplicate', 'unlinked', 'valid'])
def test_probe_completion_requires_all_frozen_acceptance_checks(case):
    run = _run('completed', receipts=[{'task_id': 'execute'}, {'task_id': 'review'}])
    run['acceptance_requirements'] = ['Original objective', 'Held-out comparison']
    run['require_requirement_originals'] = True
    rows = [{'requirement_id': key, 'outcome': 'pass', 'original_sources': [{'task_id': 'execute'}]}
            for key in run['acceptance_requirements']]
    if case == 'missing': rows.pop()
    elif case == 'unknown': rows[1]['outcome'] = 'unknown'
    elif case == 'failed': rows[1]['outcome'] = 'fail'
    elif case == 'foreign': rows[1]['requirement_id'] = 'Another objective'
    elif case == 'duplicate': rows[1]['requirement_id'] = rows[0]['requirement_id']
    elif case == 'unlinked': rows[1].pop('original_sources')
    run['review']['requirement_results'] = rows
    result = observe_run(lambda *_args: {'run': run}, objective='Explain', key='same',
                         poll_seconds=1, timeout_seconds=2)
    assert result['complete'] is (case == 'valid')


def test_live_probe_stops_after_inconsistent_acceptance_receipt(monkeypatch, tmp_path, capsys):
    run = _run('completed', tokens=23, receipts=[{'task_id': 'execute'}, {'task_id': 'review'}])
    run['acceptance_requirements'] = ['Evidence supports the original objective']
    run['review']['requirement_results'] = [{'requirement_id': run['acceptance_requirements'][0],
                                           'outcome': 'unknown'}]
    submissions = []
    def request(_base, _token, method, path, body=None, *, idempotency_key=''):
        if method == 'POST': submissions.append(idempotency_key)
        return {'run': run}
    monkeypatch.setattr(soak, '_request', request)
    output = tmp_path / 'rejected.jsonl'
    assert soak.main(['--live', '--runs', '2', '--reserve-tokens-per-run', '0',
                      '--output', str(output)]) == 1
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    summary = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['complete'] is False
    assert summary['completed'] == 0 and summary['tokens'] == 23
    assert len(submissions) == 2 and submissions[0] == submissions[1]


@pytest.mark.parametrize('scope', ['run', 'receipt', 'stage_loss'])
def test_completed_goal_with_incomplete_usage_stops_soak_and_excludes_cost_percentiles(monkeypatch, tmp_path, capsys, scope):
    run = _run('completed', tokens=23, receipts=[{'task_id': 'execute'}, {'task_id': 'review'}])
    if scope == 'run': run['actuals_complete'] = False
    elif scope == 'receipt': run['receipts'][0]['actuals_complete'] = False
    else: run['stage_losses'] = [{'task_id': 'lost-review'}]
    keys = []
    def request(_base, _token, method, path, body=None, *, idempotency_key=''):
        if method == 'POST': keys.append(idempotency_key)
        return {'run': run}
    monkeypatch.setattr(soak, '_request', request)
    output = tmp_path / 'lower-bound.jsonl'
    assert soak.main(['--live', '--runs', '3', '--reserve-tokens-per-run', '0',
                      '--output', str(output)]) == 0
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    summary = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['complete'] and rows[0]['provider_usage_unknown']
    assert len(keys) == 2 and keys[0] == keys[1]
    assert summary['completed'] == 1 and summary['tokens'] == 23
    assert summary['tokens_are_lower_bound'] and summary['unknown_usage_runs'] == 1
    assert summary['p50_tokens_per_completed_goal'] is None
    assert summary['p95_tokens_per_completed_goal'] is None


def test_soak_duration_cancels_active_run_with_remaining_probe_window(monkeypatch, tmp_path, capsys):
    clock = [0.0]
    cancelled = False
    commands = []
    def request(_base, _token, method, path, body=None, *, idempotency_key=''):
        nonlocal cancelled
        if method == 'POST': commands.append((path, clock[0]))
        if path.endswith('/cancel'): cancelled = True
        return {'run': _run('cancelled' if cancelled else 'executing', tokens=17,
                            receipts=[{'task_id':'execute'}] if cancelled else [])}
    monkeypatch.setattr(soak, '_request', request)
    output = tmp_path / 'duration.jsonl'
    assert soak.main(['--live', '--runs', '3', '--duration-seconds', '5',
                      '--run-timeout-seconds', '100', '--poll-seconds', '1',
                      '--reserve-tokens-per-run', '0', '--output', str(output)],
                     now=lambda: clock[0],
                     sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)) == 1
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    summary = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['timed_out']
    assert rows[0]['status'] == 'cancelled' and rows[0]['elapsed_seconds'] == 5
    assert commands[-1] == ('/api/v4/evolution/runs/run-probe/cancel', 5)
    assert summary['tokens'] == 17 and summary['elapsed_seconds'] == 5


@pytest.mark.parametrize('allowance', [0, -1, True, 1.5, '100'])
def test_invalid_probe_allowance_is_rejected_before_submission(allowance):
    def request(*args):
        pytest.fail('invalid allowance reached the API')
    with pytest.raises(ValueError, match='positive integer'):
        observe_run(request, objective='test', key='key', poll_seconds=1,
                    timeout_seconds=10, max_tokens=allowance)


def test_probe_budget_reduces_between_runs_and_never_submits_zero(monkeypatch, tmp_path, capsys):
    admitted = []
    def request(_base, _token, method, path, body=None, *, idempotency_key=''):
        if method == 'POST':
            admitted.append(body['budget']['max_tokens'])
        run = _run('completed', tokens=10, receipts=[{'task_id': 'task'}])
        run['run_id'] = idempotency_key if method == 'POST' else path.rsplit('/', 1)[-1]
        return {'run': run}
    monkeypatch.setattr(soak, '_request', request)
    output = tmp_path / 'bounded.jsonl'
    assert soak.main(['--live', '--runs', '3', '--max-total-tokens', '20',
                      '--reserve-tokens-per-run', '0', '--output', str(output)]) == 0
    assert admitted == [20, 20, 10, 10]
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row['admitted_max_tokens'] for row in rows] == [20, 10]
    assert json.loads(capsys.readouterr().out)['tokens'] == 20
