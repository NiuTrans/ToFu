"""Default exposure policy for every built-in tool (executable inventory).

Each registered built-in and bundled host-owned tool name has one reviewed
default exposure: a small eager core, explicit lifecycle/direct/toggle/gateway
classes, and every domain, admin, approval, coordination or rare tool deferred
behind Tool Search. A new tool fails the inventory test until its exposure is
decided here and in docs/modules/tools_execution.md ("Default tool exposure").

Projection cases run the real registry through task assembly and the shared
provider boundary: local Tool Search defers searchable tools behind
``search_tools``; provider-native search marks them deferred; Tool Search
``off`` has no discovery path, so it never hides a declared-searchable tool.
"""
from copy import deepcopy

import pytest

from lib.tools.gateway import GATEWAY_TOOL_NAMES
from lib.tools.registry import all_specs

pytestmark = pytest.mark.unit

# Direct core: read/search/edit/shell, web, and tools that model-visible
# prompts, results or recovery pointers name (image/video refs, artifact
# continuation, interrupted-write inspection, memory accumulation, skill
# listing, delegation briefs). ``ask_human`` is direct only while its explicit
# composer toggle exposes it; coordinator tools are the Coordinator role's
# whole surface (router and channel tools join it only for the owner's Global
# Router, docs/ROUTER.md R1; watchdog tools only for the owner's Router
# Watchdog, R7) and evolution_source is the executing/reviewing stage source.
EAGER = frozenset({
    'read_files', 'inspect_image', 'inspect_video', 'search_files', 'find_files', 'edit_file',
    'write_file', 'run_command', 'web_search', 'fetch_url',
    'list_conversations', 'get_conversation', 'ask_human',
    'search_memories', 'create_memory', 'read_working_facts',
    'search_skills', 'load_skill', 'read_skill_resource',
    'inspect_tool_effect', 'read_tool_artifact', 'search_tool_artifact',
    'spawn_agents', 'get_agent_result', 'evolution_source',
    'await_project_workers', 'cancel_project_work', 'create_conversation',
    'delegate_project_work', 'dispatch_project_assignment',
    'finish_project_request', 'get_project_assignment', 'get_project_workers',
    'report_to_user', 'request_user_decision', 'review_project_work', 'update_task_progress',
    'get_router_projects', 'route_to_project', 'router_instruction_cancel',
    'router_instruction_create', 'router_instruction_list',
    'channel_configure_thread', 'channel_outbox', 'channel_outbox_approve',
    'channel_read', 'channel_send', 'channel_subscribe', 'channel_threads',
    'channel_unsubscribe',
    'watchdog_configure_review', 'watchdog_message', 'watchdog_notify_router',
    'watchdog_resolve_signals', 'watchdog_schedule_recheck', 'watchdog_snapshot',
})
# Background lifecycle controls are pinned whenever assembled.
PINNED = frozenset({'poll', 'kill'})
# Protocol completion requires one direct call (declared direct_wire_tools).
DIRECT = frozenset({
    'timer_create', 'await_device', 'evolution_coordinate', 'evolution_artifact',
    'evolution_review',
})
# Deferred unless the user's composer toggle pins the whole family.
TOGGLE_PINNED = frozenset({
    'desktop_clipboard', 'desktop_gui_action', 'desktop_list_files',
    'desktop_open_app', 'desktop_open_file', 'desktop_read_file',
    'desktop_run_command', 'desktop_screenshot', 'desktop_system_info',
    'desktop_write_file',
})
# Domain, admin, approval, coordination and rare tools.
DEFERRED = frozenset({
    'browser_check', 'browser_click', 'browser_close_tab', 'browser_devtools',
    'browser_execute_js', 'browser_fill_form', 'browser_get_cookies',
    'browser_get_history', 'browser_list_tabs', 'browser_menu_click',
    'browser_navigate', 'browser_press_key', 'browser_read_page',
    'browser_research_page', 'browser_screenshot', 'browser_scroll',
    'browser_select_option', 'browser_site', 'browser_type',
    'repo_map', 'browser_preview_page', 'browser_download_url_to_server',
    'integration_checkpoint', 'integration_submit', 'send_project_message',
    'get_project_messages', 'consider_project_message',
    'update_search_settings', 'search_knowledge',
    'update_memory', 'delete_memory', 'merge_memories',
    'request_skill_install', 'todo_write',
    'schedule_create', 'schedule_list', 'schedule_manage', 'timer_manage',
    'record_working_fact',
    'send_agent_message', 'get_agent_messages', 'set_approval_delegate',
    'get_exploration', 'manage_exploration',
    'evolution_teach', 'evolution_material', 'evolution_reindex',
    'evolution_explore', 'list_agents', 'manage_agents',
    'publish_response_asset', 'publish_response_file', 'publish_rich_response',
    # Bundled optional domains (host-owned plugins).
    'generate_image',
    'motion_video_check', 'motion_video_concat', 'motion_video_env_check',
    'motion_video_mux', 'motion_video_narrate', 'motion_video_probe',
    'motion_video_render', 'motion_video_storyboard_check',
    'produce_report', 'produce_research', 'produce_slides', 'produce_video',
    'edit_slides', 'edit_video', 'patch_slides', 'restyle_slides',
    'revert_slides', 'revert_video',
    'paper_build', 'paper_import_arxiv', 'paper_layout', 'paper_propose_edits',
    'paper_citation_audit', 'paper_cite_insert', 'paper_cite_search',
    'paper_check', 'paper_review', 'paper_scaffold',
    'local_serve_deploy', 'local_serve_list', 'local_serve_prepare',
    'local_serve_recommend', 'local_serve_remove', 'local_serve_status',
    'local_serve_stop',
})
# Injected at the provider boundary by the resolved Tool Search backend.
GATEWAY = frozenset(GATEWAY_TOOL_NAMES)
# Dispatch-only names that no current builder emits.
COMPATIBILITY = frozenset({
    'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents',
    'check_background_command', 'kill_background_command', 'grep_search',
    'await_task', 'await_agents',
    'search_mcp_tools', 'call_mcp_read_tool', 'call_mcp_write_tool',
})
# Injected into sub-agents only.
CONTEXTUAL = frozenset({'store_artifact', 'read_artifact', 'list_artifacts'})

# Searchable families that request routing used to drop from every provider
# surface on an ordinary project turn.
_ROUTED_AWAY_BEFORE = frozenset({
    'integration_submit', 'request_skill_install', 'schedule_create',
    'record_working_fact', 'manage_agents',
})

INVENTORY = {
    'eager': EAGER, 'pinned': PINNED, 'direct': DIRECT,
    'toggle_pinned': TOGGLE_PINNED, 'deferred': DEFERRED, 'gateway': GATEWAY,
    'compatibility': COMPATIBILITY, 'contextual': CONTEXTUAL,
}

_BACKENDS = {
    'local': ('openai', 'https://api.moonshot.cn/v1', 'kimi-k3', ''),
    'native_anthropic': (
        'anthropic', 'https://api.anthropic.com', 'claude-opus-4-6', ''),
    'native_openai': ('responses', 'https://api.openai.com/v1', '', 'openai'),
}


def _registered():
    """Every name owned by a built-in or bundled host-owned spec."""
    owners = {}
    for spec in all_specs():
        if spec.source == 'builtin' or spec.host_owned:
            for name in spec.provides:
                owners[name] = spec
    return owners


def _declared_class(spec, name):
    if name in spec.compatibility_tools:
        return 'compatibility'
    if name in spec.contextual_tools:
        return 'contextual'
    if name in GATEWAY_TOOL_NAMES:
        return 'gateway'
    if name in spec.direct_wire_tools:
        return 'direct'
    policy = spec.discovery_policy_by_name.get(name, spec.discovery_policy)
    if spec.pin_on_exposure:
        return 'pinned' if policy == 'eager' else 'toggle_pinned'
    return 'eager' if policy == 'eager' else 'deferred'


def _names(tools):
    return {(tool.get('function') or {}).get('name') or tool.get('name')
            for tool in tools or () if isinstance(tool, dict)}


def _functions(tools):
    """Flatten provider tools, keeping each function's deferral marker."""
    functions = {}
    for tool in tools or ():
        if tool.get('type') == 'namespace':
            functions.update(_functions(tool.get('tools')))
        elif isinstance(tool.get('function'), dict):
            functions[tool['function']['name']] = tool['function']
        elif tool.get('name'):
            functions[tool['name']] = tool
    return functions


@pytest.fixture(autouse=True)
def _request_local_routing(monkeypatch):
    from lib.tools.routing import _reset_sticky_exposure_for_tests
    # Cache markers are transport metadata, not part of the tool surface.
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints',
                        lambda *args, **kwargs: None)
    # Storage seam: this ordinary project conversation has no Coordinator role.
    monkeypatch.setattr(
        'lib.conversations.project_coordination.ProjectCoordinationRepository.read',
        lambda self, path: {'availability': 'unavailable'})
    _reset_sticky_exposure_for_tests()
    yield
    _reset_sticky_exposure_for_tests()


def _project_turn(backend='local', *, tool_search='auto'):
    """Assemble one durable project turn and project it for *backend*."""
    from lib.llm._sse_core import prepare_request
    from lib.model_info._openai_gpt56 import GPT56_ALIAS_TARGET
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools)
    from lib.tasks_pkg.orchestrator._tool_request_metadata import (
        attach_tool_request_metadata)

    protocol, base_url, model, profile = _BACKENDS[backend]
    model = model or GPT56_ALIAS_TARGET
    cfg = {'model': model, 'projectPath': '/tmp/tool-exposure-policy',
           'searchMode': 'multi', 'tools': {'toolSearch': tool_search}}
    task = {'id': 'tool-exposure-policy', 'convId': 'conv-tool-exposure',
            '_userId': 7, 'config': cfg,
            'messages': [{'role': 'user', 'content': 'Fix the login bug.'}]}
    mcfg = {'project_path': cfg['projectPath'], 'project_enabled': True,
            'search_mode': 'multi', 'search_enabled': True,
            'fetch_enabled': True, 'code_exec_enabled': False,
            'browser_enabled': False, 'desktop_enabled': False,
            'image_gen_enabled': False}
    candidates, has_tools = assemble_round_tools(cfg, task, mcfg)
    assert has_tools
    body = {'model': model, 'messages': deepcopy(task['messages']),
            'tools': deepcopy(candidates)}
    attach_tool_request_metadata(task, body, body['messages'], candidates,
                                 model=model, round_num=1, tid=task['id'])
    if profile:
        body['_responses_feature_profile'] = profile
    plan = prepare_request(body, base_url=base_url, api_key='test-key',
                           api_protocol=protocol)
    return task, _names(candidates), plan


def test_inventory_classifies_every_registered_tool():
    classified = [name for names in INVENTORY.values() for name in names]
    assert len(classified) == len(set(classified)), 'classes overlap'
    registered = _registered()
    assert sorted(set(registered) - set(classified)) == [], (
        'new tool without a reviewed default exposure')
    assert sorted(set(classified) - set(registered)) == [], (
        'inventory names a tool that is no longer registered')
    custom = next(spec for spec in all_specs() if spec.key == 'custom')
    # Caller-supplied per-request tools have no static names and stay direct.
    assert custom.provides == frozenset()
    assert custom.discovery_policy == 'eager'


def test_registered_declarations_match_reviewed_policy():
    expected = {name: cls for cls, names in INVENTORY.items() for name in names}
    actual = {name: _declared_class(spec, name)
              for name, spec in _registered().items()}
    assert {name: (actual[name], expected[name]) for name in actual
            if actual[name] != expected[name]} == {}


def test_routing_narrows_only_eager_families():
    from lib.tools.registry import ToolContext, assemble_tool_list

    ctx = ToolContext(
        cfg={}, task_id='exposure-routing', project_path='/tmp/routing',
        project_enabled=True, search_mode='multi', search_enabled=True,
        fetch_enabled=True, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, conv_id='conv-routing',
        messages=[{'role': 'user', 'content': 'Fix the login bug.'}])
    candidates, _ = assemble_tool_list(ctx)
    specs = {spec.key: spec for spec in all_specs()}
    assert ctx.omitted_spec_keys
    assert {specs[key].discovery_policy for key in ctx.omitted_spec_keys} == {
        'eager'}
    # The interrupted-write recovery tool is eager; routing must not drop it.
    assert 'inspect_tool_effect' in _names(candidates)
    assert {'integration_submit', 'request_skill_install',
            'schedule_create', 'record_working_fact'} <= _names(candidates)


def test_local_search_keeps_only_the_core_direct():
    task, candidates, plan = _project_turn('local')
    assert plan.tool_search_backend == 'local'
    wire = set(_functions(plan.body['tools']))
    catalog = _names(task['_executable_tool_catalog'])
    deferred = DEFERRED & candidates
    assert {'repo_map', 'browser_download_url_to_server', 'integration_submit',
            'request_skill_install', 'schedule_create', 'todo_write'} <= deferred
    assert not deferred & wire
    assert deferred <= catalog
    assert (EAGER | PINNED) & candidates <= wire
    assert 'inspect_tool_effect' in wire
    assert {'search_tools', 'execute_tools'} <= wire
    assert wire - GATEWAY <= EAGER | PINNED | DIRECT


def test_search_off_never_hides_a_declared_searchable_tool():
    task, candidates, plan = _project_turn('local', tool_search='off')
    assert plan.tool_search_backend == 'full'
    wire = set(_functions(plan.body['tools']))
    assert 'search_tools' not in wire
    owners = _registered()
    catalog = _names(task['_executable_tool_catalog'])
    # Families routing used to drop are direct when no discovery path exists.
    assert _ROUTED_AWAY_BEFORE | {
        'repo_map', 'browser_download_url_to_server'} <= wire
    # Only an explicit exposure preference (composer toggle or settings gate)
    # keeps a task tool off the search-off surface; exact-name calls remain
    # admitted under executionScope=available.
    hidden = catalog - wire
    assert hidden == {'ask_human', 'update_search_settings'}
    assert all(owners[name].exposure_gate is not None for name in hidden)
    assert DEFERRED & catalog - hidden <= wire


def test_native_anthropic_defers_exactly_the_declared_searchable_tools():
    _task, candidates, plan = _project_turn('native_anthropic')
    assert plan.tool_search_backend == 'native_anthropic'
    functions = _functions(plan.body['tools'])
    deferred = {name for name, fn in functions.items() if fn.get('defer_loading')}
    assert 'tool_search_tool_bm25' in set(functions) - deferred
    assert deferred == DEFERRED & candidates
    assert _ROUTED_AWAY_BEFORE <= deferred
    assert (EAGER | PINNED) & candidates <= set(functions) - deferred


def test_native_openai_catalog_contains_every_declared_searchable_tool():
    _task, candidates, plan = _project_turn('native_openai')
    assert plan.tool_search_backend == 'native_openai'
    functions = _functions(plan.body['tools'])
    deferred = {name for name, fn in functions.items() if fn.get('defer_loading')}
    assert _ROUTED_AWAY_BEFORE <= DEFERRED & candidates <= deferred
    assert PINNED <= {
        name for name, fn in functions.items() if not fn.get('defer_loading')}


@pytest.mark.parametrize('query,expected', [
    ('download the latest release zip from this page onto the server',
     'browser_download_url_to_server'),
    ('show a structural outline of the repository symbols', 'repo_map'),
    ('submit my isolated work for human review', 'integration_submit'),
    ('install a verified skill from the catalog', 'request_skill_install'),
])
def test_keyword_search_discloses_deferred_tools(query, expected):
    from lib.tools.gateway import search_executable_catalog

    task, _candidates, plan = _project_turn('local')
    wire = set(_functions(plan.body['tools']))
    assert expected not in wire
    found = search_executable_catalog(
        task['_executable_tool_catalog'], query, limit=3,
        namespace_by_name=task['_executableToolNamespaceByName'],
        search_text_by_name=task['_executableToolSearchTextByName'],
        contract_documents_by_name=task['_toolContractDocumentsByName'],
        visible_names=wire)
    assert found['status'] == 'ok'
    assert found['execute_with'] == 'execute_tools'
    assert found['items'][0]['name'] == expected
    assert found['items'][0]['arguments_schema']['type'] == 'object'
