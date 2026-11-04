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


def prepared(monkeypatch, role, mode='local', policy_updates=None):
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
# ``auto`` and ``native`` are retired values a stored child policy may still carry.
@pytest.mark.parametrize('mode', ['auto', 'local', 'off', 'native'])
def test_roles_share_admission_without_losing_provider_candidates(monkeypatch, role, mode):
    task, candidates, authority = prepared(monkeypatch, role, mode)
    expected = names(authority) - {'mcp__fixture__hidden'}
    assert names(candidates) == expected
    assert names(task['_executable_tool_catalog']) == names(authority)
    discovered = mode != 'off'
    # Admission estimates the same surface the wire boundary will send.
    assert task['_toolSearchMode'] == ('local' if discovered else 'off')
    if discovered:
        assert names(task['_tool_schema']) == {'read_files', 'search_tools', 'execute_tools'}
        assert tool_schema_tokens(task['_tool_schema']) < tool_schema_tokens(authority) / 10
    else:
        assert names(task['_tool_schema']) == expected

    def projected(protocol, base_url):
        body = {'model': task['config']['model'], 'tools': deepcopy(candidates)}
        attach_tool_request_metadata(task, body, [], candidates, model=body['model'], round_num=0, tid='role')
        project_tool_wire(body, api_protocol=protocol, base_url=base_url)
        return body

    # An official provider host gets the same local surface as any gateway.
    for protocol, base_url in (('openai', 'https://fixture.invalid'),
                               ('anthropic', 'https://api.anthropic.com')):
        body = projected(protocol, base_url)
        assert body['_resolved_tool_search_backend'] == ('local' if discovered else 'full')
        if discovered:
            assert names(body['tools']) == {'read_files', 'search_tools', 'execute_tools'}
        assert 'mcp__fixture__hidden' not in names(body['tools'])


def test_parent_snapshot_retains_discovery_candidates_after_admission_projection(monkeypatch):
    from lib.tasks_pkg.child_invocation import delegated_tool_discovery_policy, snapshot_conversation_child_policy
    task, candidates, authority = prepared(monkeypatch, 'chat')
    policy = delegated_tool_discovery_policy(task, authority)
    assert set(policy['_toolInitialVisibleNames']) == names(candidates)
    task.update(_userId=7, _transientRuntime=True)
    snapshot = snapshot_conversation_child_policy(task, model=task['config']['model'], tools=candidates)
    assert set(snapshot['conversation_tool_policy']['_toolInitialVisibleNames']) == names(candidates)


@pytest.mark.parametrize('mode', ['auto', 'local', 'off', 'native'])
def test_exactly_delegated_deferred_tool_is_direct_for_the_child(monkeypatch, mode):
    """A spawn allowlist names exact tools. A tool the parent reached only
    through discovery must be callable by the child, not hidden behind a
    discovery surface the narrowed child does not hold."""
    from lib.tasks_pkg.child_invocation import delegated_tool_discovery_policy
    parent, _candidates, _authority = prepared(monkeypatch, 'chat', mode)
    delegated = [tool('read_files'), tool('mcp__fixture__hidden')]

    def child_wire(policy):
        config = {'model': 'claude-sonnet-4-6',
                  'tools': {'toolSearch': mode, 'programmaticCalling': 'off'}}
        child = {'id': 'child', 'messages': [], 'config': config,
                 '_invocationPolicy': InvocationPolicy.child('parent'),
                 '_invocationTools': deepcopy(delegated),
                 '_invocationToolDiscoveryPolicy': {**policy, '_toolSearchMode': mode}}
        mcfg = dict(project_path='', project_enabled=False, search_mode='off',
                    search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                    browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
        assembly.assemble_round_tools(config, child, mcfg)
        return names(child['_tool_schema'])

    inherited = delegated_tool_discovery_policy(parent, delegated)
    assert inherited['_requiredDirectToolNames'] == ['read_files']
    assert 'mcp__fixture__hidden' not in child_wire(inherited)

    exact = delegated_tool_discovery_policy(
        parent, delegated, direct_names=['mcp__fixture__hidden', 'foreign'])
    assert sorted(exact['_requiredDirectToolNames']) == ['mcp__fixture__hidden', 'read_files']
    assert {'read_files', 'mcp__fixture__hidden'} <= child_wire(exact)


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
