"""A delegated child's background command reports only to that child.

A child invocation (swarm worker, orchestration step, approval delegate) runs
under its parent's ``convId`` without owning a turn there. Its background
commands must never publish into, wake, or hold the parent's conversation.
The child reads the result with ``poll``, and its receipt and tool prose say
so (docs/modules/task_engine.md, "Background command delivery owner").
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from copy import deepcopy
from unittest.mock import Mock

import pytest

from lib.tasks_pkg import background_commands as bg
from lib.tasks_pkg.child_invocation import create_child_invocation
from lib.tasks_pkg.handlers import code_exec
from lib.tools.poll import KILL_TOOL, POLL_TOOL
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

pytestmark = pytest.mark.unit

OWNER = 71
PARENT_CONV = 'coordinator-conv'
RESULT = '$ echo done\ndone\n[exit code: 0]'


def _tools():
    return deepcopy([PROJECT_TOOL_RUN_COMMAND, POLL_TOOL, KILL_TOOL])


@pytest.fixture
def lanes(monkeypatch, tmp_path):
    """Real handler, registry, receipt and publication; stub only queue/inbox/wake I/O."""
    monkeypatch.setattr(bg, '_records_root', lambda: str(tmp_path))
    published = {'queue': [], 'inbox': [], 'wake': []}

    def enqueue(conv_id, *args, **kwargs):
        published['queue'].append(conv_id)
        return {'queueId': f'queue-{len(published["queue"])}'}

    monkeypatch.setattr('lib.message_queue.enqueue_message', enqueue)
    monkeypatch.setattr('lib.agent_inbox.enqueue',
                        lambda key, *args, **kwargs: published['inbox'].append(key))
    monkeypatch.setattr('lib.message_queue.dispatch_completed_background_batch',
                        lambda conv_id, **kwargs: published['wake'].append(conv_id))
    monkeypatch.setattr(code_exec, '_finalize_tool_round', Mock())
    release = threading.Event()
    release.set()
    published['release'] = release

    def execute(*args, **kwargs):
        assert release.wait(5)
        return RESULT

    monkeypatch.setattr('lib.project_mod.execute_standalone_command', execute)
    with bg._lock:
        bg._registry.clear()
    yield published
    release.set()
    with bg._lock:
        command_ids = list(bg._registry)
    for command_id in command_ids:
        _join(command_id)
    with bg._lock:
        bg._registry.clear()


def _join(command_id):
    """Join the command's worker: publication and wake run after its status."""
    names = {f'bg-cmd-{command_id[3:]}', f'tofu-background-command-{command_id[3:]}'}
    for thread in threading.enumerate():
        if thread.name in names:
            thread.join(5)
            assert not thread.is_alive()


@pytest.fixture
def worker():
    child = create_child_invocation(
        {'id': 'coordinator-task', 'convId': PARENT_CONV, '_userId': OWNER,
         '_transientRuntime': True,
         'config': {'memoryEnabled': False, 'searchMode': 'off'}},
        model='fixture-model', tools=_tools(), objective='worker objective',
        event_sink=lambda event: None, abort_check=lambda: False)
    child['_executable_tool_catalog'] = _tools()
    yield child
    from lib.tasks_pkg.manager import discard_task
    discard_task(child['id'])


def _background(task, command='echo done'):
    _, receipt, _ = code_exec._handle_code_exec(
        task, {}, 'run_command', 'tc-bg', {'command': command, 'background': True},
        1, {}, {}, None, False)
    return re.search(r'bg_[0-9a-f]{12}', receipt).group(0), receipt


def _settled(command_id):
    _join(command_id)
    return bg.check_background_command(command_id, user_id=OWNER)


def test_worker_completion_never_enters_or_wakes_the_parent_conversation(lanes, worker):
    command_id, receipt = _background(worker)
    snapshot = _settled(command_id)

    assert lanes['queue'] == [] and lanes['inbox'] == [] and lanes['wake'] == []
    assert snapshot['status'] == 'completed'
    assert snapshot['result'] == RESULT
    assert snapshot['delivery_status'] == 'not_applicable'
    assert 'shown in the conversation' not in receipt
    assert 'automatic delivery' not in receipt
    assert '`poll`' in receipt


def test_coordinator_completion_still_reaches_its_own_conversation(lanes):
    coordinator = {'id': 'coordinator-task', 'convId': PARENT_CONV, '_userId': OWNER,
                   '_executable_tool_catalog': _tools()}
    command_id, receipt = _background(coordinator)
    snapshot = _settled(command_id)

    assert snapshot['delivery_status'] == 'queued'
    assert lanes['queue'] == [PARENT_CONV]
    assert lanes['inbox'] == [PARENT_CONV]
    assert lanes['wake'] == [PARENT_CONV]
    assert 'shown in the conversation' in receipt


def test_worker_restart_record_never_names_the_parent_conversation(lanes, worker, tmp_path):
    lanes['release'].clear()
    command_id, _ = _background(worker)
    with open(os.path.join(tmp_path, command_id, 'record.json'), encoding='utf-8') as stream:
        record = json.load(stream)

    assert record['conv_id'] == ''
    # The restart sweep settles such a record without publishing anywhere.
    assert bg._deliver_record(record, RESULT) == 'not_applicable'
    assert lanes['queue'] == [] and lanes['inbox'] == []


def test_running_worker_command_does_not_hold_the_parent_batch_wake(lanes, worker):
    lanes['release'].clear()
    command_id, _ = _background(worker)
    try:
        assert bg.check_background_command(command_id, user_id=OWNER)['status'] == 'running'
        assert bg.conversation_results_complete(conv_id=PARENT_CONV, user_id=OWNER)
        # Activity still shows the command under the conversation it ran in.
        rows = bg.list_background_commands(user_id=OWNER)
        assert [(row['command_id'], row['conv_id']) for row in rows] == [
            (command_id, PARENT_CONV)]
    finally:
        lanes['release'].set()
    _settled(command_id)
    assert lanes['wake'] == []


def test_completion_after_the_worker_settled_is_retained_with_a_typed_reason(
        lanes, worker, caplog):
    caplog.set_level(logging.INFO, logger='lib.tasks_pkg')
    lanes['release'].clear()
    command_id, _ = _background(worker)
    from lib.tasks_pkg.manager import discard_task
    discard_task(worker['id'])
    lanes['release'].set()
    snapshot = _settled(command_id)

    assert snapshot['result'] == RESULT
    assert lanes['queue'] == [] and lanes['inbox'] == [] and lanes['wake'] == []
    retained = [record.getMessage() for record in caplog.records
                if command_id in record.getMessage() and 'not published' in record.getMessage()]
    assert len(retained) == 1
    assert 'reason=launcher_settled' in retained[0]


@pytest.mark.parametrize('unattended', [True, False])
def test_steer_of_the_borrowed_conversation_never_hands_off_a_worker_command(
        lanes, worker, unattended, monkeypatch):
    from lib.tasks_pkg.handlers._background_command import run_with_steer_handoff
    if not unattended:
        worker.pop('_unattended')
    worker['status'] = 'running'
    monkeypatch.setattr('lib.agent_inbox.has_pending',
                        lambda key, modes=None: key == PARENT_CONV and modes == ['user-steer'])

    def execute(command_task):
        command_task['_subprocess_pid'] = os.getpid()
        threading.Event().wait(0.5)
        command_task.pop('_subprocess_pid')
        return RESULT

    result = run_with_steer_handoff(
        task=worker, config=None, command='sleep', execute=execute,
        round_num=1, tool_call_id='tc-steer')

    assert result == RESULT
    assert bg.list_background_commands(user_id=OWNER) == []
    assert lanes['queue'] == [] and lanes['wake'] == []


def test_capped_poll_tells_only_the_conversation_owner_to_end_its_turn(lanes, worker, monkeypatch):
    from lib.tasks_pkg.handlers import poll as poll_mod
    monkeypatch.setattr(poll_mod, '_finalize_tool_round', Mock())
    coordinator = {'id': 'coordinator-task', 'convId': PARENT_CONV, '_userId': OWNER,
                   '_executable_tool_catalog': _tools()}
    lanes['release'].clear()
    worker_command, _ = _background(worker)
    coordinator_command, _ = _background(coordinator)
    try:
        views = {
            name: poll_mod._handle_poll(
                task, {}, 'poll', f'tc-{name}', {'id': command_id, 'wait_s': 1},
                2, {}, {}, None, False)[1]
            for name, task, command_id in (
                ('worker', worker, worker_command),
                ('coordinator', coordinator, coordinator_command))}
    finally:
        lanes['release'].set()

    assert 'Wait cap reached' in views['worker']
    assert 'delivered automatically' not in views['worker']
    from lib.tools.poll import POLL_WAIT_CAP_S
    assert f'poll(id={worker_command!r}, wait_s={POLL_WAIT_CAP_S}) again' in views['worker']
    assert 'end the turn — completion is delivered automatically' in views['coordinator']


def _function(tools, name):
    return next(tool['function'] for tool in tools if tool['function']['name'] == name)


@pytest.mark.parametrize('controls', [(), (POLL_TOOL,), (KILL_TOOL,), (POLL_TOOL, KILL_TOOL)])
def test_control_projection_never_restores_a_delivery_promise(controls):
    from lib.tools.background_guidance import (
        project_background_tool_guidance, project_pull_only_completion_tools)
    projected = project_pull_only_completion_tools(_tools())
    for _ in range(2):  # a latched wire schema is projected again
        projected = project_background_tool_guidance(
            projected, authority_catalog=deepcopy([PROJECT_TOOL_RUN_COMMAND, *controls]))
    background = _function(projected, 'run_command')['parameters']['properties'][
        'background']['description']
    assert background.startswith(
        'Start in background and return a command_id handle. '
        'Nothing delivers its completion to this task automatically. ')
    assert 'automatic delivery' not in background
    assert ('`poll`' in background) == (POLL_TOOL in controls)


def test_worker_tool_prose_never_promises_automatic_completion_delivery(worker):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    from lib.tools.gateway import preserve_background_wire_tools

    tools, has_tools = assemble_round_tools(dict(worker['config']), worker, {})
    assert has_tools
    wire = preserve_background_wire_tools(
        tools, authority_catalog=worker['_executable_tool_catalog'])
    for catalog in (worker['_executable_tool_catalog'], wire):
        command = _function(catalog, 'run_command')
        background = command['parameters']['properties']['background']['description']
        poll = _function(catalog, 'poll')['description']
        assert 'results arrive automatically' not in command['description']
        assert 'delivered automatically' not in background
        assert 'Nothing delivers its completion to this task automatically' in background
        assert '`poll`' in background
        assert 'delivered automatically' not in poll
        assert 'bg_*' in poll
