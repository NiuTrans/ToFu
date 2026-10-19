"""Joined conversation completion releases tool graphs without widening authority."""
import copy

import pytest

from lib.tasks_pkg.orchestrator._post_commit import finish_committed_task
from tests.tasks_pkg.test_finalize_persist_before_autopilot import _mk_task

pytestmark = pytest.mark.unit


def task_with_tools():
    from lib.tools.contracts import compile_execution_contract_documents
    tools = [{'type': 'function', 'function': {'name': 'inspect', 'description': 'Read evidence',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
    documents = compile_execution_contract_documents(tools)
    task = _mk_task('tool-release')
    task.update(_turnId='turn', _attemptId='attempt', _tool_schema=tools,
        _executable_tool_catalog=tools, _toolContractDocumentsByName=documents,
        _executableToolSearchTextByName={'inspect': 'Read evidence'})
    task['config'] = {'model': 'm', 'toolPermissions': {'shell': 'deny'},
        '_executableToolCatalog': tools, '_toolContractDocumentsByName': documents,
        '_executableToolSearchTextByName': task['_executableToolSearchTextByName'],
        '_toolExecutionScope': 'selected', '_requiredDirectToolNames': ['inspect']}
    task['events'] = [{'type': 'message', 'content': 'retained replay'}]
    return task


@pytest.fixture
def quiet_observers(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.event_invariants.maybe_check_task_log', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.notify_terminal_conversation_change', lambda task: None)
    monkeypatch.setattr('lib.swarm.delivery.restore_unconsumed_results', lambda task: None)
    monkeypatch.setattr('lib.swarm.turn_handoff.resume_swarm_after_turn', lambda *a, **kw: None)


@pytest.mark.parametrize('status', ['done', 'error', 'aborted', 'interrupted'])
def test_post_commit_releases_tool_graphs_after_observers_capture_context(quiet_observers, status):
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    task = task_with_tools()
    task['status'] = status
    borrowed_config = task['config']
    original = copy.deepcopy(borrowed_config)
    events, rounds, content = task['events'], task['apiRounds'], task['content']
    captured = []

    def capture(task, *args, **kwargs):
        assert task['config'] is borrowed_config
        assert task['_executable_tool_catalog']
        captured.append(snapshot_conversation_child_policy(task, model='m',
            tools=task['_executable_tool_catalog']))

    def consolidate(task, messages, cfg):
        assert cfg is borrowed_config and cfg == original
        assert task['_toolContractDocumentsByName']

    finish_committed_task(task, messages=task['messages'], cfg=borrowed_config,
        project_enabled=False, project_path=None, commit_round=capture,
        consolidate_profile=consolidate)
    assert task['_executable_tool_catalog'] == task['_tool_schema'] == []
    assert task['_toolContractDocumentsByName'] == task['_executableToolSearchTextByName'] == {}
    assert task['config'] is not borrowed_config
    assert task['config']['_executableToolCatalog'] == []
    assert task['config']['_toolContractDocumentsByName'] == {}
    assert task['config']['_executableToolSearchTextByName'] == {}
    assert task['config']['toolPermissions'] == {'shell': 'deny'}
    assert task['config']['_toolExecutionScope'] == 'selected'
    assert task['config']['_requiredDirectToolNames'] == ['inspect']
    assert borrowed_config == original
    assert captured[0]['conversation_tool_policy']['_executableToolCatalog'] == original['_executableToolCatalog']
    assert captured[0]['tool_contracts'] == original['_toolContractDocumentsByName']
    assert task['events'] is events and task['apiRounds'] is rounds and task['content'] == content


@pytest.mark.parametrize('changes', [
    {'status': 'running'}, {'_terminalPersistencePending': True},
    {'_attemptId': None}, {'_inline_messages': True}, {'_vu_subtask': True},
    {'_transientRuntime': True},
])
def test_unsettled_or_nonconversation_tool_authority_is_retained(changes):
    from lib.tasks_pkg.manager._persist import _release_terminal_tool_state
    task = task_with_tools()
    task.update(changes)
    config, catalog, documents = task['config'], task['_executable_tool_catalog'], task['_toolContractDocumentsByName']
    assert _release_terminal_tool_state(task) == 0
    assert task['config'] is config
    assert task['_executable_tool_catalog'] is catalog and catalog
    assert task['_toolContractDocumentsByName'] is documents and documents


def test_early_persist_release_keeps_tools_until_post_commit(quiet_observers):
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    task = task_with_tools()
    catalog, config = task['_executable_tool_catalog'], task['config']
    _release_heavy_task_state(task)
    assert task['_executable_tool_catalog'] is catalog and catalog
    assert task['config'] is config


def test_profile_observer_failure_keeps_original_error_and_releases_joined_tools(quiet_observers):
    task = task_with_tools()
    original_config = task['config']
    failure = OSError('profile capture failed')

    def fail(task, messages, cfg):
        assert cfg['_executableToolCatalog']
        raise failure

    with pytest.raises(OSError) as caught:
        finish_committed_task(task, messages=task['messages'], cfg=original_config,
            project_enabled=False, project_path=None, commit_round=lambda *a, **kw: None,
            consolidate_profile=fail)
    assert caught.value is failure
    assert task['_executable_tool_catalog'] == []
    assert original_config['_executableToolCatalog']


def test_repeated_release_preserves_retired_config_and_remaining_policy():
    from lib.tasks_pkg.manager._persist import _release_terminal_tool_state
    task = task_with_tools()
    assert _release_terminal_tool_state(task) > 0
    config = task['config']
    assert _release_terminal_tool_state(task) == 0
    assert task['config'] is config
    assert config['toolPermissions'] == {'shell': 'deny'}
