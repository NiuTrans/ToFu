"""Real selected SQL RPC -> shared Rust journal/Goal -> durable transactions."""
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import time

import pytest

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.storage_authority_sql.native_checkpoint_config import NativeCheckpointConfiguration
from lib.storage_authority_sql.server import create_server
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend, acquire, binding, call, rows, snapshot

pytestmark = pytest.mark.api
TOKEN = 'native-checkpoint-selected-sql-'*2


@pytest.fixture
def configuration():
    path = os.environ.get('TOFU_CHECKPOINT_SQL_TEST_LIBRARY')
    if not path:
        pytest.skip('explicit pinned SQL checkpoint image required')
    image = Path(path)
    return NativeCheckpointConfiguration(image, os.environ['TOFU_CHECKPOINT_SQL_TEST_SHA256'], image.stat().st_size,
                                         3, 7, 1 << 20, 1024)


class ApplicationPort:
    """Use the actual application client with the existing controller fixtures."""
    def __init__(self, client, owner=7):
        self.client, self.owner = client, owner

    def control(self, operation, payload, *, deadline_at, response_bytes):
        return self.client.native_checkpoint_control(operation, payload, user_id=self.owner,
            deadline_unix_ms=int((time.time()+max(0, deadline_at-time.monotonic()))*1000), response_bytes=response_bytes)


@pytest.fixture
def serving(backend, configuration, monkeypatch):
    @contextmanager
    def serve(config, pipeline, read_only):
        monkeypatch.setattr(backend, 'config', replace(backend.config, native_checkpoint=config))
        server = create_server(backend, TOKEN, logical_outbox=pipeline, read_only_preview=read_only)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = StorageClient('127.0.0.1', server.server_address[1], TOKEN, timeout=5)
        try:
            yield server, client, ApplicationPort(client)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(5)
            assert not thread.is_alive()
    with ExitStack() as resources:
        def create(*, config=configuration, pipeline=None, read_only=False):
            return resources.enter_context(serve(config, pipeline, read_only))
        yield create


def test_lazy_sql_negotiation_and_journal_resume_use_selected_authority(serving, backend):
    server, client, port = serving()
    assert server.native_checkpoint._controller is None
    assert client.native_checkpoint_available(user_id=7)
    cap = acquire(port)
    assert call(port, 'load', cap)['state'] is None
    assert call(port, 'begin', cap, round=0)['accepted']
    assert call(port, 'complete', cap, state=snapshot())['accepted']
    assert call(port, 'finish', cap, status='aborted', suspended=True)['accepted']
    assert call(port, 'release', cap)['accepted']
    assert call(port, 'forget', cap)['accepted']
    resumed = acquire(port, token='resumed')
    assert call(port, 'load', resumed)['state'] == snapshot()
    assert len(rows(backend)) == 2


@pytest.mark.parametrize('read_only', [False, True])
def test_disabled_or_preview_port_refuses_without_loading_code(serving, backend, read_only):
    server, client, port = serving(read_only=True) if read_only else serving(config=None)
    assert client.native_checkpoint_available(user_id=7) is False
    with pytest.raises(StorageError, match='lacks durable'):
        call(port, 'create', parameters=binding())
    assert server.native_checkpoint._controller is None and rows(backend) == []


def test_owner_and_inner_owner_refuse_before_private_mutation(serving, backend):
    server, client, port = serving()
    with pytest.raises(StorageError) as caught:
        client.native_checkpoint_available(user_id=8)
    assert caught.value.code == 'database_forbidden'
    assert server.native_checkpoint._controller is None
    response = call(port, 'create', parameters=binding(owner=8))
    assert not response['accepted'] and rows(backend) == []


def test_stale_instance_binding_never_refreshes_after_endpoint_replacement(serving, backend):
    first, client, port = serving()
    assert client.native_checkpoint_available(user_id=7)
    second, fresh, new_port = serving()
    assert first.native_checkpoint.runtime_id != second.native_checkpoint.runtime_id
    client._port = second.server_address[1]
    for _ in range(2):
        with pytest.raises(StorageError, match='stale'):
            call(port, 'create', parameters=binding())
    assert second.native_checkpoint._controller is None and rows(backend) == []
    assert fresh.native_checkpoint_available(user_id=7)
    assert acquire(new_port) > 0


def test_commit_without_ack_is_never_repeated_by_application_transport(serving, backend, monkeypatch):
    _, _, port = serving()
    cap = acquire(port)
    call(port, 'load', cap)
    assert call(port, 'begin', cap, round=0)['accepted']
    original, commits = backend.command, []
    def lose_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == 'native_checkpoint.cas':
            commits.append(result)
            raise StorageError('database_unavailable', 'injected acknowledgement loss', True, 1)
        return result
    monkeypatch.setattr(backend, 'command', lose_ack)
    with pytest.raises(StorageError, match='acknowledgement loss'):
        call(port, 'complete', cap, state=snapshot())
    assert len(commits) == 1 and len(rows(backend)) == 2
    monkeypatch.setattr(backend, 'command', original)
    assert not call(port, 'begin', cap, round=1)['accepted']


def test_server_close_joins_entered_controller_before_backend_shutdown(serving, backend, monkeypatch):
    server, _, port = serving()
    entered, release = threading.Event(), threading.Event()
    original = backend.command
    def blocked(*args, **kwargs):
        if args[0] == 'native_checkpoint.cas':
            def transaction(session):
                entered.set()
                assert release.wait(10)
                return args[4](session)
            return original(*args[:4], transaction, *args[5:], **kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(backend, 'command', blocked)
    with ThreadPoolExecutor(max_workers=2) as threads:
        active = threads.submit(acquire, port)
        assert entered.wait(5)
        stopped = threads.submit(server.server_close)
        try:
            controller = server.native_checkpoint._controller
            with controller._condition:
                assert controller._condition.wait_for(lambda: controller._state == 'closing', timeout=5)
            assert not stopped.done()
        finally:
            release.set()
        assert active.result(timeout=10) > 0
        stopped.result(timeout=10)
    assert len(rows(backend)) == 1


@pytest.mark.parametrize('cost_only', [False, True])
def test_whole_goal_claim_and_handoff_use_application_rpc(serving, backend, cost_only):
    from tests.support.native_sql_goal import GoalCase, writer, goal_call
    case = GoalCase(backend, cost_only=cost_only)
    _, client, port = serving()
    assert client.native_checkpoint_available(user_id=7)
    with writer(case) as process:
        process.kill()
        process.join(10)
        claim = case.claim(port)
        assert claim['accepted'] and case.claim(port) == claim
        group = case.prepare_target()
        resumed = goal_call(port, 'goal_handoff', group)
        assert resumed['accepted'] and len(resumed['state']['leases']) == 2
        assert goal_call(port, 'goal_handoff', group) == resumed
        for lease in resumed['state']['leases']:
            assert call(port, 'load', lease['capability_id'])['state']['task_id'] == lease['task_id']
