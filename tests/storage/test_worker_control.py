"""Cross-process wakeups are tied to committed jobs, never caller-local hints."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest

from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_watch_close_interrupts_only_its_owned_transport(semantic_authority):
    from lib.durable_worker.runtime_policy import runtime_policy
    from lib.storage.worker_wake import StorageWorkerWakeSource
    factory, _owner, _engine = semantic_authority
    waiting = threading.Event()
    class Client:
        def __init__(self, client):
            self.client = client
        def worker_watch(self, *, cursor, **kwargs):
            if cursor is not None:
                waiting.set()
            return self.client.worker_watch(cursor=cursor, **kwargs)
    source = StorageWorkerWakeSource(lambda: Client(factory()), runtime_policy())
    source.start(lambda operation: None)
    try:
        assert waiting.wait(5)
        with ThreadPoolExecutor() as executor:
            executor.submit(source.close).result(5)
        # Closing a subscription cannot close the shared client's other RPCs.
        assert factory().query('worker_job.claim_horizon', {
            'task_kinds': ['unrelated-control-test'], 'now_ms': int(time.time() * 1000),
        })['claimableInMs'] is None
    finally:
        source.close()


def test_committed_job_wakes_independent_client_and_gap_is_not_lost(semantic_authority):
    factory, owner, _engine = semantic_authority
    client = factory()
    before = client.worker_watch(cursor=None, deadline_unix_ms=int((time.time() + 5) * 1000))
    # The producer is a storage RPC; no application commit bus is subscribed.
    task = 'wake-' + uuid.uuid4().hex
    factory(write=True).command('worker_job.enqueue', {
        'task_id': task, 'user_id': owner, 'tenant_id': '', 'task_kind': 'wake-test',
        'payload': {}, 'idempotency_key': task, 'priority': 0,
        'available_at_ms': 0, 'now_ms': int(time.time() * 1000),
    }, command_id=uuid.uuid4().hex)
    after = client.worker_watch(cursor=before['cursor'], deadline_unix_ms=int((time.time() + 5) * 1000))
    assert after['cursor']['queue'] > before['cursor']['queue']
    assert after['cancelTasks'] == []
    factory(write=True).command('worker_job.request_cancel', {
        'task_id': task, 'user_id': owner, 'reason': 'Stop',
        'now_ms': int(time.time() * 1000),
    }, command_id=uuid.uuid4().hex)
    cancelled = client.worker_watch(cursor=after['cursor'], deadline_unix_ms=int((time.time() + 5) * 1000))
    assert cancelled['cursor']['cancel'] > after['cursor']['cancel']
    assert cancelled['cancelTasks'] is None  # Cancellation occurred between waits.


@pytest.mark.unit
def test_wait_holds_no_history_and_close_joins_waiters():
    from lib.storage_authority_sql.worker_control import WorkerControl
    control = WorkerControl()
    first = control.watch({'cursor': None}, time.monotonic() + 5)
    entered = threading.Event()

    def waiting():
        entered.set()
        return control.watch({'cursor': first['cursor']}, time.monotonic() + 5)

    with ThreadPoolExecutor() as pool:
        result = pool.submit(waiting)
        assert entered.wait(5)
        control.publish(False, {'one'})
        value = result.result(5)
        assert value['cancelTasks'] in (None, ['one'])
        pending = pool.submit(control.watch, {'cursor': value['cursor']}, time.monotonic() + 5)
        control.close()
        with pytest.raises(StorageError, match='closed'):
            pending.result(5)
    assert control._waiters == {}


@pytest.mark.unit
def test_worker_control_generation_is_current():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, 'scripts/gen_worker_control.py', '--check'], cwd=root, check=True)


@pytest.mark.unit
def test_authority_restart_refreshes_claims_even_without_cancel_history():
    from lib.storage.worker_wake import StorageWorkerWakeSource

    source = StorageWorkerWakeSource(None, {})
    active = threading.Event()
    unsubscribe = source.signals.subscribe('active', active)
    before = {'epoch': 'old', 'queue': 0, 'cancel': 0}
    after = {'epoch': 'new', 'queue': 0, 'cancel': 0}
    hints = []

    def watch(cursor):
        assert cursor == before
        source._stop.set()
        return {'cursor': after, 'cancelTasks': []}

    source._watch = watch
    source._run(before, hints.append)
    assert active.is_set()
    assert hints == ['authority-commit']
    unsubscribe()
    assert source.signals._active == {}
