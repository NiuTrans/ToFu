"""Conversation wire durability is independent of caches and body replay."""
import json
from types import SimpleNamespace

import pytest

from lib.mcp import tool_search
from lib.storage.mcp_declarations import MCPDeclarationError, MCPDeclarationStore
from lib.tools.registry import _build
from tests.mcp_protocol.test_mcp_tool_search import _large_snapshot, _definition

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_caches():
    tool_search.clear_tool_search_caches()
    yield
    tool_search.clear_tool_search_caches()


def context():
    return SimpleNamespace(cfg={'mcpActiveToolLimit': 2}, tid='test', task_id='test',
                           conv_id='durable', owner_user_id=7,
                           messages=[{'role': 'user', 'content': 'hope search'}])


def bridge(monkeypatch, snapshot=None, *, connected=True):
    import lib.mcp
    rows = _large_snapshot() if snapshot is None else snapshot
    value = SimpleNamespace(connected=connected, server_count=2,
        get_openai_tool_defs=lambda: [row['openai_def'] for row in rows],
        get_tool_catalog_snapshot=lambda: rows)
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: value)
    return value


@pytest.mark.parametrize('cold', ['clear', 'churn', 'ttl', 'pressure'])
def test_full_ordered_wire_survives_cold_state(cold, monkeypatch):
    ctx = context()
    bridge(monkeypatch)
    initial = _build._build_mcp(ctx)
    initial_bytes = json.dumps(initial)
    if cold == 'clear':
        tool_search.clear_tool_search_caches()
    elif cold == 'churn':
        for index in range(4097):
            tool_search.record_mcp_tool_used(f'unrelated-task-{index}', 'mcp__other__read')
    elif cold == 'pressure':
        tool_search.reclaim_tool_search_indexes()
    else:
        tool_search._prune_states(tool_search.time.monotonic() + tool_search._STATE_TTL_SECONDS + 1)
    bridge(monkeypatch, [_large_snapshot()[0]])
    ctx.messages = [{'role': 'user', 'content': 'different latest intent'}]
    assert json.dumps(_build._build_mcp(ctx)) == initial_bytes
    assert all('wire' not in state for state in tool_search._SELECTION_STATE.values())


@pytest.mark.parametrize('connected', [False, True])
def test_disconnect_or_empty_catalog_keeps_wire_not_authority(monkeypatch, connected):
    ctx = context()
    bridge(monkeypatch)
    initial = _build._build_mcp(ctx)
    tool_search.clear_tool_search_caches()
    bridge(monkeypatch, [], connected=connected)
    assert _build._build_mcp(ctx) == initial
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []


def test_explicit_empty_survives_clear_and_late_connection(monkeypatch):
    ctx = context()
    bridge(monkeypatch, connected=False)
    assert _build._build_mcp(ctx) == []
    tool_search.clear_tool_search_caches()
    bridge(monkeypatch)
    assert _build._build_mcp(ctx) == []
    assert ctx.cfg['_mcpAllowedToolCatalog']


def test_restored_wire_bypasses_search_failure_and_initial_failure_is_visible(monkeypatch):
    ctx = context()
    bridge(monkeypatch)
    initial = _build._build_mcp(ctx)
    tool_search.clear_tool_search_caches()
    def fail(*args, **kwargs):
        raise ValueError('broken search')
    monkeypatch.setattr(tool_search, 'select_active_mcp_tools', fail)
    assert _build._build_mcp(ctx) == initial
    ctx.conv_id = 'new-conversation'
    with pytest.raises(MCPDeclarationError, match='selection failed'):
        _build._build_mcp(ctx)
    assert MCPDeclarationStore().get(user_id=7, conversation_id=ctx.conv_id) is None


def test_storage_outage_does_not_fall_back_to_reselection(monkeypatch, declaration_authority):
    ctx = context()
    bridge(monkeypatch)
    _build._build_mcp(ctx)
    def fail(*args):
        raise OSError('storage unavailable')
    monkeypatch.setattr(declaration_authority, 'query', fail)
    with pytest.raises(MCPDeclarationError, match='read failed'):
        _build._build_mcp(ctx)


def test_owner_isolation_reset_and_legacy_body_cannot_resurrect(monkeypatch):
    ctx = context()
    bridge(monkeypatch)
    original = _build._build_mcp(ctx)
    store = MCPDeclarationStore()
    assert store.get(user_id=8, conversation_id=ctx.conv_id) is None
    store.freeze([], user_id=8, conversation_id=ctx.conv_id)
    assert store.get(user_id=7, conversation_id=ctx.conv_id) == original
    store.reset(user_id=7, conversation_id=ctx.conv_id)
    from lib.tasks_pkg import model_replay
    config = {}
    assert model_replay.restore_prefix([], config, conversation_id=ctx.conv_id, user_id=7) == ([], 0)
    assert store.get(user_id=7, conversation_id=ctx.conv_id) is None
    bridge(monkeypatch, [], connected=False)
    assert _build._build_mcp(ctx) == []
    store.reset(user_id=7, conversation_id=ctx.conv_id)
    bridge(monkeypatch)
    assert _build._build_mcp(ctx) == original


def registry_context(cfg):
    from lib.tools.registry import _spec
    return _spec.ToolContext(cfg=cfg, task_id='test', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False)


def test_assembly_propagates_declaration_errors_and_never_grants_old_wire(monkeypatch):
    from lib.tools.registry import _spec
    wire = [_definition('gone', 'read')]
    spec = _spec.ToolSpec('mcp', lambda ctx: wire, phase='base')
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [spec])
    ctx = registry_context({'_mcpAllowedToolCatalog': []})
    tools, _ = _spec.assemble_tool_list(ctx)
    assert tools == wire
    assert ctx.executable_tool_catalog == []
    def fail(ctx):
        raise MCPDeclarationError('declaration outage')
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [_spec.ToolSpec('mcp', fail, phase='base')])
    with pytest.raises(MCPDeclarationError, match='outage'):
        _spec.assemble_tool_list(registry_context({}))


def test_complete_resident_observations_and_durable_wire_footprint(monkeypatch, declaration_authority):
    from lib.storage.mcp_declarations import MAX_DECLARATION_BYTES, SETTING_KEY
    rows = _large_snapshot()
    for index in range(100):
        scope = tool_search.mcp_selection_scope_id(task_id='', conv_id=f'footprint-{index}', owner_user_id=7)
        tool_search.select_active_mcp_tools(rows, task_id='', selection_scope_id=scope, query='hope', limit=4)
    assert len(tool_search._SELECTION_STATE) == 100
    assert all('wire' not in state for state in tool_search._SELECTION_STATE.values())
    sizes = [len(settings[SETTING_KEY]['wireJson'].encode('utf-8'))
             for settings in declaration_authority.settings.values()]
    assert len(sizes) == 100 and max(sizes) <= MAX_DECLARATION_BYTES
    print(f'footprint conversations=100 resident_states=100 resident_wires=0 '
          f'wire_bytes_each={max(sizes)} total_wire_bytes={sum(sizes)}')


@pytest.mark.parametrize('body', [None, {'version': 0}, {'version': 1, 'policy': 'obsolete'}])
def test_body_replay_failure_does_not_erase_durable_wire(monkeypatch, body):
    from lib.tasks_pkg import model_replay
    ctx = context()
    bridge(monkeypatch)
    initial = _build._build_mcp(ctx)
    monkeypatch.setattr(model_replay.ModelContinuationStore, 'get', lambda *args, **kwargs: body)
    config = {'_modelReplayPolicy': 'current'}
    rows = [{'turnId': 'old', 'laneId': 'main', 'projection': {'content': 'done'}},
            {'turnId': 'target', 'laneId': 'main', 'projection': {}}]
    assert model_replay.restore_prefix(rows, config, conversation_id=ctx.conv_id, user_id=7) == ([], 0)
    assert config['_modelReplayStatus'] in {'checkpoint_missing', 'policy_changed'}
    tool_search.clear_tool_search_caches()
    bridge(monkeypatch, [], connected=False)
    assert _build._build_mcp(ctx) == initial


def test_legacy_valid_checkpoint_cannot_resurrect_reset_wire(monkeypatch):
    from lib.tasks_pkg import model_replay
    config = {'model': 'test'}
    row = {'turnId': 'turn', 'status': 'completed', 'projection': {}}
    body = {'version': 1, 'policy': model_replay.policy_witness(config),
            'turns': [model_replay.turn_witness(row)],
            'messages': [{'role': 'assistant', 'content': 'done'}],
            'mcpWire': [_definition('obsolete', 'read')]}
    monkeypatch.setattr(model_replay.ModelContinuationStore, 'get', lambda *args, **kwargs: body)
    store = MCPDeclarationStore()
    store.freeze([_definition('old', 'read')], user_id=7, conversation_id='durable')
    store.reset(user_id=7, conversation_id='durable')
    assert model_replay.restore_prefix([row, {'turnId': 'target', 'projection': {}}], config,
                                      conversation_id='durable', user_id=7)[1] == 1
    assert store.get(user_id=7, conversation_id='durable') is None


def test_turn_assembly_does_not_hide_declaration_failure(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep
    def fail(*args, **kwargs):
        raise MCPDeclarationError('declaration authority unavailable')
    monkeypatch.setattr(prep, '_assemble_tool_list', fail)
    task = {'id': 'test', 'messages': [], '_executable_tool_catalog': [_definition('old', 'read')]}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off',
                search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
    with pytest.raises(MCPDeclarationError):
        prep.assemble_round_tools({}, task, mcfg)
    assert not task.get('_executable_tool_catalog')


@pytest.fixture(params=['get_openai_tool_defs', 'get_tool_catalog_projection'],
                ids=['early', 'partial_catalog'])
def catalog_outage(monkeypatch, request):
    def inject(live):
        def fail():
            raise OSError('catalog unavailable')
        monkeypatch.setattr(live, request.param, fail, raising=False)
    return inject


@pytest.mark.parametrize('exposure', [
    'delegate', 'inline', 'all', 'full', 'progressive', 'wrapper', 'legacy',
])
def test_explicit_mode_catalog_outage_keeps_baseline_without_mcp_grants(
        monkeypatch, catalog_outage, exposure, declaration_authority):
    from lib.tools.registry import _spec

    ctx = registry_context({
        'mcpToolExposure': exposure, 'tools.nativeExposure': 'full',
    })
    ctx.owner_user_id = 7
    ctx.conv_id = 'explicit-outage'
    live = bridge(monkeypatch)
    _build._build_mcp(ctx)
    assert ctx.cfg['_mcpAllowedToolCatalog']
    assert ctx.cfg['_mcpToolSearchTextByName']

    baseline = {'type': 'function', 'function': {
        'name': 'local_tool', 'description': 'Available without MCP',
        'parameters': {'type': 'object', 'properties': {}},
    }}
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [
        _spec.ToolSpec('local', lambda ctx: [baseline], phase='base'),
        _spec.ToolSpec('mcp', _build._build_mcp, phase='base'),
    ])
    catalog_outage(live)

    tools, _ = _spec.assemble_tool_list(ctx)

    assert tools == [baseline]
    assert ctx.executable_tool_catalog == [baseline]
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []
    assert ctx.cfg['_mcpToolSearchTextByName'] == {}
    assert ctx.cfg['_mcpActiveToolNames'] == []
    assert declaration_authority.settings == {}


def test_catalog_exception_retains_wire_without_execution_grants(
        monkeypatch, catalog_outage):
    ctx = context()
    live = bridge(monkeypatch)
    initial = _build._build_mcp(ctx)
    catalog_outage(live)
    tool_search.clear_tool_search_caches()
    assert _build._build_mcp(ctx) == initial
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []
    assert ctx.cfg['_mcpToolSearchTextByName'] == {}
    assert ctx.cfg['_mcpActiveToolNames'] == [
        tool['function']['name'] for tool in initial]
    ctx.conv_id = 'new-catalog-outage'
    with pytest.raises(MCPDeclarationError, match='catalog assembly failed'):
        _build._build_mcp(ctx)
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []
    assert ctx.cfg['_mcpToolSearchTextByName'] == {}
    assert ctx.cfg['_mcpActiveToolNames'] == []
    assert MCPDeclarationStore().get(user_id=7, conversation_id=ctx.conv_id) is None


@pytest.mark.parametrize('loss', ['disconnect', 'revoke', 'outage'])
def test_declared_mcp_loss_is_unavailable_not_hallucinated_and_recovers(monkeypatch, loss):
    import threading
    from lib.tools.registry import _spec
    from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    name = 'mcp__llm__experiment_eval_result'
    schema = _definition('llm', 'experiment_eval_result')
    schema['function']['parameters'] = {
        'type': 'object', 'properties': {'experiment_id_or_name_or_url': {'type': 'string'}},
        'required': ['experiment_id_or_name_or_url'], 'additionalProperties': False}
    rows = [{'openai_def': schema, 'namespaced_name': name, 'meta': {}}]
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [
        _spec.ToolSpec('mcp', _build._build_mcp, phase='base')])
    cfg = {'mcpToolExposure': 'auto'}
    def assemble():
        ctx = registry_context(cfg)
        ctx.owner_user_id, ctx.conv_id = 7, 'unavailable-regression'
        wire, _ = _spec.assemble_tool_list(ctx)
        return wire, ctx

    bridge(monkeypatch, rows)
    initial, _ = assemble()
    assert initial == [schema]
    live = bridge(monkeypatch, [], connected=loss != 'disconnect')
    if loss == 'outage':
        def fail():
            raise OSError('discovery unavailable')
        live.get_openai_tool_defs = fail
    wire, ctx = assemble()
    assert wire == initial
    task = {'id': 'unavailable-test', 'convId': 'unavailable-regression',
            'model': 'test', 'events': [], 'events_lock': threading.Lock(),
            'toolRounds': [], 'aborted': False, 'config': {'autopilotEnabled': True},
            '_tool_schema': wire, '_executable_tool_catalog': ctx.executable_tool_catalog,
            '_toolContractDocumentsByName': ctx.tool_contract_documents_by_name}
    assert name not in _known_tool_names(task)
    def parse(called=name):
        return parse_tool_calls({'content': '', 'tool_calls': [{
            'id': 'mcp-call', 'type': 'function', 'function': {
                'name': called, 'arguments': '{"experiment_id_or_name_or_url":"331785"}'}}]},
            task, round_num=6, tool_round_num=len(task['toolRounds']), project_enabled=False)[0][0]

    for _ in range(5):
        parsed = parse()
        assert parsed[5]['status'] == 'rejected'
        assert parsed[5]['_rejected']['kind'] == 'tool_unavailable'
        assert parsed[5]['_rejected']['executable'] is False
        assert 'not a real tool' not in str(parsed[5])
        assert not task['aborted']
    assert name not in _known_tool_names(task)
    invented = parse('mcp__llm__invented')
    assert invented[5]['_rejected']['kind'] == 'hallucinated'

    bridge(monkeypatch, rows)
    restored, ctx = assemble()
    task.update(_tool_schema=restored, _executable_tool_catalog=ctx.executable_tool_catalog,
                _toolContractDocumentsByName=ctx.tool_contract_documents_by_name)
    assert restored == initial and name in _known_tool_names(task)
    accepted = parse()
    assert accepted[1] == name
    assert accepted[5].get('status') != 'rejected'


def test_root_request_refresh_recovers_startup_mcp_without_widening(monkeypatch):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools, refresh_root_mcp_authority)
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    rows = _large_snapshot()[:2]
    bridge(monkeypatch, rows)
    ctx = context()
    initial = _build._build_mcp(ctx)
    name = initial[0]['function']['name']
    bridge(monkeypatch, [], connected=False)
    cfg = {'userId': 7, 'model': 'test', 'mcpToolExposure': 'auto',
           'memoryEnabled': False, 'schedulerEnabled': False,
           'preferencesEnabled': False, 'plugins': []}
    task = {'id': 'root-startup', 'convId': ctx.conv_id, '_userId': 7,
            'messages': [], 'config': cfg}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off',
                search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
    wire, _ = assemble_round_tools(cfg, task, mcfg)

    admitted_wire = list(task['_tool_schema'])

    from lib.tools.gateway import local_wire_tools
    task['_toolDiscoveryPolicyByName'][name] = 'searchable'
    task['_toolSearchCatalogSize'] = 100
    task['_toolSearchableCount'] = 1
    def provider_wire():
        return local_wire_tools(task['_tool_schema'],
            discovery_policy_by_name=task['_toolDiscoveryPolicyByName'],
            discovery_catalog_size=task['_toolSearchCatalogSize'],
            searchable_count=task['_toolSearchableCount'], apply_schema_budget=False)
    frozen_provider_wire = provider_wire()
    assert name not in _known_tool_names(task)
    before = [tool for tool in task['_executable_tool_catalog']
              if not tool['function']['name'].startswith('mcp__')]
    bridge(monkeypatch, rows + _large_snapshot()[2:4])
    from lib.tasks_pkg.orchestrator import _round_request_prep as request_prep
    monkeypatch.setattr(request_prep.orchestrator_ports, 'build_request_body',
                        lambda model, messages, **kw: {'messages': messages, 'tools': kw['tools']})
    monkeypatch.setattr(request_prep, 'emit_messages_snapshot_event', lambda *a, **kw: None)
    _, body = request_prep.build_round_request(
        task, SimpleNamespace(model='test', preset=None, thinking_enabled=False), [], wire,
        round_num=6, tid=task['id'], thinking_depth=None, temperature=None,
        max_tokens=100, response_format=None)
    assert name in {tool['function']['name'] for tool in body['_executable_tool_catalog']}
    assert name in _known_tool_names(task)
    assert name in task['_toolContractDocumentsByName']
    assert [tool for tool in task['_executable_tool_catalog']
            if not tool['function']['name'].startswith('mcp__')] == before
    assert task['_tool_schema'] == admitted_wire
    new_names = {row['namespaced_name'] for row in _large_snapshot()[2:4]}
    assert not new_names.intersection(_known_tool_names(task))
    bridge(monkeypatch, [], connected=True)
    refresh_root_mcp_authority(task)
    assert name not in _known_tool_names(task)
    assert name not in task['_toolContractDocumentsByName']

    assert provider_wire() == frozen_provider_wire
    bridge(monkeypatch, rows)
    refresh_root_mcp_authority(task)
    assert name in _known_tool_names(task)
    cfg['mcpEnabled'] = False
    refresh_root_mcp_authority(task)
    assert name not in _known_tool_names(task)


def test_root_mcp_refresh_never_reassembles_child_authority(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep
    from lib.tasks_pkg.invocation import InvocationPolicy
    task = {'_invocationPolicy': InvocationPolicy.child(parent_task_id='parent'),
            '_rootToolAssemblyConfig': {}, '_rootMcpToolNames': ['mcp__llm__read'],
            '_tool_schema': [_definition('llm', 'read')], '_executable_tool_catalog': []}
    def forbidden(*args, **kwargs):
        pytest.fail('child must never reassemble root inventory')
    monkeypatch.setattr(prep, 'assemble_round_tools', forbidden)
    prep.refresh_root_mcp_authority(task)
    assert task['_executable_tool_catalog'] == []


def test_text_only_turn_clears_root_mcp_recovery(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep
    task = {'_rootToolAssemblyConfig': {}, '_rootMcpToolNames': ['mcp__llm__read']}
    prep._disable_tools_for_turn(task)
    def forbidden(*args, **kwargs):
        pytest.fail('text-only turn must not rebuild tool authority')
    monkeypatch.setattr(prep, 'assemble_round_tools', forbidden)
    prep.refresh_root_mcp_authority(task)
    assert task['_executable_tool_catalog'] == []
