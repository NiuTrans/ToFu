"""Only a task that owns its conversation turn receives deferred completions.

``completion_conversation_id`` (lib/tasks_pkg/conversation_delivery.py) names
the conversation that receives a task's timer continuations, ``current``
agent schedules and background-command completions. A child invocation
(swarm worker, orchestration step, approval delegate) runs under its parent's
``convId`` without owning a turn there, and a root task without a
conversation (Evolution stages, embedded runs) has none. Neither may create a
continuation that would start a turn in a conversation nobody there asked
for: the call is refused with a typed error before the scheduler runs, the
tool surface does not offer it, and command/poll prose never promises
automatic delivery. Headless requests are specified in
``tests/api/test_headless_no_saved_conversation.py``.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from lib.scheduler.tool_defs import SCHEDULE_TOOL_CREATE, TIMER_TOOL_CREATE
from lib.tasks_pkg.child_invocation import create_child_invocation
from lib.tasks_pkg.conversation_delivery import (
    completion_conversation_id,
    deferred_completion_refusal,
)
from lib.tasks_pkg.handlers.misc import _agents
from lib.tools.poll import KILL_TOOL, POLL_TOOL
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

pytestmark = pytest.mark.unit

OWNER = 71
PARENT_CONV = 'coordinator-conv'
TIMER = {'continuation_message': 'Report the build result.',
         'condition_command': 'test -f /nonexistent/ready'}
OWN_TURN_SCHEDULES = ({'task_type': 'agent', 'target_conv_id': 'current'},
                      {'task_type': 'agent', 'target_conv_id': PARENT_CONV})


def _tools():
    return deepcopy([PROJECT_TOOL_RUN_COMMAND, POLL_TOOL, KILL_TOOL,
                     TIMER_TOOL_CREATE, SCHEDULE_TOOL_CREATE])


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


def _root(conv_id):
    return {'id': f'root-{conv_id or "none"}', 'convId': conv_id, '_userId': OWNER,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'config': {'memoryEnabled': False, 'searchMode': 'off'}}


def _error(text):
    payload = json.loads(str(text))
    assert payload['status'] == 'error' and payload['retryable'] is False
    return payload


@pytest.fixture
def scheduler(monkeypatch):
    """The real handler; only the scheduler executor is recorded."""
    calls = []
    monkeypatch.setattr(_agents, 'execute_scheduler_tool',
                        lambda name, args: calls.append((name, dict(args))) or 'created')

    def call(task, name, arguments):
        round_entry = {'roundNum': 1, 'toolCallId': 'tc-lane', 'toolName': name,
                       'query': 'deferred lane'}
        _, content, _ = _agents._handle_scheduler_tool(
            task, {}, name, 'tc-lane', dict(arguments), 1, round_entry, {}, '', False)
        return str(content)

    call.calls = calls
    return call


def test_worker_timer_and_own_conversation_schedule_never_reach_the_scheduler(worker, scheduler):
    assert completion_conversation_id(worker) == ''
    timer = _error(scheduler(worker, 'timer_create', TIMER))
    assert timer['code'] == 'no_conversation_turn'
    assert "parent's conversation" in timer['message'] and 'No timer was created' in timer['message']
    assert 'run_command' in timer['nextAction'] and 'parent' in timer['nextAction']
    for arguments in OWN_TURN_SCHEDULES:
        schedule = _error(scheduler(worker, 'schedule_create', arguments))
        assert schedule['code'] == 'no_conversation_turn'
        assert 'No job was created' in schedule['message']
        assert 'target_conv_id' in schedule['nextAction']
    assert scheduler.calls == []


def test_worker_may_still_schedule_into_another_existing_conversation(worker, scheduler):
    assert scheduler(worker, 'schedule_create',
                     {'task_type': 'agent', 'target_conv_id': 'conv-other'}) == 'created'
    (name, arguments), = scheduler.calls
    assert name == 'schedule_create' and arguments['target_conv_id'] == 'conv-other'
    # The worker owns no conversation, so it never lends the parent's ID.
    assert arguments['_source_conv_id'] == ''


def test_worker_background_command_is_pull_only_not_refused(worker):
    assert deferred_completion_refusal(
        worker, 'run_command', {'command': 'sleep 30', 'background': True}) is None


def test_conversationless_root_cannot_create_a_continuation(scheduler):
    task = _root('')
    timer = _error(scheduler(task, 'timer_create', TIMER))
    current = _error(scheduler(task, 'schedule_create', OWN_TURN_SCHEDULES[0]))
    for payload in (timer, current):
        assert payload['code'] == 'no_conversation_turn'
        assert 'no conversation' in payload['message']
    assert 'run_command' in timer['nextAction']
    assert 'target_conv_id' in current['nextAction']
    # Before, ``current`` stayed literal and created a job for a missing conversation.
    assert scheduler.calls == []


def test_conversation_owner_keeps_its_continuations(scheduler):
    task = _root('conv-owner')
    for name, arguments in (('timer_create', TIMER),
                            ('schedule_create', OWN_TURN_SCHEDULES[0])):
        assert deferred_completion_refusal(task, name, arguments) is None
        assert scheduler(task, name, arguments) == 'created'
    assert [args['_source_conv_id'] for _, args in scheduler.calls] == ['conv-owner'] * 2


def _function(tools, name):
    return next((tool['function'] for tool in tools or []
                 if tool.get('function', {}).get('name') == name), None)


def test_worker_surface_withholds_timer_and_current_schedule(worker):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools

    tools, has_tools = assemble_round_tools(dict(worker['config']), worker, {})
    assert has_tools
    for catalog in (worker['_executable_tool_catalog'], tools):
        assert _function(catalog, 'timer_create') is None
        target = _function(catalog, 'schedule_create')['parameters']['properties'][
            'target_conv_id']['description']
        assert 'for this conversation' not in target and 'unavailable' in target
    assert 'timer_create' not in (worker.get('_toolContractDocumentsByName') or {})


def _assemble_root(conv_id):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools

    cfg = {'userId': OWNER, 'model': 'test', 'plugins': [], 'mcpEnabled': False,
           'memoryEnabled': False, 'preferencesEnabled': False, 'codeExecEnabled': True}
    task = dict(_root(conv_id), config=cfg)
    mcfg = {'project_path': '', 'project_enabled': False, 'search_mode': 'off',
            'search_enabled': False, 'fetch_enabled': False, 'code_exec_enabled': True,
            'browser_enabled': False, 'desktop_enabled': False, 'image_gen_enabled': False,
            'model': 'test'}
    assemble_round_tools(cfg, task, mcfg)
    return {tool['function']['name']: tool['function']
            for tool in task['_executable_tool_catalog']}, task


def test_conversationless_root_prose_never_promises_automatic_delivery():
    catalog, task = _assemble_root('')
    command = catalog['run_command']
    background = command['parameters']['properties']['background']['description']
    assert 'results arrive automatically' not in command['description']
    assert 'Nothing delivers its completion to this task automatically' in background
    assert 'delivered automatically' not in catalog['poll']['description']
    assert 'bg_*' in catalog['poll']['description']
    assert 'timer_create' not in catalog
    target = catalog['schedule_create']['parameters']['properties']['target_conv_id']
    assert 'for this conversation' not in target['description']
    document = json.dumps(task['_toolContractDocumentsByName']['run_command'])
    assert 'arrive automatically' not in document and 'delivered automatically' not in document


def test_conversation_owner_root_surface_is_canonical():
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.tools.project import with_command_input_observation_scope

    catalog, _ = _assemble_root('conv-owner')
    expected, = with_command_input_observation_scope([CODE_EXEC_TOOL])
    assert catalog['run_command'] == expected['function']
    assert catalog['poll'] == POLL_TOOL['function']
    assert catalog['kill'] == KILL_TOOL['function']
    assert catalog['timer_create'] == TIMER_TOOL_CREATE['function']
    assert catalog['schedule_create'] == SCHEDULE_TOOL_CREATE['function']


def test_control_advice_states_delivery_from_the_callers_fact():
    from lib.tools.background_guidance import background_command_guidance

    unowned = background_command_guidance(authority_catalog=[], has_conversation=False)
    owned = background_command_guidance(authority_catalog=[], has_conversation=True)
    assert 'automatic' not in unowned and 'when available' not in unowned
    assert 'parent or operator' in unowned
    assert 'delivered automatically' in owned and 'when available' not in owned
