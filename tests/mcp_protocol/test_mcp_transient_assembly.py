"""Headless message labels never grant durable conversation declaration access."""
import pytest

from lib.mcp import tool_search
from lib.storage.mcp_declarations import MCPDeclarationError, MCPDeclarationStore
from lib.tasks_pkg.model_config import _assemble_tool_list
from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
from tests.mcp_protocol.test_mcp_declaration_lifecycle import bridge
from tests.mcp_protocol.test_mcp_tool_search import _large_snapshot
from tests.goals.test_goal_invocation_integration import runtime as _invocation_runtime_fixture
invocation_runtime = _invocation_runtime_fixture

pytestmark = pytest.mark.unit


@pytest.fixture
def assembly(monkeypatch):
    tool_search.clear_tool_search_caches()
    bridge(monkeypatch, _large_snapshot()[:2])
    accesses = []
    class MissingConversation:
        def query(self, operation, payload):
            accesses.append((operation, payload))
            assert operation == 'conversation.get'
            return None
    monkeypatch.setattr(MCPDeclarationStore, '_storage', lambda self: MissingConversation())
    cfg = {'userId': 7, 'model': 'test', 'memoryEnabled': False, 'schedulerEnabled': False,
           'preferencesEnabled': False, 'plugins': [], 'mcpActiveToolLimit': 2}
    task = {'id': 'transient-mcp-test', 'convId': 'client-supplied-job-label', '_userId': 7,
            'messages': [{'role': 'user', 'content': 'hope search'}], 'config': cfg}
    resolved = {'project_path': '', 'project_enabled': False, 'search_mode': 'off',
                'search_enabled': False, 'fetch_enabled': False, 'code_exec_enabled': False,
                'browser_enabled': False, 'desktop_enabled': False, 'image_gen_enabled': False}
    yield cfg, task, resolved, accesses
    tool_search.clear_tool_search_caches()


@pytest.mark.parametrize('runtime_flag', ['_inline_messages', '_transientRuntime'])
def test_headless_root_uses_ephemeral_mcp_scope_without_reading_conversation(assembly, runtime_flag):
    cfg, task, resolved, accesses = assembly
    task[runtime_flag] = True
    assemble_round_tools(cfg, task, resolved)
    assert not accesses
    assert cfg['_mcpSelectionScopeId'] == 'task:' + task['id']
    assert cfg['_mcpAllowedToolCatalog']
    wire = tool_search.frozen_wire_definitions(cfg['_mcpSelectionScopeId'])
    assert wire and any((tool.get('function') or {}).get('name', '').startswith('mcp__') for tool in wire)


def test_storage_free_direct_assembly_ignores_a_supplied_conversation_label(assembly):
    cfg, task, resolved, accesses = assembly
    cfg['_storageFreeRuntime'] = True
    _assemble_tool_list(cfg, '', False, task['id'], 'off', False,
                        False, False, False, False,
                        messages=task['messages'], conv_id=task['convId'])
    assert not accesses
    assert cfg['_mcpSelectionScopeId'] == 'task:' + task['id']
    assert cfg['_mcpAllowedToolCatalog']


def test_durable_ui_missing_conversation_still_refuses_instead_of_downgrading(assembly):
    cfg, task, resolved, accesses = assembly
    with pytest.raises(MCPDeclarationError, match='missing or not owned'):
        assemble_round_tools(cfg, task, resolved)
    assert accesses == [('conversation.get', {'conv_id': task['convId'], 'user_id': 7, 'derive_messages': False})]
    assert cfg['_mcpSelectionScopeId'] == 'conversation:7:' + task['convId']


def test_reused_headless_job_label_cannot_reuse_another_tasks_frozen_wire(assembly, monkeypatch):
    cfg, task, resolved, accesses = assembly
    task['_inline_messages'] = True
    assemble_round_tools(cfg, task, resolved)
    first_scope = cfg['_mcpSelectionScopeId']
    first_wire = tool_search.frozen_wire_definitions(first_scope)
    bridge(monkeypatch, _large_snapshot()[2:4])
    task['id'] += '-next-request'
    assemble_round_tools(cfg, task, resolved)
    assert not accesses
    assert cfg['_mcpSelectionScopeId'] != first_scope
    assert tool_search.frozen_wire_definitions(first_scope) == first_wire
    assert tool_search.frozen_wire_definitions(cfg['_mcpSelectionScopeId']) != first_wire


def test_headless_root_reaches_the_provider_and_terminal_delivery_with_mcp_enabled(assembly, invocation_runtime, monkeypatch):
    from lib.tasks_pkg.orchestrator._run import run_task
    from lib.tasks_pkg.manager import _stream
    from lib.llm.stream_result import ProviderStreamResult
    from lib.turn_verdict import derive_task_verdict, TurnStatus
    _, _, _, accesses = assembly
    parent = invocation_runtime.parent
    parent['convId'] = 'client-supplied-job-label'
    parent['config'].update(mcpEnabled=True, memoryEnabled=False, preferencesEnabled=False,
                            schedulerEnabled=False, fetchEnabled=False, browserEnabled=False,
                            desktopEnabled=False, codeExecEnabled=False, imageGenEnabled=False)
    requests = []
    def provider(body, **kwargs):
        requests.append(body)
        assert kwargs['owner_user_id'] == parent['_userId']
        kwargs['on_content']('Completed the caller-owned headless request.')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Completed the caller-owned headless request.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr(_stream, 'dispatch_stream', provider)
    run_task(parent)
    assert derive_task_verdict(parent).status is TurnStatus.COMPLETED
    assert len(requests) == 1 and not accesses
    assert parent['config']['_mcpSelectionScopeId'] == 'task:' + parent['id']
    assert parent['usage']['prompt_tokens'] == 20
