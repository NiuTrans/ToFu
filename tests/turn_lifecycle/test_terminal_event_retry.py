"""Lost terminal ACKs replay success from the authority's atomic receipt."""
import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage, turn_service
from tests.turn_lifecycle.test_turn_event_carried_task_event import _running_task, _stored_event

pytestmark = pytest.mark.unit


def test_terminal_transport_retry_returns_the_original_success(turn_service, storage, monkeypatch):
    assert_terminal_transport_retry(turn_service, storage, monkeypatch)


def assert_terminal_transport_retry(turn_service, storage, monkeypatch):
    _, task = _running_task(turn_service, storage, 'terminal-retry', 'terminal-retry-task')
    task.update(status='done', finishReason='stop')
    original = storage.client.command
    replies = []

    def lose_first_ack(operation, payload, *args, **kwargs):
        result = original(operation, payload, *args, **kwargs)
        if operation == 'turn.event.record' and payload.get('terminal'):
            replies.append(result)
            result = original(operation, payload, *args, **kwargs)
            replies.append(result)
        return result

    monkeypatch.setattr(storage.client, 'command', lose_first_ack)
    event = {'type': 'done', 'finishReason': 'stop'}
    assert turn_service.record_task_event(task, event, task_event={
        'task_id': task['id'], 'sequence': 0, 'event': event}) == 'carried'
    assert len(replies) == 2 and replies[0] == replies[1]
    assert _stored_event(storage, task['id'], 0)['event'] == {'v': 1, **event}


def test_terminal_transport_retry_postgres(tmp_path, monkeypatch):
    from tests.support.postgres_cluster import owned_postgres_cluster
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    from lib import turn_lifecycle
    with owned_postgres_cluster() as dsn:
        path = tmp_path / 'private-postgres-dsn'
        path.write_text(str(dsn))
        monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(path))
        supervisor = StorageSupervisor(project_root=tmp_path, backend='postgres', startup_timeout=60)
        runtime = StorageRuntime(supervisor, auto_restart=False)
        install_runtime_for_test(runtime)
        try:
            runtime.start()
            assert_terminal_transport_retry(turn_lifecycle, supervisor, monkeypatch)
        finally:
            install_runtime_for_test(None)


@pytest.mark.parametrize('terminal,required', [(True, True), (False, False), (None, False), ('true', False), (1, False)])
def test_only_a_typed_terminal_flag_selects_a_command_receipt(terminal, required):
    from lib.storage_authority_sql.operations import resolve_operation_contract
    receipt, _, _ = resolve_operation_contract('turn.event.record', 'command', {'terminal': terminal})
    assert receipt is required


@pytest.mark.parametrize('kind,field', [('query', 'terminal'), ('command', ''), ('command', True)])
def test_invalid_conditional_receipt_declarations_fail_closed(kind, field):
    from lib.storage_authority_sql.operations import OperationSpec
    with pytest.raises(ValueError, match='Conditional receipts'):
        OperationSpec(kind, False, lambda *_: None, receipt_when_true=field)
