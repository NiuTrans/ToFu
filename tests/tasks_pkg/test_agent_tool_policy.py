"""All invocation roles share startup admission and provider discovery policy."""
from copy import deepcopy

import pytest

from lib.llm.tool_wire import project_tool_wire
from lib.tasks_pkg.invocation import InvocationPolicy
from lib.tasks_pkg.orchestrator import _tool_assembly_prep as assembly
from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.gateway import tool_schema_tokens

pytestmark = pytest.mark.unit


def tool(name, description='fixture'):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': {}}}}


def names(schemas):
    return {schema['function']['name'] for schema in schemas}


def prepared(monkeypatch, role, mode='auto', policy_updates=None):
    catalog = [tool('read_files')] + [tool(f'deferred_{i}', 'documentation ' * 1000)
                                    for i in range(32)] + [tool('mcp__fixture__hidden')]
    policy = {
        '_executableToolCatalog': catalog,
        '_toolContractDocumentsByName': compile_execution_contract_documents(catalog),
        '_toolDiscoveryPolicyByName': {name: 'searchable' for name in names(catalog)},
        '_requiredDirectToolNames': ['read_files'],
        '_toolInitialVisibleNames': [schema['function']['name'] for schema in catalog[:-1]],
        '_toolSearchMode': mode,
    }
    policy.update(policy_updates or {})
    config = {'model': 'claude-sonnet-4-6', 'tools': {'toolSearch': mode, 'programmaticCalling': 'off'}}
    task = {'id': 'role-policy', 'messages': [], 'config': config}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off', search_enabled=False,
                fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
                desktop_enabled=False, image_gen_enabled=False)
    if role in ('delegated', 'conversation'):
        task.update(_invocationPolicy=InvocationPolicy.child('parent'), _invocationTools=catalog)
        if role == 'conversation':
            task['_invocationConversationToolPolicy'] = policy
        else:
            task['_invocationToolDiscoveryPolicy'] = policy
    else:
        # Registry/role adapters own only capability selection, never projection.
        config.update(policy)
        if role == 'coordinator':
            config['evolution'] = {'stage': 'coordinator'}
        monkeypatch.setattr(assembly, '_assemble_tool_list', lambda *a, **kw: (catalog, True))
    candidates, enabled = assembly.assemble_round_tools(config, task, mcfg)
    assert enabled
    return task, candidates, catalog


@pytest.mark.parametrize('role', ['chat', 'coordinator', 'conversation', 'delegated'])
@pytest.mark.parametrize('mode', ['auto', 'local', 'off', 'native'])
def test_roles_share_admission_without_losing_provider_candidates(monkeypatch, role, mode):
    task, candidates, authority = prepared(monkeypatch, role, mode)
    expected = names(authority) - {'mcp__fixture__hidden'}
    assert names(candidates) == expected
    assert names(task['_executable_tool_catalog']) == names(authority)
    if mode in ('auto', 'local'):
        assert names(task['_tool_schema']) == {'read_files', 'search_tools', 'execute_tools'}
        assert tool_schema_tokens(task['_tool_schema']) < tool_schema_tokens(authority) / 10
    else:
        assert names(task['_tool_schema']) == expected

    def projected(protocol, base_url):
        body = {'model': task['config']['model'], 'tools': deepcopy(candidates)}
        attach_tool_request_metadata(task, body, [], candidates, model=body['model'], round_num=0, tid='role')
        project_tool_wire(body, api_protocol=protocol, base_url=base_url)
        return body

    local = projected('openai', 'https://fixture.invalid')
    if mode in ('auto', 'local'):
        assert names(local['tools']) == {'read_files', 'search_tools', 'execute_tools'}
    native = projected('anthropic', 'https://api.anthropic.com')
    if mode in ('auto', 'native'):
        assert native['_resolved_tool_search_backend'] == 'native_anthropic'
        assert names(native['tools']) == expected
    assert 'mcp__fixture__hidden' not in names(native['tools'])


def test_parent_snapshot_retains_discovery_candidates_after_admission_projection(monkeypatch):
    from lib.tasks_pkg.child_invocation import delegated_tool_discovery_policy, snapshot_conversation_child_policy
    task, candidates, authority = prepared(monkeypatch, 'chat')
    policy = delegated_tool_discovery_policy(task, authority)
    assert set(policy['_toolInitialVisibleNames']) == names(candidates)
    task.update(_userId=7, _transientRuntime=True)
    snapshot = snapshot_conversation_child_policy(task, model=task['config']['model'], tools=candidates)
    assert set(snapshot['conversation_tool_policy']['_toolInitialVisibleNames']) == names(candidates)


@pytest.mark.parametrize('role', ['chat', 'coordinator', 'conversation', 'delegated'])
def test_pins_and_required_tools_survive_visibility_without_granting_unknown_names(monkeypatch, role):
    task, candidates, authority = prepared(monkeypatch, role, policy_updates={
        '_toolInitialVisibleNames': [],
        '_requiredDirectToolNames': ['read_files', 'foreign'],
        '_frontendSelectedToolNames': ['deferred_1', 'foreign'],
    })
    assert names(candidates) == {'read_files', 'deferred_1'}
    body = {'model': task['config']['model'], 'tools': candidates}
    attach_tool_request_metadata(task, body, [], candidates, model=body['model'], round_num=0, tid='pins')
    project_tool_wire(body, api_protocol='openai', base_url='https://fixture.invalid')
    assert names(body['tools']) == {'read_files', 'deferred_1', 'search_tools', 'execute_tools'}
    assert 'foreign' not in names(task['_executable_tool_catalog'])
    assert names(task['_executable_tool_catalog']) == names(authority)
