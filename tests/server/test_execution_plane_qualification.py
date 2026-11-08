"""Equal-work public Goal qualification across all-in-one and resident execution.

Real HTTP, SQL authority, model transport, file tools, registered Flow children
and process owners. Only the loopback provider responses and crash timing are
controlled. All processes and storage belong to the disposable test project.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest

from serverctl_pkg.execution_service import ExecutionService
from serverctl_pkg.native_storage_service import NativeStorageService
from serverctl_pkg.resident_service import ServiceOwnerDraining
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS
from tests.tasks_pkg.test_native_production_prices import install_prices
from tests.tasks_pkg.test_native_production_recovery import wait_turn

ROOT = Path(__file__).resolve().parents[2]
WORK_ITEMS = 8  # The complete fixture workload, never a production concurrency setting.
HTTP_READY_SECONDS = 180  # serverctl start/restart --wait default for the same boot.
# Two complete topologies; every wait inside is bounded, so this is only the hang guard.
pytestmark = [pytest.mark.api, pytest.mark.slow, pytest.mark.timeout(1800)]


def eventually(read, predicate, *, seconds=90):
    expires = time.monotonic() + seconds
    while True:
        value = read()
        if predicate(value):
            return value
        assert time.monotonic() < expires, value
        time.sleep(.05)


@contextmanager
def web(directory, environment, *, generation):
    role = environment['TOFU_PROCESS_ROLE']
    ready = directory / (role + '-ready')
    ready.unlink(missing_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = directory / f'http-{generation}.log'
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-m', 'tests.support.execution_application',
            '--http', str(port)], cwd=ROOT,
            env={**environment, 'PORT': str(port), 'TOFU_PORT': str(port)},
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        def started():
            assert process.poll() is None, log.read_text()[-4000:]
            return ready.is_file()
        eventually(started, bool, seconds=HTTP_READY_SECONDS)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=10) as client:
            def probe():
                assert process.poll() is None, log.read_text()[-4000:]
                try:
                    return client.get('/api/health').json()
                except httpx.TransportError:
                    return {}  # A loaded host may delay the first post-ready response.
            health = eventually(probe, lambda value: bool(value))
            assert health['boot']['status'] == 'ready', health
            yield process, client, health
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                assert process.wait(timeout=45) == 0, log.read_text()[-4000:]
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)


class Provider(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(('127.0.0.1', 0), ProviderHandler)
        self.counts = {}
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.crash_release = threading.Event()
        self.crash_entered = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.crash_release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            marker = re.findall(r'QUALIFICATION-ITEM-(\d+)', json.dumps(body['messages']))[-1]
            item = int(marker)
            with self.server.lock:
                sequence = self.server.counts.get(item, 0) + 1
                self.server.counts[item] = sequence
            if item == 99:
                self.server.crash_entered.set()
                assert self.server.crash_release.wait(180)
            if item < WORK_ITEMS and sequence == 1:
                delta = {'tool_calls': [{'index': 0, 'id': 'publish-' + marker, 'type': 'function',
                    'function': {'name': 'write_file', 'arguments': json.dumps(dict(
                        path=f'item-{marker}.txt', append=True, content='once\n',
                        description='Publish qualification ledger'))}}]}
            else:
                if item < WORK_ITEMS and sequence == 2:
                    assert self.server.release.wait(180)
                delta = {'content': f'QUALIFICATION-ITEM-{marker} verified. [VERDICT: STOP]'}
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta,
                'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
                + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The deliberately killed execution process owns this connection.
        except BaseException as error:
            self.server.errors.append(repr(error))


def admit(client, project, item, *, goal=True):
    conversation = 'execution-qualification-' + uuid.uuid4().hex
    objective = f'QUALIFICATION-ITEM-{item}: append once to item-{item}.txt and verify it.'
    config = dict(modelRef={'creator_id': 'creator', 'model_id': 'alpha'},
        routing={'preferred_provider_id': 'provider-a'}, maxTokens=1024,
        thinkingEnabled=False, memoryEnabled=False, projectEnabled=True,
        projectPath=str(project), autoApply=True, searchMode='off', preferencesEnabled=False,
        tools={'programmaticCalling': 'off'},
        executionLimits={'max_tokens': 100000, 'max_tool_calls': 8, 'deadline': time.time() + 240})
    if goal:
        config['flowDefinition'] = dict(schema='tofu.orchestration/v1', name='Qualification', nodes=[
            dict(id='start', type='control', kind='start'),
            dict(id='writer', type='role', role='worker', params=dict(
                objective=objective, model_override='alpha', tools=['write_file'])),
            dict(id='review', type='role', role='virtual_user', params=dict(objective=objective)),
            dict(id='stop', type='control', kind='stop')],
            edges=[{'from': a, 'to': b} for a, b in [('start', 'writer'), ('writer', 'review'), ('review', 'stop')]])
    response = client.post(f'/api/v4/conversations/{conversation}/turns', json={
        'commandId': uuid.uuid4().hex,
        'conversation': {'allowCreate': True, 'title': objective, 'settings': {'projectPath': str(project)}},
        'inputTurn': {'content': objective}, 'config': config})
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    return f'/api/v4/conversations/{conversation}/turns/' + turn['turnId']


def storage_client(service):
    from lib.storage.client import StorageClient
    from lib.storage.connection_file import read_connection_file
    connection = read_connection_file(service.connection_path)
    assert connection['backend'] == 'sqlite'
    return StorageClient(connection['host'], connection['port'], connection['token'])


def goal_fees(service, turn):
    """Paid rounds from the Goal run's durable effect receipts, in journal order."""
    client = storage_client(service)
    run_id = turn['projection']['orchestration']['goalRunId']
    cursor, rows = 0, []
    while True:
        page = client.query('orchestration.event.page', {
            'user_id': 1, 'tenant_id': '', 'run_id': run_id, 'cursor': cursor})
        assert not page['cursor_reset'], page
        rows.extend(row for event in page['events'] if event['type'] == 'execution_effect_completed'
                    for row in event['result']['output_projection']['apiRounds'])
        if page['caught_up']:
            return rows
        cursor = page['next_cursor']


def run_workload(directory, *, split):
    directory.mkdir()
    project = directory / 'project'
    project.mkdir()
    (directory / 'contracts').mkdir()
    for name in ('native_storage_connection_v2.json', 'worker_job_lifecycle_v1.json'):
        (directory / 'contracts' / name).symlink_to(ROOT / 'contracts' / name)
    # Conftest already scrubbed the launching deployment's identity and pinned
    # hermetic inputs (no shared Codex login, catalog feed or replica cache);
    # the startup catalogue this test extends must come from that baseline.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('TOFUDB_') and key not in {
                       'LD_LIBRARY_PATH', 'LLM_API_KEY', 'LLM_API_KEYS',
                       'TOFU_STORAGE_TEST_BACKEND', 'TOFU_STORAGE_CONNECTION_FILE',
                       'TOFU_STORAGE_TOKEN', 'TOFU_STORAGE_PARENT_PID'}}
    environment.update(PYTHONPATH=str(ROOT), TOFU_PROJECT_PATH=str(directory),
        TOFU_DATA_DIR=str(directory), TOFU_STORAGE_PROJECT_ROOT=str(directory),
        TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE='1', TOFU_AUTH_MODE='open',
        TOFU_DEPLOYMENT_MODE='personal', TOFU_PERSONAL_STORAGE_BACKEND='sqlite',
        TOFU_PROCESS_ROLE='api' if split else 'all',
        TOFU_MCP_CACHE_DIR=str(directory / 'mcp-cache'),
        UV_CACHE_DIR=str(directory / 'uv-cache'), UV_OFFLINE='1',
        npm_config_cache=str(directory / 'npm-cache'), npm_config_offline='true')
    storage = NativeStorageService(str(directory), sys.executable)
    execution = ExecutionService(str(directory), sys.executable)
    provider = Provider()
    if not split:
        provider.release.set()
    measurements = {'split': split}
    try:
        storage_plan = storage.prepare(environment)
        environment['TOFU_STORAGE_CONNECTION_FILE'] = storage.ensure(storage_plan, environment)
        if split:
            execution_env = dict(environment, TOFU_PROJECT_PATH=str(ROOT))
            execution_plan = execution.prepare(execution_env)
            execution.ensure(execution_plan, execution_env)
            record = eventually(execution.record, lambda row: row.get('state') in {'ready', 'failed'},
                                seconds=NATIVE_SERVICE_PREPARATION_SECONDS)
            assert record['state'] == 'ready', record
        started = time.monotonic()
        with web(directory, environment, generation=1) as (process, client, first_health):
            install_prices(client, f'http://127.0.0.1:{provider.server_port}/v1')
            with ThreadPoolExecutor(max_workers=WORK_ITEMS) as pool:
                paths = list(pool.map(lambda item: admit(client, project, item), range(WORK_ITEMS)))
            if split:
                eventually(lambda: dict(provider.counts), lambda counts:
                    all(counts.get(item, 0) == 2 for item in range(WORK_ITEMS)))
                storage_pid = storage.record()['pid']
                execution_pid = execution.record()['pid']
                latencies = []
                for _ in range(12):
                    before = time.monotonic()
                    assert client.get('/api/health').status_code == 200
                    latencies.append(time.monotonic() - before)
                measurements['blockedWorkApiLatencySeconds'] = sorted(latencies)
                process.kill()
                assert process.wait(timeout=10) == -signal.SIGKILL
            else:
                turns = [wait_turn(client, path, {'completed', 'failed', 'interrupted'}) for path in paths]
        if split:
            with web(directory, environment, generation=2) as (_, client, second_health):
                assert second_health['bootId'] != first_health['bootId']
                assert storage.record()['pid'] == storage_pid
                assert execution.record()['pid'] == execution_pid
                assert provider.counts == {item: 2 for item in range(WORK_ITEMS)}
                assert all(client.get(path).json()['turn']['status'] == 'running' for path in paths)
                provider.release.set()
                turns = [wait_turn(client, path, {'completed', 'failed', 'interrupted'}) for path in paths]
                lost = admit(client, project, 99, goal=False)
                assert provider.crash_entered.wait(45)
                os.kill(execution_pid, signal.SIGKILL)

                def released():
                    # Recover like the manager: only after the killed host's lease is free.
                    try:
                        execution.stop()
                        return True
                    except ServiceOwnerDraining:
                        return False
                eventually(released, bool, seconds=NATIVE_SERVICE_STOP_SECONDS)
                execution.ensure(execution_plan, execution_env)
                record = eventually(execution.record, lambda row: row.get('state') in {'ready', 'failed'},
                                    seconds=NATIVE_SERVICE_PREPARATION_SECONDS)
                assert record['state'] == 'ready', record
                assert record['pid'] != execution_pid
                interrupted = wait_turn(client, lost, {'interrupted', 'failed'})
                assert interrupted['status'] == 'interrupted', interrupted
                assert interrupted['settlement']['error']['code'] == 'worker_effect_outcome_unknown'
                assert provider.counts[99] == 1
                provider.crash_release.set()
                storage.stop()
                storage.ensure(storage_plan, environment)

                def reattached():
                    # The API's storage runtime re-attaches after the resident
                    # host restarts; only its typed retryable refusal or a request
                    # held at the readiness gate may intervene.
                    try:
                        response = client.get(paths[0])
                    except httpx.TimeoutException:
                        return None
                    if response.status_code == 503:
                        error = response.json()['error']
                        assert (error['kind'], error['retryable']) == ('storage_unavailable', True), error
                        return None
                    assert response.status_code == 200, response.text
                    return response.json()['turn']
                after_storage_restart = eventually(reattached, lambda turn: turn is not None,
                                                   seconds=NATIVE_SERVICE_PREPARATION_SECONDS)
                assert after_storage_restart == turns[0]
                fresh = wait_turn(client, admit(client, project, 100, goal=False), {'completed', 'failed'})
                assert fresh['status'] == 'completed', fresh
                assert provider.counts[100] == 1
        assert not provider.errors, provider.errors
        assert all(turn['status'] == 'completed' for turn in turns), turns
        assert provider.counts == {**{item: 3 for item in range(WORK_ITEMS)},
                                   **({99: 1, 100: 1} if split else {})}
        assert [path.read_text() for path in sorted(project.glob('item-*.txt'))] == ['once\n'] * WORK_ITEMS
        fees = [row for turn in turns for row in goal_fees(storage, turn)]
        assert sum(row['usage']['total_tokens'] for row in fees) == WORK_ITEMS * 3 * 5
        active = lambda: storage_client(storage).query('worker_job.active.list', {
            'user_id': 1, 'task_kinds': ['conversation-attempt']})
        quiescent = eventually(active, lambda rows: rows == [])
        jobs = [storage_client(storage).query('worker_job.get', {
            'task_id': 'conversation-attempt:' + turn['currentAttemptId'], 'user_id': 1}) for turn in turns]
        measurements.update(elapsedSeconds=time.monotonic() - started,
            completed=sum(turn['status'] == 'completed' for turn in turns),
            modelCalls=sum(provider.counts[item] for item in range(WORK_ITEMS)),
            fileWrites=sum(path.read_text().count('once\n') for path in project.glob('item-*.txt')),
            totalTokens=sum(row['usage']['total_tokens'] for row in fees),
            totalCostCny=sum(row['cost']['costCny'] for row in fees),
            workerJobBytes=sum(len(json.dumps(job, separators=(',', ':')).encode()) for job in jobs),
            activeJobs=len(quiescent))
        return measurements
    finally:
        provider.close()
        try:
            execution.stop()
        finally:
            storage.stop()
            (directory / 'measurement.json').write_text(json.dumps(measurements, indent=2))


def test_equal_work_survives_api_execution_and_storage_process_loss(tmp_path):
    baseline = run_workload(tmp_path / 'all', split=False)
    split = run_workload(tmp_path / 'split', split=True)
    for key in ('completed', 'modelCalls', 'fileWrites', 'totalTokens', 'totalCostCny', 'activeJobs'):
        assert split[key] == baseline[key], (key, baseline, split)
    assert baseline['totalCostCny'] > 0
