"""Agent-visible inspection traverses common dispatch and the real file authority."""
import json
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_file_effect_inspection import (
    storage as storage, turn_service as turn_service, owner as owner, project as project, _task, _discard, write,
)
from lib.tasks_pkg.executor._execute import _execute_tool_one
from lib.tasks_pkg.handlers.tool_effect_inspection import handle_tool_effect_inspection
from lib.tools.tool_effect_inspection import TOOL_EFFECT_INSPECTION
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


def read(task, operation_id, number):
    row = dict(query='inspect publication', roundNum=number, toolName='inspect_tool_effect',
               toolCallId=f'inspect-{number}', status='running', taskId=task['id'], attemptId=task['_attemptId'])
    task['toolRounds'].append(row)
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: handle_tool_effect_inspection)
    task['_toolContractDocumentsByName'] = {'inspect_tool_effect': TOOL_EFFECT_INSPECTION.search_document()}
    result = _execute_tool_one(task, {}, 'inspect_tool_effect', row['toolCallId'],
        {'operation_id': operation_id}, number, row, {}, None, False)
    return row, json.loads(result[1])


def test_captured_write_error_gives_callable_inspection_and_fresh_read_evidence(
        storage, turn_service, owner, project, monkeypatch):
    from lib.conversation_sync import repository
    repository_type = repository.SidecarConversationSyncRepository
    monkeypatch.setattr(repository, 'SidecarConversationSyncRepository',
        lambda: repository_type(client_factory=lambda write=False: storage.client))
    _, task = _task(turn_service, storage, 'agent-file-inspection')
    def lose_undo_ack(*a, **kw):
        raise OSError('file replaced, undo acknowledgement unavailable')
    monkeypatch.setattr(_ops, '_record_modification_checked', lose_undo_ack)
    try:
        row, result = write(task, project, append=True)
        effect = row['executionEffect']; reference = effect['inspection']
        assert reference['turnId'] == task['_turnId'] and reference['conversationId'] == task['convId']
        assert reference['operationId'] == effect['operationId']
        assert 'inspect_tool_effect(' in result[1] and effect['operationId'] in result[1]
        assert task['_turnId'] in result[1]
        effects = []
        original = turn_service.record_task_event
        def observe(task, wire, *a, **kw):
            if wire['type'] == 'tool_effect': effects.append(wire)
            return original(task, wire, *a, **kw)
        monkeypatch.setattr(turn_service, 'record_task_event', observe)
        inspected, observed = read(task, effect['operationId'], 2)
        assert observed.get('outcome') == 'file_published' and observed['reason'] == 'prepared_file_applied'
        assert inspected['executionEffect']['recovery'] == 'read_only'
        assert effects == []
        target = project / 'new.txt'; assert target.read_text() == 'hello\n'
        target.write_text('a newer human change\n')
        _, observed = read(task, effect['operationId'], 3)
        assert observed['outcome'] == 'unknown' and observed['reason'] == 'evidence_conflict'
        assert target.read_text() == 'a newer human change\n'
        assert effects == []
    finally:
        _discard(task)
