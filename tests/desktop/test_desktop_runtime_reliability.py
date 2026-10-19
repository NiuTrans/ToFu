"""Desktop poll liveness, replay safety and spooled transport contracts."""
import threading

import pytest
import requests

from lib.desktop_agent import _run
from lib.desktop_agent._runtime import Outbox, RuntimeState, retry_delay, validate_poll_response


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('body', [None, {}, {'commands': {}}, {'commands': [None]},
                                       {'commands': [{'id': '', 'type': 'x'}]}])
def test_invalid_poll_cannot_ack(body):
    with pytest.raises(ValueError):
        validate_poll_response(body)


def test_demand_execution_retains_every_id_without_replay_or_count_ceiling():
    state = RuntimeState(threading.Event())
    for index in range(70000):
        assert state.admit(str(index))
    assert len(state.running) == len(state.seen) == 70000
    for index in range(70000):
        state.finish(str(index))
    assert not state.admit('0')
    assert state.admit('next')
    state.finish('next')
    state.close()
    assert not state.running and not state.seen
    assert not state.admit('after-close')


def test_outbox_spools_past_poll_budget_and_acknowledges_only_sent_prefix():
    lock = threading.Condition(threading.RLock())
    box = Outbox(lock, threading.Event(), 1, 100)
    try:
        for index in range(32):
            box.append({'n': index})
        assert box.bytes > box.max_bytes
        assert list(box) == [{'n': 0}]
        first = box._records[0][0]
        with lock:
            del box[:1]
        assert not first.exists() and list(box) == [{'n': 1}]
        with pytest.raises(ValueError):
            box.append({'data': 'x' * 101})
        with pytest.raises(ValueError):
            del box[1:2]
        directory = box._temporary.name
    finally:
        box.close()
    from pathlib import Path
    assert not Path(directory).exists() and box.bytes == 0


def test_poll_keeps_heartbeating_and_retains_results_on_bad_200(monkeypatch):
    stop = threading.Event()
    release = threading.Event()
    started = threading.Event()
    executions = []
    sent = []
    command = {'id': 'one', 'type': 'slow', 'params': {}}
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'retry_delay', lambda *args: .001)

    def dispatch(*args):
        executions.append(args)
        started.set()
        assert release.wait(2)
        return {'value': 1}

    class Response:
        status_code = 200
        def __init__(self, body):
            self.body = body
        def json(self):
            return self.body

    def post(url, **kwargs):
        payload = kwargs['json']
        sent.append(payload)
        if len(sent) == 1:
            return Response({'commands': [command]})
        assert started.wait(1)
        if len(sent) == 2:
            release.set()
        if payload['results']:
            result_polls = [p for p in sent if p['results']]
            if len(result_polls) == 1:
                return Response({'landing': 'not Tofu'})
            assert result_polls[0]['results'] == payload['results']
            stop.set()
        if len(sent) > 30:
            stop.set()
            pytest.fail('No terminal result delivered')
        return Response({'commands': [command]})

    monkeypatch.setattr(_run, 'dispatch_command', dispatch)
    monkeypatch.setattr(_run.requests.Session, 'post', lambda self, *a, **kw: post(*a, **kw))
    _run.run_agent('http://test', {}, .001, stop_event=stop)
    assert len(executions) == 1
    assert len([p for p in sent if p['results']]) == 2
    assert all(p['agent']['agent_id'] == 'test-agent' for p in sent)


@pytest.mark.parametrize('failure', [requests.ReadTimeout(), 503, 401, 403])
def test_retry_wait_is_interruptible(monkeypatch, failure):
    stop = threading.Event()
    states = []
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'retry_delay', lambda *args: 60)
    class Response:
        status_code = failure
        def json(self):
            return {'ok': False, 'error': {'code': 'UNAUTHORIZED', 'message': 'Denied'}}
    def post(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return Response()
    def status(value):
        states.append(value['state'])
        stop.set()
    monkeypatch.setattr(_run.requests.Session, 'post', lambda self, *a, **kw: post(*a, **kw))
    thread = threading.Thread(target=_run.run_agent, args=('http://test', {}),
                              kwargs={'stop_event': stop, 'on_status': status})
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert states == [('unreachable' if isinstance(failure, Exception) else
                       'auth' if failure in (401, 403) else 'http')]


@pytest.mark.parametrize('kind', ['project_run_command', 'desktop_run_command', 'egress_http_stream'])
def test_streamed_work_retains_admission_until_terminal(monkeypatch, kind):
    stop, started, release, finished = (threading.Event() for _ in range(4))
    calls = []
    terminal = []
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})

    def stream(cmd_id, params, permissions, results, streams, lock, **options):
        calls.append(cmd_id)
        started.set()
        def finish():
            assert release.wait(2)
            results.append({'id': cmd_id, 'result': {}, 'error': None})
            finished.set()
        if kind in ('project_run_command', 'desktop_run_command'):
            assert options.get('cmd_type', 'project_run_command') == kind
            threading.Thread(target=finish).start()
        else:
            finish()  # Egress implementation is synchronous.

    monkeypatch.setattr(_run, '_start_project_run_streamed' if kind in ('project_run_command', 'desktop_run_command')
                        else '_start_egress_stream_streamed', stream)
    count = 0
    class Response:
        status_code = 200
        def json(self):
            return {'commands': [{'id': 'stream', 'type': kind, 'params': {}}]}
    def post(*args, **kwargs):
        nonlocal count
        count += 1
        if count > 1:
            assert started.wait(1)
        if count == 3:
            release.set()
        terminal.extend(kwargs['json']['results'])
        if terminal or count > 30:
            stop.set()
        return Response()
    monkeypatch.setattr(_run.requests.Session, 'post', lambda self, *a, **kw: post(*a, **kw))
    _run.run_agent('http://test', {}, .001, stop_event=stop)
    assert finished.wait(1)
    assert calls == ['stream']
    assert terminal


def test_retry_jitter_has_a_hard_ceiling():
    for failures in (1, 5, 10000):
        delay = retry_delay(1, failures)
        assert 0 < delay <= 60


def test_retired_worker_environment_cannot_reject_device_commands(monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_EXECUTION_WORKERS', '1')
    state = RuntimeState(threading.Event())
    for index in range(32):
        assert state.admit(str(index))
        state.finish(str(index))
    state.close()


def test_poll_never_forwards_credential_on_redirect(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'test-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})

    def post(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        assert 'proxies' not in kwargs  # Honor deployment proxy and CA settings.
        assert callable(kwargs['auth'])  # Suppress ambient netrc identity.
        stop.set()
        return type('Response', (), {'status_code': 302})()

    monkeypatch.setattr(_run.requests.Session, 'post', lambda self, *a, **kw: post(*a, **kw))
    _run.run_agent('https://test', {}, .001, stop_event=stop, bridge_secret='test-only')


@pytest.mark.parametrize("negotiated",[False,True])
def test_poll_dispatches_burst_without_worker_quota_and_replayed_execution(monkeypatch,negotiated):
    import time
    stop, release, all_started = threading.Event(), threading.Event(), threading.Event()
    commands = [{'id': f'burst-{index}', 'type': 'fixture', 'params': {}} for index in range(32)]
    if negotiated:
        for row in commands:row.update(target_agent_id='fixture-agent',expires_at=900,deadline_enforced=False)
    entered, delivered = [], []
    lock = threading.Lock()
    def execute(kind, params, permissions):
        with lock:
            entered.append(kind)
            if len(entered) == len(commands):
                all_started.set()
        assert release.wait(5)
        return {'done': True}
    polls = [0]
    class Response:
        status_code = 200
        def json(self):
            return {'commands': commands, **({'protocol_version':3,'server_time':1000} if negotiated else {})}
    def post(url, **kwargs):
        polls[0] += 1
        if polls[0] > 1:
            assert all_started.wait(3)
            release.set()
        delivered.extend(kwargs['json']['results'])
        if len(delivered) == len(commands):
            stop.set()
        assert polls[0] < 500
        return Response()
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'fixture-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'dispatch_command', execute)
    monkeypatch.setattr(_run.requests.Session, 'post', lambda self, *a, **kw: post(*a, **kw))
    try:
        _run.run_agent('http://fixture', {}, .001, stop_event=stop)
        assert len(entered) == 32
        assert {row['id'] for row in delivered} == {row['id'] for row in commands}
        assert all(row['error'] is None for row in delivered)
    finally:
        stop.set()
        release.set()


def test_spool_write_failure_does_not_publish_or_leave_partial_frame(monkeypatch):
    from pathlib import Path
    box = Outbox(threading.Condition(threading.RLock()), threading.Event(), 1, 100)
    original = Path.write_bytes
    def fail(path, data):
        original(path, data[:1])
        raise OSError('injected volume failure')
    try:
        with monkeypatch.context() as mutation:
            mutation.setattr(Path, 'write_bytes', fail)
            with pytest.raises(OSError, match='volume failure'):
                box.append({'n': 1})
        assert not box._records and box.bytes == 0
        assert list(Path(box._temporary.name).iterdir()) == []
        box.append({'n': 2})
        assert list(box) == [{'n': 2}]
    finally:
        box.close()


def test_hot_outbox_memory_spills_without_reordering_or_admission(monkeypatch):
    from pathlib import Path
    box = Outbox(threading.Condition(threading.RLock()), threading.Event(), 8, 100, memory_bytes=8)
    try:
        box.append({'n': 1})
        assert box._temporary is None
        box.append({'n': 2})
        assert list(box) == [{'n': 1}, {'n': 2}]
        assert isinstance(box._records[0][0], bytes)
        assert isinstance(box._records[1][0], Path)
        del box[:1]
        box.append({'n': 3})
        assert list(box) == [{'n': 2}, {'n': 3}]
        assert box._resident_bytes <= box._memory_budget
    finally:
        box.close()
    assert box._resident_bytes == 0


def test_outbox_memory_reuse_scales_with_launch_capacity_and_falls_back_lean(monkeypatch):
    from types import SimpleNamespace
    import runtime_guards
    from lib.desktop_agent._runtime import outbox_memory_budget
    snapshot = SimpleNamespace(effective_memory_capacity_mb=8192, effective_memory_available_mb=4096)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda: snapshot)
    assert outbox_memory_budget() == 32 * 1024 * 1024
    snapshot.effective_memory_capacity_mb = snapshot.effective_memory_available_mb = 131072
    assert outbox_memory_budget() == 1024 * 1024 * 1024
    snapshot.effective_memory_available_mb = 0
    assert outbox_memory_budget() == 1024 * 1024
    snapshot.effective_memory_available_mb = None
    assert outbox_memory_budget() == 1024 * 1024


def test_device_thread_start_failure_settles_once_and_does_not_block_other_work(monkeypatch):
    stop = threading.Event()
    started, delivered = [], []
    original = threading.Thread.start
    failed = [False]
    def start(thread):
        if thread.name == 'desktop-command' and not failed[0]:
            failed[0] = True
            raise RuntimeError('injected device worker failure')
        return original(thread)
    commands = [{'id': name, 'type': 'fixture', 'params': {'job': name}} for name in ('failed', 'healthy')]
    class Response:
        status_code = 200
        def json(self):
            return {'commands': commands}
    polls = [0]
    def post(url, **kwargs):
        polls[0] += 1
        delivered.extend(kwargs['json']['results'])
        if len(delivered) == 2:
            stop.set()
        assert polls[0] < 500
        return Response()
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'fixture-agent')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'dispatch_command', lambda kind, params, permissions: (started.append(params['job']) or {}))
    monkeypatch.setattr(_run.requests.Session, 'post', lambda self, *a, **kw: post(*a, **kw))
    _run.run_agent('http://fixture', {}, .001, stop_event=stop)
    assert started == ['healthy']
    outcomes = {row['id']: row for row in delivered}
    assert 'device worker failure' in outcomes['failed']['error']
    assert outcomes['healthy']['error'] is None


def test_outbox_budget_matches_ascii_wire_encoding_and_rejects_invalid_numbers():
    box = Outbox(threading.Condition(threading.RLock()), threading.Event(), 1, 100)
    try:
        with pytest.raises(ValueError, match='poll byte budget'):
            box.append({'data': '😀' * 8})
        with pytest.raises(ValueError):
            box.append({'data': float('nan')})
        assert not box._records and box.bytes == 0
        box.append({'data': '你好'})
        assert list(box) == [{'data': '你好'}]
    finally:
        box.close()
