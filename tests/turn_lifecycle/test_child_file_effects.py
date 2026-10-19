"""Registered children use real file publication and the selected durable store."""
import copy
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_file_effect_inspection import project as project
from tests.turn_lifecycle.test_task_event_delivery import storage as storage, turn_service as turn_service, owner as owner, _task, _discard
from lib.tasks_pkg.child_invocation import create_child_invocation
from lib.tasks_pkg.executor._execute import _execute_tool_one
from lib.tasks_pkg.handlers.project import _handle_project_tool
from lib.tasks_pkg.handlers.tool_effect_inspection import inspect_for_task
from lib.tasks_pkg.manager import discard_task

pytestmark = pytest.mark.unit


def child_of(parent, project):
    parent['config']['projectPath'] = str(project)
    return create_child_invocation(parent, model='offline-test', tools=[],
        objective='Write a child result', project_path=str(project), event_sink=lambda event: None,
        abort_check=lambda: False)


def write(child, project, call='call', path='child.txt'):
    child['config']['projectPath'] = str(project)
    row = dict(query='write child result', roundNum=1, toolName='write_file',
               toolCallId=call, status='running', taskId=child['id'])
    child['toolRounds'].append(row)
    child['_tool_env'] = SimpleNamespace(resolve=lambda name: _handle_project_tool)
    result = _execute_tool_one(child, {}, 'write_file', call,
        {'path': path, 'content': 'once\n', 'append': True}, 1, row, {}, str(project), True)
    return row, result


@pytest.mark.parametrize('lost_undo_ack', [False, True])
def test_child_file_evidence_survives_restart(storage, turn_service, owner, project,
                                             monkeypatch, lost_undo_ack):
    _, parent = _task(turn_service, storage, 'child-file')
    child = child_of(parent, project)
    if lost_undo_ack:
        def fail(*args, **kwargs):
            raise OSError('lost undo ACK after publication')
        monkeypatch.setattr('lib.project_mod.write_tools._ops._record_modification_checked', fail)
    try:
        row, result = write(child, project)
        effect = copy.deepcopy(row['executionEffect'])
        assert effect['scope'] == 'invocation', result
        assert effect['reconciliation']['taskId'] == child['id']
        assert effect['invocationInspection']['invocationId'] == child['id']
        assert not child.get('_turnId') and not child.get('_attemptId')
        arguments = {'invocation_id': child['id'], 'operation_id': effect['operationId']}
        context = {'_userId': 1, 'convId': parent['convId']}
    finally:
        discard_task(child['id'])
        _discard(parent)
    storage.stop(); storage.start()
    for _ in range(2):
        observed = inspect_for_task(context, arguments)
        assert observed['outcome'] == 'file_published'
        assert observed['reason'] == ('prepared_file_applied' if lost_undo_ack else 'undo_committed')
    assert (project / 'child.txt').read_text() == 'once\n'
    from lib.conversation_sync.service import ConversationSyncNotFound
    from lib.storage.errors import StorageError
    for changes in ({'_userId': 2}, {'convId': 'foreign'}):
        with pytest.raises((ConversationSyncNotFound, StorageError)):
            inspect_for_task({**context, **changes}, arguments)


@pytest.mark.parametrize('fault', ['before_commit', 'lost_ack'])
def test_child_reference_is_durable_before_publication(storage, turn_service, owner,
                                                      project, monkeypatch, fault):
    import json
    _, parent = _task(turn_service, storage, 'child-barrier')
    child = child_of(parent, project)
    command = storage.client.command
    intercepted = []
    def injected(operation, payload, *args, **kwargs):
        if operation == 'task_results.checkpoint' and payload['key'] == child['id']:
            rows = json.loads(payload['value'].get('tool_rounds') or '[]')
            if any(row['executionEffect'].get('reconciliation') for row in rows):
                intercepted.append(payload)
                assert not (project / 'child.txt').exists()
                if fault == 'lost_ack':
                    command(operation, payload, *args, **kwargs)
                raise OSError('injected reference acknowledgement failure')
        return command(operation, payload, *args, **kwargs)
    monkeypatch.setattr(storage.client, 'command', injected)
    try:
        row, result = write(child, project)
        assert intercepted, result
        assert not (project / 'child.txt').exists()
        observed = inspect_for_task(child, {'invocation_id': child['id'],
                                          'operation_id': row['executionEffect']['operationId']})
        assert observed['outcome'] == 'unknown'
    finally:
        discard_task(child['id'])
        _discard(parent)


def test_parallel_child_writes_keep_both_exact_receipts(storage, turn_service, owner, project):
    from concurrent.futures import ThreadPoolExecutor
    _, parent = _task(turn_service, storage, 'parallel-child-files')
    child = child_of(parent, project)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(write, child, project, f'call-{index}', f'{index}.txt')
                       for index in range(2)]
            results = [future.result() for future in futures]
        operations = [row['executionEffect']['operationId'] for row, _ in results]
        assert len(set(operations)) == 2
        for operation in operations:
            assert inspect_for_task(child, {'invocation_id': child['id'],
                'operation_id': operation})['outcome'] == 'file_published'
        assert [(project / f'{index}.txt').read_text() for index in range(2)] == ['once\n'] * 2
    finally:
        discard_task(child['id'])
        _discard(parent)


def test_failed_child_write_requires_an_observed_inspection_before_new_write(
        storage, turn_service, owner, project, monkeypatch):
    import json
    from tests.turn_lifecycle.test_file_recovery_admission import prepare, invoke
    from lib.project_mod.write_tools import _ops
    _, parent = _task(turn_service, storage, 'child-file-replan')
    child = child_of(parent, project)
    original = _ops._record_modification_checked
    def fail(*args, **kwargs):
        raise OSError('lost undo acknowledgement')
    monkeypatch.setattr(_ops, '_record_modification_checked', fail)
    try:
        source, _ = write(child, project)
        definitions = prepare(child, project)
        arguments = {'path': 'child.txt', 'content': 'next\n', 'append': True}
        row, result = invoke(child, project, definitions, 'write_file', arguments, 1)
        assert row['status'] == 'rejected' and 'tool_effect_inspection_required' in result
        assert (project / 'child.txt').read_text() == 'once\n'
        row, result = invoke(child, project, definitions, 'inspect_tool_effect', {
            'invocation_id': child['id'], 'operation_id': source['executionEffect']['operationId']}, 2)
        assert json.loads(result)['outcome'] == 'file_published'
        monkeypatch.setattr(_ops, '_record_modification_checked', original)
        row, result = invoke(child, project, definitions, 'write_file', arguments, 3)
        assert row['status'] == 'done', result
        assert (project / 'child.txt').read_text() == 'once\nnext\n'
    finally:
        discard_task(child['id'])
        _discard(parent)


@pytest.mark.parametrize('fault', ['missing_registration', 'intent_ack_lost', 'cancel_after_reference'])
def test_child_admission_failure_never_enters_file_publication(
        storage, turn_service, owner, project, monkeypatch, fault):
    import json
    _, parent = _task(turn_service, storage, 'child-admission')
    child = child_of(parent, project)
    command = storage.client.command
    if fault == 'missing_registration':
        child.pop('_archiveScope')
    else:
        def injected(operation, payload, *args, **kwargs):
            result = command(operation, payload, *args, **kwargs)
            if operation == 'task_results.checkpoint' and payload['key'] == child['id']:
                rows = json.loads(payload['value']['tool_rounds'])
                if fault == 'intent_ack_lost':
                    raise OSError('lost intent ACK')
                if any(row['executionEffect'].get('reconciliation') for row in rows):
                    child['aborted'] = True
            return result
        monkeypatch.setattr(storage.client, 'command', injected)
    try:
        row, result = write(child, project)
        assert not (project / 'child.txt').exists(), result
        assert row['executionEffect']['state'] in ('not_started', 'unknown', 'returned')
    finally:
        discard_task(child['id'])
        _discard(parent)


def test_killed_child_is_queryable_after_store_restart_without_replay(
        storage, turn_service, owner, project, tmp_path):
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time
    from lib.project_mod import modifications
    client = storage.client
    native = hasattr(client, '_secret')
    ready, logfile = tmp_path / 'child-ready.json', tmp_path / 'child-writer.log'
    values = dict(endpoint=client.endpoint, native=native,
        token=client._secret.decode() if native else client._token,
        owner_id=getattr(client, '_owner_id', 1), tenant_id=getattr(client, '_tenant_id', None),
        ready=str(ready), project=str(project), journal=modifications.SESSIONS_DIR, child=True)
    with logfile.open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'tests.support.file_effect_crash_writer'],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, stdout=log,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            process.stdin.write(json.dumps(values)); process.stdin.close()
            deadline = time.monotonic() + 30
            while not ready.exists():
                assert process.poll() is None, logfile.read_text()[-3000:]
                assert time.monotonic() < deadline, 'child did not reach publication'
                time.sleep(.02)
            source = json.loads(ready.read_text())
            context = dict(_userId=1, convId=source['convId'])
            arguments = dict(invocation_id=source['id'], operation_id=source['operationId'])
            observed = inspect_for_task(context, arguments)
            assert observed['outcome'] == 'unknown' and observed['reason'] == 'writer_active'
            os.killpg(process.pid, signal.SIGKILL)
            assert process.wait(timeout=10) == -signal.SIGKILL
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
    storage.stop(); storage.start()
    observed = inspect_for_task(context, arguments)
    assert observed['outcome'] == 'file_published' and observed['reason'] == 'prepared_file_applied'
    assert (project / 'child.txt').read_text() == 'once\n'


def test_transient_reference_ack_loss_retries_only_the_frozen_checkpoint(
        storage, turn_service, owner, project, monkeypatch):
    import json
    from lib.storage.errors import StorageError
    _, parent = _task(turn_service, storage, 'child-receipt-retry')
    child = child_of(parent, project)
    command = storage.client.command
    candidates = []
    def injected(operation, payload, *args, **kwargs):
        is_reference = (operation == 'task_results.checkpoint' and payload['key'] == child['id']
            and any(row['executionEffect'].get('reconciliation')
                    for row in json.loads(payload['value']['tool_rounds'])))
        if is_reference:
            assert not (project / 'child.txt').exists()
            candidates.append(copy.deepcopy(payload))
        result = command(operation, payload, *args, **kwargs)
        if is_reference and len(candidates) == 1:
            raise StorageError('database_unavailable', 'injected lost ACK', retryable=True)
        return result
    monkeypatch.setattr(storage.client, 'command', injected)
    try:
        row, result = write(child, project)
        assert len(candidates) == 2 and candidates[0] == candidates[1], result
        assert row['status'] == 'done'
        assert (project / 'child.txt').read_text() == 'once\n'
    finally:
        discard_task(child['id'])
        _discard(parent)


def test_registered_grandchild_retains_its_own_durable_producer(storage, turn_service, owner, project):
    _, parent = _task(turn_service, storage, 'grandchild-file')
    child = child_of(parent, project)
    grandchild = child_of(child, project)
    try:
        assert child['_transientRuntime'] and grandchild['_transientRuntime']
        row, result = write(grandchild, project)
        effect = row['executionEffect']
        assert effect['scope'] == 'invocation', result
        assert effect['reconciliation']['taskId'] == grandchild['id']
        identity = storage.client.query('invocation.resolve_child', {
            'archive_scope': grandchild['_archiveScope'], 'user_id': 1})
        assert identity['parent_task_id'] == child['id']
        context = dict(_userId=1, convId=parent['convId'])
        arguments = dict(invocation_id=grandchild['id'], operation_id=effect['operationId'])
    finally:
        discard_task(grandchild['id'])
        discard_task(child['id'])
        _discard(parent)
    storage.stop(); storage.start()
    assert inspect_for_task(context, arguments)['outcome'] == 'file_published'
    assert (project / 'child.txt').read_text() == 'once\n'
