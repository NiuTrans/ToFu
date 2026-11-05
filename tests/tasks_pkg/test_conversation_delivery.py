"""Headless request tasks lose every deferred-completion lane.

``create_task(..., saved_conversation=False)`` is the single writer of the
marker. Transient tasks carry ``_inline_messages``/``_transientRuntime`` too,
but a root task that owns its saved conversation keeps its lanes, and
ordinary conversations keep their full tool surface. A storage-free runtime
run keeps explicit background commands with pull-only completions. Child invocations and
tasks without a conversation are specified in
``tests/tasks_pkg/test_deferred_lane_owner.py``; the headless side end to end
in ``tests/api/test_headless_no_saved_conversation.py``.
"""

from __future__ import annotations

import json

import pytest

from lib.tasks_pkg.conversation_delivery import (
    conversation_delivery_available,
    deferred_completion_refusal,
    has_no_saved_conversation,
)
from lib.tasks_pkg.manager import create_task, discard_task

pytestmark = pytest.mark.unit

OWNER = 71
BACKGROUND = {'command': 'sleep 30', 'background': True}


@pytest.fixture
def created():
    tasks = []

    def create(conv_id, **kwargs):
        config = kwargs.pop('config', {})
        task = create_task(conv_id, [{'role': 'user', 'content': 'hi'}], config,
                           user_id=OWNER, supersede=False, transient=True, **kwargs)
        tasks.append(task)
        return task

    yield create
    for task in tasks:
        discard_task(task['id'])


def test_only_task_creation_declares_a_missing_conversation(created):
    headless = created('api-job-label', saved_conversation=False)
    assert has_no_saved_conversation(headless)
    assert not conversation_delivery_available(headless)
    assert deferred_completion_refusal(headless, 'run_command', BACKGROUND)
    assert deferred_completion_refusal(headless, 'timer_create', {})
    assert deferred_completion_refusal(headless, 'run_command', {'command': 'ls'}) is None
    for target in ('current', 'api-job-label'):
        assert deferred_completion_refusal(
            headless, 'schedule_create', {'task_type': 'agent', 'target_conv_id': target})
    # An existing conversation receives the scheduled turn; a missing target
    # keeps the executor's own required-argument error.
    for arguments in ({'task_type': 'agent', 'target_conv_id': 'conv-real'},
                      {'task_type': 'agent'}, {'task_type': 'command'}):
        assert deferred_completion_refusal(headless, 'schedule_create', arguments) is None


def test_headless_request_refuses_every_sub_agent_launch(created, monkeypatch):
    headless = created('api-job-label', saved_conversation=False)
    for name, arguments in (('spawn_agents', {'agents': [{'id': 'a', 'objective': 'x'}]}),
                            ('manage_agents', {'action': 'assign', 'agent_id': 'a'})):
        refusal = json.loads(str(deferred_completion_refusal(headless, name, arguments)))
        assert refusal['code'] == 'no_saved_conversation'
        assert 'directly' in refusal['nextAction']
    # Roster maintenance starts nothing and stays available.
    for action in ('list', 'inspect', 'create', 'archive'):
        assert deferred_completion_refusal(headless, 'manage_agents', {'action': action}) is None

    from lib.tasks_pkg.handlers import retained_agents
    monkeypatch.setattr('lib.swarm.retained.execute_manage_agents',
                        lambda *args, **kwargs: pytest.fail('assign reached the roster'))
    _, content, _ = retained_agents._handle_manage_agents(
        headless, {}, 'manage_agents', 'tc-assign',
        {'action': 'assign', 'agent_id': 'a', 'objective': 'x'}, 1,
        {'roundNum': 1, 'toolCallId': 'tc-assign', 'toolName': 'manage_agents',
         'query': 'assign'}, {}, '', False)
    assert json.loads(str(content))['code'] == 'no_saved_conversation'


def test_transient_root_inside_a_saved_conversation_keeps_its_lanes(created):
    # Transient like a swarm worker, but a root task that owns its saved
    # conversation. A real child invocation has no conversation lanes; see
    # tests/tasks_pkg/test_deferred_lane_owner.py.
    worker = created('conv-parent')
    worker['_unattended'] = True
    assert worker['_inline_messages'] and worker['_transientRuntime']
    assert not has_no_saved_conversation(worker)
    assert conversation_delivery_available(worker)
    for name, arguments in (('run_command', BACKGROUND), ('timer_create', {}),
                            ('schedule_create', {'task_type': 'agent',
                                                 'target_conv_id': 'current'})):
        assert deferred_completion_refusal(worker, name, arguments) is None


def test_transient_root_explicit_background_still_promises_conversation_delivery(
        created, monkeypatch):
    from lib.tasks_pkg.handlers import code_exec

    worker = created('conv-parent')
    worker['_unattended'] = True
    started = []
    monkeypatch.setattr('lib.tasks_pkg.background_commands.start_background_command',
                        lambda **kwargs: started.append(kwargs) or 'bg_worker')
    monkeypatch.setattr(code_exec, '_finalize_tool_round', lambda *args, **kwargs: None)
    _, content, _ = code_exec._handle_code_exec(
        worker, None, 'run_command', 'tc-worker', dict(BACKGROUND), 1,
        {'toolName': 'code_exec', 'status': 'searching'}, {}, '/tmp', False)
    assert len(started) == 1 and started[0]['task'] is worker
    assert '[Command moved to background: bg_worker.' in content
    assert 'will be shown in the conversation' in content


def test_storage_free_run_is_pull_only_but_keeps_background_commands(created, monkeypatch):
    run = created('agent-job-label', config={'_storageFreeRuntime': True})
    assert not has_no_saved_conversation(run)
    assert not conversation_delivery_available(run)
    assert deferred_completion_refusal(run, 'run_command', BACKGROUND) is None
    assert deferred_completion_refusal(run, 'timer_create', {})

    from lib.tasks_pkg.handlers import code_exec
    from lib.tools.poll import KILL_TOOL, POLL_TOOL
    run['_executable_tool_catalog'] = [POLL_TOOL, KILL_TOOL]
    started = []
    monkeypatch.setattr('lib.tasks_pkg.background_commands.start_background_command',
                        lambda **kwargs: started.append(kwargs) or 'bg_run')
    monkeypatch.setattr(code_exec, '_finalize_tool_round', lambda *args, **kwargs: None)
    _, content, _ = code_exec._handle_code_exec(
        run, None, 'run_command', 'tc-run', dict(BACKGROUND), 1,
        {'toolName': 'code_exec', 'status': 'searching'}, {}, '/tmp', False)
    assert len(started) == 1 and started[0]['task'] is run
    assert 'shown in the conversation' not in content
    assert 'end this turn' not in content
    assert 'Nothing delivers its completion to this run' in content
    assert 'keeps running' in content
    assert 'kill' in content


def _surface(**kwargs):
    from lib.tasks_pkg.model_config import _assemble_tool_list

    cfg = {'userId': OWNER, 'model': 'test', 'plugins': [], 'mcpEnabled': False,
           'memoryEnabled': False, 'preferencesEnabled': False}
    tools, _ = _assemble_tool_list(cfg, '', False, 'surface-task', 'off', False,
                                   False, True, False, False, **kwargs)
    catalog = {tool['function']['name']: tool['function']
               for tool in cfg['_executableToolCatalog']}
    return catalog, tools, cfg.get('_toolContractDocumentsByName') or {}


def test_ordinary_conversation_surface_is_unchanged():
    from lib.scheduler.tool_defs import SCHEDULE_TOOL_CREATE, TIMER_TOOL_CREATE
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.tools.poll import KILL_TOOL, POLL_TOOL
    from lib.tools.project import with_command_input_observation_scope

    catalog, _, _ = _surface(conv_id='conv-1')
    expected, = with_command_input_observation_scope([CODE_EXEC_TOOL])
    assert catalog['run_command'] == expected['function']
    assert catalog['poll'] == POLL_TOOL['function']
    assert catalog['kill'] == KILL_TOOL['function']
    assert catalog['timer_create'] == TIMER_TOOL_CREATE['function']
    assert catalog['schedule_create'] == SCHEDULE_TOOL_CREATE['function']


def test_storage_free_surface_offers_pull_only_controls():
    from lib.tasks_pkg.model_config import _assemble_tool_list

    cfg = {'userId': OWNER, 'model': 'test', 'plugins': [], 'mcpEnabled': False,
           'memoryEnabled': False, 'preferencesEnabled': False,
           '_storageFreeRuntime': True}
    tools, _ = _assemble_tool_list(
        cfg, '', False, 'storage-free-task', 'off', False, False, True, False, False,
        conv_id='agent-job',
        completion_delivery=conversation_delivery_available({'config': cfg}))
    catalog = {tool['function']['name']: tool['function']
               for tool in cfg['_executableToolCatalog']}
    assert 'poll' in catalog and 'kill' in catalog
    command = catalog['run_command']
    assert 'arrive automatically' not in command['description']
    assert 'Nothing delivers its completion to this task automatically' in (
        command['parameters']['properties']['background']['description'])
    for name in ('poll', 'kill'):
        assert 'delivered automatically' not in catalog[name]['description']


def test_headless_surface_withholds_timers_and_describes_foreground_commands():
    from lib.tools.code_exec import CODE_EXEC_TOOL

    catalog, wire, contracts = _surface(conv_id='', no_saved_conversation=True)
    assert 'timer_create' not in catalog
    assert 'timer_create' not in {tool['function']['name'] for tool in wire or []}
    command = catalog['run_command']
    background = command['parameters']['properties']['background']
    assert 'no saved conversation' in command['description']
    assert 'arrive automatically' not in command['description']
    assert background['description'].startswith('Unavailable')
    # Prose only: validation keeps accepting the argument so the handler can
    # answer with its typed refusal and next action.
    assert {key: value for key, value in background.items() if key != 'description'} == {
        key: value for key, value in CODE_EXEC_TOOL['function']['parameters'][
            'properties']['background'].items() if key != 'description'}
    target = catalog['schedule_create']['parameters']['properties']['target_conv_id']
    assert 'current' in target['description'] and 'unavailable' in target['description']
    # No command or sub-agent handle can exist here; kill would stop nothing.
    text = json.dumps(catalog['poll'])
    assert 'bg_*' not in text and 'delivered automatically' not in text
    assert 'swarm' not in text and 'spawn_agents' not in text
    assert catalog['poll']['parameters']['properties']['id']['description'].startswith('One handle: a task id.')
    assert 'kill' not in catalog
    assert not {'spawn_agents', 'get_agent_result', 'send_agent_message', 'get_agent_messages',
                'set_approval_delegate'} & set(catalog)
    # Tool-search documents are compiled from the same projected catalog.
    document = json.dumps(contracts['run_command'])
    assert 'no saved conversation' in document
    assert 'delivered automatically' not in document and 'arrive automatically' not in document


# Reviewed set of tools whose every call registers in, reads from or reports
# into the request's own saved conversation (ToolSpec.saved_conversation_tools).
SAVED_CONVERSATION_TOOLS = frozenset({
    'spawn_agents', 'get_agent_result', 'send_agent_message', 'get_agent_messages',
    'set_approval_delegate', 'list_agents', 'manage_agents', 'get_exploration',
    'manage_exploration', 'read_working_facts', 'record_working_fact',
    'send_project_message', 'get_project_messages', 'consider_project_message',
})


def test_headless_surface_withholds_every_saved_conversation_tool(monkeypatch, tmp_path):
    """One registry declaration, applied before routing or Tool Search see it."""
    from lib.tasks_pkg.model_config import _assemble_tool_list
    from lib.tools.registry import all_specs

    # Storage seam: this project has no Coordinator binding.
    monkeypatch.setattr(
        'lib.conversations.project_coordination.ProjectCoordinationRepository.read',
        lambda self, path: {'availability': 'unavailable'})

    def catalog(**kwargs):
        cfg = {'userId': OWNER, 'model': 'test', 'plugins': [], 'mcpEnabled': False,
               'memoryEnabled': False, 'preferencesEnabled': False,
               'projectPath': str(tmp_path)}
        _assemble_tool_list(cfg, str(tmp_path), True, 'surface-task', 'off', False,
                            False, False, False, False, conv_id='api-job', **kwargs)
        return {tool['function']['name'] for tool in cfg['_executableToolCatalog']}

    declared = set().union(*(spec.saved_conversation_tools for spec in all_specs()))
    assert declared == SAVED_CONVERSATION_TOOLS
    # The same project surface with a saved conversation offers every family.
    assert SAVED_CONVERSATION_TOOLS <= catalog()
    headless = catalog(no_saved_conversation=True)
    assert not SAVED_CONVERSATION_TOOLS & headless
    # Owner- and task-scoped neighbours of those families stay available.
    assert {'schedule_create', 'schedule_list', 'integration_checkpoint',
            'integration_submit', 'list_conversations', 'get_conversation'} <= headless


def test_saved_conversation_tools_must_be_declared_names():
    from lib.tools.registry import ToolSpec, register_tool_spec

    with pytest.raises(ValueError, match='saved_conversation_tools'):
        register_tool_spec(ToolSpec(
            'headless-declaration-probe', lambda ctx: [],
            provides=frozenset({'probe_tool'}),
            saved_conversation_tools=frozenset({'undeclared_tool'})))
