"""Real common tool dispatch, file journal and selected-store recovery boundary."""
import copy
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage, turn_service as turn_service, owner as owner, _task, _discard
from lib.tasks_pkg.executor._execute import _execute_tool_one
from lib.tasks_pkg.handlers.project import _handle_project_tool
from lib.project_mod import modifications as mods
from lib.project_mod.write_tools import _ops
from lib.conversation_sync.repository import SidecarConversationSyncRepository
from lib.conversation_sync.service import ConversationSyncService, ConversationSyncNotFound

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    from lib import file_history
    base = tmp_path / 'project'; base.mkdir()
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    return base


def write(task, base, *, append=False, path="new.txt"):
    task['config']['projectPath'] = str(base)
    row = dict(query='write new.txt', roundNum=1, toolName='write_file', toolCallId='call', status='running',
               taskId=task['id'], attemptId=task['_attemptId'])
    task['toolRounds'] = [row]
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: _handle_project_tool)
    args = {'path': path, 'content': 'hello\n', 'append': append}
    result = _execute_tool_one(task, {}, 'write_file', 'call', args, 1, row, {}, str(base), True)
    return row, result


def service_for(storage):
    return ConversationSyncService(SidecarConversationSyncRepository(
        client_factory=lambda write=False: storage.client))


@pytest.mark.parametrize('fault', ['none', 'undo_ack_lost'])
def test_file_outcome_survives_store_restart_without_repeating_write(
        storage, turn_service, owner, project, monkeypatch, fault):
    _, task = _task(turn_service, storage, 'file-effect')
    if fault == 'undo_ack_lost':
        def fail(*a, **kw):
            raise OSError('injected undo acknowledgement loss after file replace')
        monkeypatch.setattr(_ops, '_record_modification_checked', fail)
    try:
        row, result = write(task, project, append=True)
        assert (project / 'new.txt').read_text() == 'hello\n', result
        effect = copy.deepcopy(row['executionEffect'])
        assert effect['reconciliation']['kind'] == 'project_file_write', result
        persisted = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert persisted['projection']['toolRounds'][0]['executionEffect']['reconciliation'] == effect['reconciliation']
        conv_id, turn_id = task['convId'], task['_turnId']
    finally:
        _discard(task)
    storage.stop(); storage.start()
    service = service_for(storage)
    for _ in range(3):
        value = service.inspect_tool_effect(conv_id, turn_id, 1, operation_id=effect['operationId'])
        assert value['outcome'] == 'file_published'
        assert value['reason'] == ('undo_committed' if fault == 'none' else 'prepared_file_applied')
    assert (project / 'new.txt').read_text() == 'hello\n'
    from lib.storage.errors import StorageError
    with pytest.raises((ConversationSyncNotFound, StorageError)) as denied:
        service.inspect_tool_effect(conv_id, turn_id, 2, operation_id=effect['operationId'])
    if isinstance(denied.value, StorageError):
        assert denied.value.code == 'database_forbidden'
    with pytest.raises(ConversationSyncNotFound):
        service.inspect_tool_effect(conv_id, turn_id, 1, operation_id='f' * 32)


@pytest.mark.parametrize('fault', ['before_commit', 'lost_ack', 'stop_after_ack'])
def test_reference_ack_precedes_file_publication(
        storage, turn_service, owner, project, monkeypatch, fault):
    _, task = _task(turn_service, storage, 'file-effect-barrier')
    original = turn_service.record_task_event
    bound = []
    def record(task, wire, *a, **kw):
        effect = wire.get('executionEffect') or {}
        if wire['type'] == 'tool_effect' and effect.get('reconciliation'):
            bound.append(copy.deepcopy(effect))
            assert not (project / 'new.txt').exists()
            if fault != 'before_commit':
                result = original(task, wire, *a, **kw)
                if fault == 'stop_after_ack':
                    task['aborted'] = True
                    return result
            raise OSError('injected file-reference ACK failure')
        return original(task, wire, *a, **kw)
    monkeypatch.setattr(turn_service, 'record_task_event', record)
    try:
        row, result = write(task, project)
        assert bound, result
        assert not (project / 'new.txt').exists(), result
        # Failed publication may have deleted its private prepared file. Never
        # turn a missing receipt into proof that an operation did not happen.
        value = service_for(storage).inspect_tool_effect(
            task['convId'], task['_turnId'], 1, operation_id=row['executionEffect']['operationId'])
        assert value['outcome'] == 'unknown'
    finally:
        _discard(task)
