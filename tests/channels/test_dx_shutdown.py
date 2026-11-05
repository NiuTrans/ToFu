"""大象 shutdown joins its producers before releasing storage (C6)."""
import queue
import threading
from types import SimpleNamespace

import pytest

from lib.channels.dx.adapter import DxAdapter
from lib.channels.model import ChannelError
from lib.storage.errors import StorageError
from tests.channels.conftest import OWNER

pytestmark = pytest.mark.unit


class Bridge:
    def __init__(self):
        self.events = queue.Queue()
        self.returncode = None

    def alive(self):
        return self.returncode is None

    def terminate(self):
        self.returncode = -15

    def close(self, timeout):
        self.returncode = 0
        return True


MESSAGE = {'type': 'message', 'source': 'im', 'message': {
    'mid': '7400000000000000001', 'from': '2002', 'gid': '500',
    'message': {'text': 'pending message'}}}


def test_storage_failure_diagnostics_cannot_skip_bridge_termination(store, monkeypatch):
    bridge = Bridge()
    adapter = DxAdapter(OWNER, store=store)
    adapter._bridge = bridge
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise StorageError('database_unavailable', 'Storage sidecar is unavailable')

    monkeypatch.setattr(store, 'account', unavailable)
    monkeypatch.setattr(store, 'set_status', unavailable)
    bridge.events.put(MESSAGE)
    bridge.events.put(None)
    adapter._process()
    assert len(calls) == 2
    assert bridge.returncode == -15
    assert adapter.outcome() == ('error', 'ingest failed: database_unavailable')


@pytest.mark.parametrize('item', [MESSAGE, {'type': 'status', 'status': 'connected'}])
def test_stopping_ingress_does_not_consume_queued_events(store, monkeypatch, item):
    bridge = Bridge()
    adapter = DxAdapter(OWNER, store=store)
    adapter._bridge = bridge
    calls = []
    monkeypatch.setattr('lib.channels.ingress.ingest', lambda *a, **k: calls.append('ingest'))
    monkeypatch.setattr(adapter, '_on_status', lambda item: calls.append('status'))
    bridge.events.put(item)
    bridge.events.put(None)
    adapter._stopping.set()
    adapter._process()
    assert calls == []


def test_stop_wakes_idle_ingress_without_waiting_for_bridge_eof(store):
    bridge = Bridge()
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter.start()
    try:
        assert adapter.stop(1)
    finally:
        bridge.events.put(None)
        adapter.stop(1)


def test_inflight_ingest_keeps_stop_incomplete_and_checkpoint_waits_for_join(store, monkeypatch):
    bridge = Bridge()
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def ingest(*args, **kwargs):
        calls.append('ingest')
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr('lib.channels.ingress.ingest', ingest)
    monkeypatch.setattr(adapter, 'checkpoint', lambda: calls.append('checkpoint'))
    adapter.start()
    bridge.events.put(MESSAGE)
    try:
        assert entered.wait(5)
        bridge.events.put(MESSAGE)
        assert not adapter.stop(0)
        assert calls == ['ingest']
    finally:
        release.set()
        bridge.events.put(None)
        assert adapter.stop(1)
    assert calls == ['ingest', 'checkpoint']


class Session:
    def __init__(self, owner, *, store, login_mis):
        self.login_mis = login_mis
        self.running = False
        self.joined = True
        self.stop_error = False
        self.stops = 0

    def start(self):
        self.running = True

    def alive(self):
        return self.running

    def tick(self):
        pass

    def stop(self, timeout):
        self.stops += 1
        self.running = False
        if self.stop_error:
            raise StorageError('database_unavailable', 'checkpoint failed')
        return self.joined

    def outcome(self):
        return 'disconnected', 'bridge exited'


@pytest.fixture
def supervisor(monkeypatch):
    from lib.channels.dx import supervisor
    monkeypatch.setattr(supervisor, '_ADAPTERS', {})
    monkeypatch.setattr(supervisor, '_CONFIRMING', set())
    monkeypatch.setattr(supervisor, '_WORKERS', set(), raising=False)
    monkeypatch.setattr(supervisor, '_STOPPING', threading.Event(), raising=False)
    monkeypatch.setattr(supervisor, '_HOSTING', threading.Event())
    monkeypatch.setattr(supervisor, 'launch_spec', lambda owner: SimpleNamespace(installed=True))
    return supervisor


@pytest.mark.parametrize('failure', [None, ChannelError('needs_login', 'approval refused'),
                                   StorageError('database_unavailable', 'storage stopped')])
def test_shutdown_joins_identity_confirmation_and_suppresses_its_late_result(
        store, supervisor, monkeypatch, failure):
    store.update_account('dx', lambda account: {**account, 'enabled': True, 'mis': 'owner'})
    entered, release = threading.Event(), threading.Event()
    started = []
    workers = []

    def resolve(*args):
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(5)
        if failure:
            raise failure
        return {'uid': '1001', 'mis': 'owner', 'name': 'Owner'}

    def factory(*args, **kwargs):
        started.append(args)
        return Session(*args, **kwargs)

    assert supervisor.reconcile_owner(
        OWNER, store=store, adapter_factory=factory, identity_resolver=resolve,
        session_factory=lambda *args: SimpleNamespace(mis='owner'),
        approval_check=lambda *args: False) == 'confirming'
    try:
        assert entered.wait(5)
        before = store.account('dx')
        assert not supervisor.stop_channel_bridges(0)
        with pytest.raises(RuntimeError, match='quiesc'):
            supervisor.start_channel_bridges()
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert supervisor.stop_channel_bridges(1)
    assert store.account('dx') == before
    assert started == []


@pytest.mark.parametrize('failure', ['timeout', 'exception'])
def test_stop_retains_unjoined_session_for_retry(store, router, enabled, supervisor, failure):
    enabled()
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=Session) == 'started'
    session = supervisor.running_adapter(OWNER)
    session.joined = False
    session.stop_error = failure == 'exception'
    assert not supervisor.stop_channel_bridges(0)
    assert not supervisor.stop_channel_bridges(0)
    assert session.stops == 2
    session.joined, session.stop_error = True, False
    assert supervisor.stop_channel_bridges(1)
    assert session.stops == 3


@pytest.mark.parametrize('change', ['disable', 'switch', 'exit'])
def test_reconcile_does_not_replace_a_session_before_join(
        store, router, enabled, supervisor, change):
    enabled()
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=Session) == 'started'
    session = supervisor.running_adapter(OWNER)
    session.joined = False
    if change == 'disable':
        store.update_account('dx', lambda account: {**account, 'enabled': False})
    elif change == 'switch':
        store.update_account('dx', lambda account: {**account, 'mis': 'alice'})
    else:
        session.running = False
    assert supervisor.reconcile_owner(
        OWNER, store=store, adapter_factory=Session,
        session_factory=lambda *args: SimpleNamespace(mis='alice'),
        approval_check=lambda *args: False,
        identity_resolver=lambda *args: {'uid': '3003', 'mis': 'alice'}) == 'stopping'
    assert not supervisor.stop_channel_bridges(0)
    session.joined = True
    assert supervisor.stop_channel_bridges(1)
    assert session.stops == 3


def test_shutdown_fences_late_reconcile_until_explicit_start(store, router, enabled, supervisor, monkeypatch):
    enabled()
    assert supervisor.stop_channel_bridges(1)
    account = store.account('dx')

    def forbidden(*args, **kwargs):
        pytest.fail('shutdown accessed storage')

    with monkeypatch.context() as stopped:
        stopped.setattr(store, 'account', forbidden)
        stopped.setattr(supervisor, 'ChannelStore', forbidden)
        assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=Session) == 'stopped'
        supervisor.reconcile_channel_bridges(SimpleNamespace(owner_user_id=OWNER))
        supervisor.notify_account_changed(OWNER)
    assert not supervisor._HOSTING.is_set()
    supervisor.start_channel_bridges()
    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=Session) == 'started'
    assert store.account('dx')['identity'] == account['identity']
    assert supervisor.stop_channel_bridges(1)


def test_stop_joins_configuration_notification_worker(store, supervisor, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers = []
    original = supervisor.reconcile_owner

    def delayed(owner):
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(5)
        return original(owner, store=store)

    monkeypatch.setattr(supervisor, 'reconcile_owner', delayed)
    supervisor._HOSTING.set()
    supervisor.notify_account_changed(OWNER)
    try:
        assert entered.wait(5)
        assert not supervisor.stop_channel_bridges(0)
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert supervisor.stop_channel_bridges(1)


@pytest.mark.parametrize('joined', [True, False])
def test_partial_start_failure_stops_and_retains_cleanup_debt(
        store, router, enabled, supervisor, joined):
    enabled()
    instances = []

    class PartialSession(Session):
        def start(self):
            self.joined = joined
            instances.append(self)
            raise RuntimeError('outbox worker failed to start')

    assert supervisor.reconcile_owner(OWNER, store=store, adapter_factory=PartialSession) == 'error'
    [session] = instances
    assert session.stops == 1
    assert store.account('dx')['status']['state'] == 'error'
    session.joined = True
    assert supervisor.stop_channel_bridges(1)
    assert session.stops == (1 if joined else 2)
