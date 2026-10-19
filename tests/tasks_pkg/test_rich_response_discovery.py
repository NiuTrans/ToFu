"""Rich publication crosses real registry/search/policy and ordinary settlement."""
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.system_prompt_base import build_static_prompt
from lib.tools.rich_response import NAMES, build_tools

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('profile', ['lean', 'full'])
def test_native_rich_discovery_requires_searchable_catalog(profile):
    kwargs = dict(cwd='/workspace', is_git=False, model='kimi-k3',
                  tool_names={'read_files'}, include_date=False, profile=profile)
    hidden = build_static_prompt(**kwargs, tool_search_available=False)
    searchable = build_static_prompt(**kwargs, tool_search_available=True)
    assert 'rich responses' not in hidden
    assert 'rich responses' in searchable


def test_timing_diagram_tool_exposes_authoritative_schema_and_inline_route():
    ctx = SimpleNamespace(durable_state_available=True, conv_id='owned', owner_user_id=7)
    tools = {tool['function']['name']: tool['function'] for tool in build_tools(ctx)}
    rich = tools['publish_rich_response']
    assert 'timing diagram' in rich['description']
    assert 'tofu-ui' in rich['description']
    families = rich['parameters']['properties']['payload']['properties']['blocks']['items']['oneOf']
    assert any(item['properties']['type']['const'] == 'timeline' for item in families)
    assert 'publish_response_file' in tools
    ctx.durable_state_available = False
    assert build_tools(ctx) == []


def context(**overrides):
    from lib.tools.registry import ToolContext
    fields = dict(cfg={'memoryEnabled': False, 'mcpEnabled': False},
                  task_id='rich-publication-test', project_path='', project_enabled=False,
                  search_mode='off', search_enabled=False, fetch_enabled=False,
                  code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
                  enabled_plugins=set(), owner_user_id=7, conv_id='rich-owned',
                  messages=[{'role': 'user', 'content': 'Explain this.'}])
    fields.update(overrides)
    return ToolContext(**fields)


@pytest.mark.parametrize('exposure', ['routed', 'full'])
def test_live_catalog_search_and_dispatch_contain_publication(exposure):
    from lib.tools.registry import assemble_tool_list, all_specs, sync_spec_handlers
    from lib.tools.gateway import search_executable_catalog
    ctx = context(cfg={'memoryEnabled': False, 'mcpEnabled': False,
                       'tools.nativeExposure': exposure})
    surface, _ = assemble_tool_list(ctx)
    by_name = {item['function']['name']: item for item in ctx.executable_tool_catalog}
    assert NAMES <= by_name.keys()
    assert NAMES <= ctx.tool_contract_documents_by_name.keys()
    for name in NAMES:
        result = search_executable_catalog(ctx.executable_tool_catalog, '+' + name,
                                          namespace_by_name=ctx.tool_namespace_by_name,
                                          contract_documents_by_name=ctx.tool_contract_documents_by_name)
        assert result['items'][0]['name'] == name
        assert result['items'][0]['permission'] == 'write'
    if exposure == 'full':
        assert NAMES <= {item['function']['name'] for item in surface}
    from lib.tasks_pkg.executor import tool_registry
    sync_spec_handlers(tool_registry)
    assert NAMES <= {name for name, *_ in tool_registry.list_tools()}
    spec = next(spec for spec in all_specs() if spec.key == 'rich_response')
    assert spec.write_tools == NAMES
    assert spec.cacheable_tools == frozenset()

    from lib.tools.gateway import local_wire_tools
    projected = local_wire_tools(surface, discovery_policy_by_name=ctx.discovery_policy_by_name)
    assert not NAMES.intersection(item['function']['name'] for item in projected)
    assert NAMES <= by_name.keys()  # Deferral never removes executable authority.


@pytest.mark.parametrize('overrides', [
    {'owner_user_id': 0}, {'conv_id': ''},
    {'cfg': {'_storageFreeRuntime': True, 'memoryEnabled': False, 'mcpEnabled': False}},
])
def test_missing_durable_identity_does_not_grant_publication(overrides):
    from lib.tools.registry import assemble_tool_list
    ctx = context(**overrides)
    surface, _ = assemble_tool_list(ctx)
    assert not NAMES.intersection(item['function']['name'] for item in surface)
    assert not NAMES.intersection(item['function']['name'] for item in ctx.executable_tool_catalog)


def test_routed_project_delivery_exposes_native_publishers():
    from lib.tools.registry import assemble_tool_list
    ctx = context(messages=[{'role': 'user', 'content': 'Download this prototype file.'}])
    surface, _ = assemble_tool_list(ctx)
    assert NAMES <= {item['function']['name'] for item in surface}


def test_unrelated_project_turn_keeps_publication_discoverable_without_full_wire_cost(tmp_path):
    from lib.tools.registry import assemble_tool_list
    ctx = context(project_path=str(tmp_path), project_enabled=True,
                  conv_id='unrelated-project-' + tmp_path.name,
                  messages=[{'role': 'user', 'content': 'Fix a Python function in the repository.'}])
    surface, _ = assemble_tool_list(ctx)
    assert not NAMES.intersection(item['function']['name'] for item in surface)
    assert NAMES <= {item['function']['name'] for item in ctx.executable_tool_catalog}


def test_plan_policy_removes_publishers_from_both_surface_and_catalog():
    from lib.tasks_pkg.plan_mode import plan_mode_filter_tool_schemas
    from lib.tools.registry import assemble_tool_list, all_specs
    ctx = context()
    surface, _ = assemble_tool_list(ctx)
    writes = set().union(*(spec.write_tools for spec in all_specs()))
    for lane in (surface, ctx.executable_tool_catalog):
        kept, _ = plan_mode_filter_tool_schemas(lane, writes, owner_user_id=7)
        assert not NAMES.intersection(item['function']['name'] for item in kept)


def test_shared_inventory_and_live_publication_declaration_agree():
    from lib.tools.registry import all_specs
    from lib.tools.registry._builtin_specs import build_builtin_specs
    live = next(spec for spec in all_specs() if spec.key == 'rich_response')
    declared = next(spec for spec in build_builtin_specs() if spec.key == 'rich_response')
    for field in ('build', 'provides', 'write_tools', 'handler', 'search_hints', 'native_route_groups'):
        assert getattr(live, field) == getattr(declared, field)


def test_file_handler_settles_with_artifact_receipt_and_event(monkeypatch):
    from lib.tools import rich_response as tools
    import lib.tasks_pkg.executor as executor
    from lib.artifacts import core, events
    receipt = {'id': 'published-id', 'format': 'html', 'filename': 'preview.html',
               'sizeBytes': 100, 'sha256': 'verified',
               'artifact': {'id': 'published-id', 'format': 'html'}}
    monkeypatch.setattr(tools, 'publish_file_for_task', lambda *args: receipt)
    monkeypatch.setattr(core, 'get_artifact', lambda artifact_id: {'id': artifact_id})
    emitted, settled = [], []
    monkeypatch.setattr(events, 'emit_artifact_event', lambda task, artifact: emitted.append(artifact))
    monkeypatch.setattr(executor, '_finalize_tool_round', lambda *args, **kwargs: settled.append((args, kwargs)))
    result = tools.handle({'convId': 'owned'}, None, 'publish_response_file', 'call-file',
                          {'path': 'preview.html'}, 0, {}, {}, '/workspace', True)
    assert result[0] == 'call-file'
    assert 'published-id' in result[1]
    assert len(emitted) == len(settled) == 1
    assert settled[0][0][3][0]['artifactId'] == receipt['id']


def test_file_handler_refusal_settles_error_without_artifact(monkeypatch):
    from lib.tools import rich_response as tools
    import lib.tasks_pkg.executor as executor
    def refuse(*args):
        raise PermissionError('Unavailable')
    monkeypatch.setattr(tools, 'publish_file_for_task', refuse)
    settled = []
    monkeypatch.setattr(executor, '_finalize_tool_round', lambda *args, **kwargs: settled.append((args, kwargs)))
    result = tools.handle({'convId': 'owned'}, None, 'publish_response_file', 'call-file',
                          {'path': '/private/file'}, 0, {}, {}, '/workspace', True)
    assert result[1].startswith('Error:')
    assert len(settled) == 1
    assert settled[0][1]['status'] == 'error'
    assert 'artifactId' not in settled[0][0][3][0]


def test_attachment_lifecycle_event_points_to_authorized_download(monkeypatch):
    from lib.artifacts.events import emit_artifact_event
    import lib.tasks_pkg.manager as manager
    emitted = []
    monkeypatch.setattr(manager, 'append_event', lambda task, event: emitted.append(event))
    emit_artifact_event({'id': 'task'}, {'id': 'file-id', 'format': 'file', 'conv_id': 'owned'})
    assert len(emitted) == 1
    assert emitted[0]['type'] == 'artifact'
    assert emitted[0]['url'] == '/api/artifacts/file-id/download'


def test_schema_projection_is_request_owned_despite_lazy_immutable_cache():
    ctx = SimpleNamespace(durable_state_available=True, conv_id='owned', owner_user_id=7)
    first = {item['function']['name']: item for item in build_tools(ctx)}
    first['publish_rich_response']['function']['parameters']['properties']['payload']['properties']['title']['maxLength'] = 1
    second = {item['function']['name']: item for item in build_tools(ctx)}
    assert second['publish_rich_response']['function']['parameters']['properties']['payload']['properties']['title']['maxLength'] == 200
