"""Optional domains do not pollute core tasks or gain authority from routing."""
from dataclasses import replace
import pytest
from lib.tools.registry import ToolContext, ToolSpec, all_specs, assemble_tool_list, resolve_enabled_plugins

pytestmark = pytest.mark.unit


def context(tmp_path, plugins=(), mode='routed', **changes):
    values = dict(cfg={'plugins': list(plugins), 'mcpEnabled': False,
                       'toolSpecAllowlist': ['read_files', 'project', 'image_gen', 'produce', 'produce_edits',
                                            'motion_video', 'paper_build', 'paper_citations', 'paper_quality', 'local_serve'],
                       'tools': {'nativeExposure': mode}},
        task_id='optional-test', project_path=str(tmp_path), project_enabled=True,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        enabled_plugins=set(plugins), owner_user_id=7,
        messages=[{'role': 'user', 'content': 'Fix this Python file'}])
    values.update(changes)
    return ToolContext(**values)


def names(rows):
    return {r['function']['name'] for r in rows}


def test_domains_are_plugins_and_default_project_does_not_acquire_them(tmp_path):
    ctx = context(tmp_path)
    assemble_tool_list(ctx)
    available = names(ctx.executable_tool_catalog)
    assert {'read_files', 'edit_file', 'run_command'} <= available
    assert not available & {'produce_slides', 'paper_build', 'motion_video_render', 'local_serve_deploy', 'generate_image'}
    for spec in all_specs():
        if spec.key in {'produce', 'paper_build', 'motion_video', 'local_serve', 'image_gen'}:
            assert spec.source == 'plugin'


@pytest.mark.parametrize('mode', ['full', 'routed'])
@pytest.mark.parametrize('plugin,expected', [('papers', 'paper_check'), ('production', 'produce_slides'),
                                           ('image_generation', 'generate_image'), ('local_models', 'local_serve_list')])
def test_explicit_plugin_is_visible_searchable_and_not_pinned(tmp_path, mode, plugin, expected):
    ctx = context(tmp_path, [plugin], mode)
    wire, _ = assemble_tool_list(ctx)
    assert expected in names(wire)
    assert expected in names(ctx.executable_tool_catalog)
    assert ctx.discovery_policy_by_name[expected] == 'searchable'
    assert expected not in ctx.frontend_selected_tool_names
    if plugin == 'production':
        assert ctx.search_mode == 'off'  # Production no longer depends on the search toolbar.


def test_explicit_empty_list_overrides_feature_and_environment_defaults(monkeypatch):
    monkeypatch.setenv('TOFU_DEFAULT_TOOL_PLUGINS', 'production')
    assert resolve_enabled_plugins({'plugins': [], 'imageGenEnabled': True}) == set()
    assert resolve_enabled_plugins({'imageGenEnabled': True}) == {'production', 'image_generation'}


def test_image_switch_remains_an_explicit_opt_in(tmp_path):
    ctx = context(tmp_path, image_gen_enabled=True)
    ctx.cfg.pop('plugins')
    wire, _ = assemble_tool_list(ctx)
    assert 'generate_image' in names(wire)
    ctx = context(tmp_path, image_gen_enabled=True)
    wire, _ = assemble_tool_list(ctx)
    assert 'generate_image' not in names(wire)


def test_declared_lifecycles_distinguish_subagent_tools_from_retired_names():
    specs = {s.key: s for s in all_specs()}
    assert specs['swarm'].contextual_tools == {'store_artifact', 'read_artifact', 'list_artifacts'}
    assert specs['swarm'].compatibility_tools == {'await_agents'}
    assert {'apply_diff', 'check_background_command'} <= specs['project'].compatibility_tools
    from lib.tools.registry._builtin_specs import build_builtin_specs
    canonical = {s.key: s for s in build_builtin_specs()}
    assert {s.key for s in all_specs() if s.source == 'builtin'} == set(canonical)
    for key, declaration in canonical.items():
        assert declaration.provides == specs[key].provides
        assert declaration.discovery_policy == specs[key].discovery_policy


def test_fine_grained_policies_do_not_hide_execution_authority():
    specs = {s.key: s for s in all_specs()}
    assert specs['memory'].discovery_policy_by_name['delete_memory'] == 'searchable'
    assert specs['swarm'].discovery_policy_by_name['set_approval_delegate'] == 'searchable'
    assert specs['timer_watcher'].discovery_policy == 'searchable'
    assert specs['todo'].discovery_policy == 'searchable'


def test_external_entrypoint_cannot_replace_bundled_schema_or_write_policy():
    from lib.tools.registry import register_tool_spec
    original = next(s for s in all_specs() if s.key == 'paper_citations')
    assert original.host_owned
    replacement = replace(original, host_owned=False, write_tools=frozenset())
    register_tool_spec(replacement, replace=True)
    assert next(s for s in all_specs() if s.key == original.key) is original


def test_external_discovery_clears_spoofed_host_ownership(monkeypatch):
    from lib.tools.registry import _plugins
    import importlib.metadata
    collected = []
    marker = ToolSpec('spoofed', lambda ctx: [], source='plugin',
                      plugin_name='spoofed', host_owned=True)
    class EntryPoint:
        name = 'external_spoof'
        def load(self):
            return lambda register: register(marker)
    monkeypatch.setattr(_plugins, 'register_tool_spec', lambda spec, **kwargs: collected.append(spec))
    monkeypatch.setattr(importlib.metadata, 'entry_points', lambda **kwargs: [EntryPoint()])
    _plugins.discover_plugin_specs()
    actual = next(spec for spec in collected if spec.key == 'spoofed')
    assert actual.source == 'plugin' and not actual.host_owned
    assert actual.plugin_name == 'external_spoof'


def test_bundled_owner_can_reclaim_a_late_registered_external_name(monkeypatch):
    from lib.tools.registry import _spec
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [])
    monkeypatch.setattr(_spec, '_REGISTERED_KEYS', set())
    monkeypatch.setattr(_spec, '_dispatch_registry', None)
    foreign = ToolSpec('foreign', lambda ctx: [], provides=frozenset({'owned_name'}),
                       source='plugin', plugin_name='foreign')
    owned = ToolSpec('owned', lambda ctx: [], provides=frozenset({'owned_name'}),
                     source='plugin', plugin_name='bundled', host_owned=True)
    _spec.register_tool_spec(foreign)
    _spec.register_tool_spec(owned)
    assert _spec.all_specs() == [owned]
