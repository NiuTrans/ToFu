"""Native attachment identity, cancellation and transport ownership."""
import hashlib
import os
import threading

import pytest

from lib.storage.connection_file import read_connection_file, write_connection_document
from lib.storage.supervisor import StorageSupervisor
from lib.storage.errors import StorageError
from server_manager import proc_start_ticks

pytestmark = pytest.mark.unit


@pytest.fixture
def document(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFUDB_OWNER_ID', '7')
    monkeypatch.setenv('TOFUDB_TENANT_ID', '3')
    return {
        'format': 'tofu.storage-connection/v2', 'protocol': 2, 'backend': 'tofudb',
        'host': '127.0.0.1', 'port': 12345, 'token': 'a' * 48,
        'ownerId': 7, 'tenantId': 3, 'projectPath': str(tmp_path),
        'dataDirectory': str(tmp_path / 'data'),
        'pid': os.getpid(), 'processStartTime': proc_start_ticks(os.getpid()),
        'binarySha256': 'b' * 64, 'serviceId': 'c' * 32,
        'searchProjectionDirectory': str(tmp_path / 'search'), 'nativeFastpath': None,
    }


def test_native_document_roundtrip_is_private(document, tmp_path):
    path = tmp_path / 'connection.json'
    write_connection_document(path, document)
    assert read_connection_file(path) == document
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('field,value', [
    ('protocol', True), ('ownerId', True), ('tenantId', 0), ('pid', -1),
    ('processStartTime', 0), ('projectPath', 'relative'), ('serviceId', 'bad'),
    ('binarySha256', 'bad'), ('backend', 'sqlite'), ('token', '密' * 48),
])
def test_native_document_rejects_invalid_identity(document, tmp_path, field, value):
    with pytest.raises(RuntimeError):
        write_connection_document(tmp_path / 'connection.json', {**document, field: value})


@pytest.mark.parametrize('field,value,reason', [
    ('ownerId', 8, 'scope'), ('tenantId', 4, 'scope'),
    ('projectPath', '/', 'project'), ('dataDirectory', '/', 'data directory'),
    ('binarySha256', 'd' * 64, 'upgrade'),
    ('processStartTime', 1, 'exited'),
])
def test_native_attach_fences_identity_before_rpc(document, tmp_path, monkeypatch, field, value, reason):
    supervisor = StorageSupervisor(project_root=tmp_path, backend='tofudb')
    supervisor._admitted_native_artifact = ('test-artifact', 'b' * 64)
    monkeypatch.setattr(supervisor, '_resolve_native_daemon_binary', lambda: 'test-artifact')
    error = RuntimeError if field == 'processStartTime' else StorageError
    with pytest.raises(error, match=reason):
        supervisor._external_client({**document, field: value})


def test_known_identity_conflict_fails_without_waiting_for_startup_budget(document, tmp_path, monkeypatch):
    path = tmp_path / 'connection.json'
    write_connection_document(path, {**document, 'ownerId': 8})
    supervisor = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=path)
    monkeypatch.setattr(supervisor._external_monitor_stop, 'wait',
                        lambda *_: pytest.fail('identity conflict must not consume startup retries'))
    with pytest.raises(StorageError, match='scope mismatch') as raised:
        supervisor.start()
    assert raised.value.retryable is False
    assert supervisor.status()['state'] == 'stopped'


@pytest.fixture
def admitted_external(document, tmp_path, monkeypatch):
    from lib.storage import supervisor as supervisor_module

    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'admitted-engine')
    engine.chmod(0o755)
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    pin = tmp_path / 'tofu-db.sha256'
    pin.write_text(digest)
    monkeypatch.setenv('TOFUDB_BIN', str(engine))
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    monkeypatch.setattr(supervisor_module, '_advise_tofudb_engine_staleness', lambda _: None)
    clients = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.closed = False
            clients.append(self)

        def health(self, **kwargs):
            return {'ready': True, 'backend': 'tofudb'}

        def close(self):
            self.closed = True

    monkeypatch.setattr('lib.storage.v2_client.StorageV2Client', Client)
    document = {**document, 'binarySha256': digest}
    connection = tmp_path / 'connection.json'
    write_connection_document(connection, document)
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='tofudb', connection_file=connection)
    supervisor._liveness_interval_seconds = 60.0
    try:
        supervisor.start()
        yield supervisor, engine, pin, document, connection, clients
    finally:
        supervisor.stop()


@pytest.mark.parametrize('deployment', ['replace', 'repin', 'remove'])
def test_native_reattach_preserves_admitted_process_after_disk_change(admitted_external, deployment):
    supervisor, engine, pin, _document, _connection, clients = admitted_external
    supervisor.stop()
    assert clients[0].closed
    if deployment == 'remove':
        engine.unlink()
        pin.unlink()
    else:
        engine.write_bytes(b'prepared-upgrade')
        if deployment == 'repin':
            pin.write_text(hashlib.sha256(engine.read_bytes()).hexdigest())

    supervisor.start()
    assert supervisor.ready
    assert len(clients) == 2 and not clients[-1].closed


def test_native_reattach_refuses_new_engine_even_when_disk_matches(admitted_external):
    supervisor, engine, pin, document, connection, clients = admitted_external
    supervisor.stop()
    engine.write_bytes(b'prepared-upgrade')
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    pin.write_text(digest)
    write_connection_document(connection, {**document, 'binarySha256': digest})

    with pytest.raises(StorageError, match='upgrade requires a drained replacement') as raised:
        supervisor.start()
    assert raised.value.retryable is False
    assert not supervisor.ready and len(clients) == 1


def test_first_native_attachment_still_verifies_disk_pin(admitted_external, tmp_path):
    _supervisor, engine, _pin, _document, connection, clients = admitted_external
    engine.write_bytes(b'unverified-replacement')
    fresh = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=connection)
    try:
        with pytest.raises(StorageError, match='SHA-256 mismatch') as raised:
            fresh.start()
        assert raised.value.retryable is False
        assert not fresh.ready and len(clients) == 1
    finally:
        fresh.stop()


def test_new_application_requires_service_to_match_newly_admitted_engine(admitted_external, tmp_path):
    supervisor, engine, pin, _document, connection, clients = admitted_external
    supervisor.stop()
    engine.write_bytes(b'prepared-upgrade')
    pin.write_text(hashlib.sha256(engine.read_bytes()).hexdigest())
    fresh = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=connection)
    try:
        with pytest.raises(StorageError, match='upgrade requires a drained replacement') as raised:
            fresh.start()
        assert raised.value.retryable is False
        assert not fresh.ready and len(clients) == 1
    finally:
        fresh.stop()


def test_stop_wins_over_late_external_handshake(document, tmp_path, monkeypatch):
    path = tmp_path / 'connection.json'
    write_connection_document(path, document)
    supervisor = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=path)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Client:
        def health(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return {'ready': True, 'backend': 'tofudb'}

        def close(self):
            closed.set()

    monkeypatch.setattr(supervisor, '_external_client', lambda _: Client())
    errors = []

    def attach():
        try:
            supervisor.start()
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=attach)
    thread.start()
    try:
        assert entered.wait(5)
        supervisor.stop()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and closed.is_set()
    assert errors == ['external storage attachment was cancelled']
    assert not supervisor.ready


def test_failed_health_closes_every_rejected_client(document, tmp_path, monkeypatch):
    path = tmp_path / 'connection.json'
    write_connection_document(path, document)
    supervisor = StorageSupervisor(project_root=tmp_path, backend='tofudb', connection_file=path)
    supervisor._startup_timeout = 0.12
    clients = []

    class Client:
        closed = False

        def health(self, **kwargs):
            return {'ready': False, 'backend': 'tofudb'}

        def close(self):
            self.closed = True

    def client(_):
        result = Client()
        clients.append(result)
        return result

    monkeypatch.setattr(supervisor, '_external_client', client)
    with pytest.raises(RuntimeError, match='timed out'):
        supervisor.start()
    assert clients and all(client.closed for client in clients)
