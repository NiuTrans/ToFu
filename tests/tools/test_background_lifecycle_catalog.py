"""Lifecycle visibility follows executable authority, never intent or budget."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from lib.swarm.tools import MASTER_TOOLS, SPAWN_AGENTS_TOOL
from lib.tools import gateway
from lib.tools.poll import BACKGROUND_TOOL_NAMES, KILL_TOOL, POLL_TOOL
from lib.tools.registry import ToolContext, all_specs, assemble_tool_list
from lib.tools.registry import _build
from lib.tools.registry._builtin_specs import build_builtin_specs

pytestmark = pytest.mark.unit


def _ctx(**overrides):
    fields = dict(
        cfg={}, task_id='lifecycle-catalog', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, enabled_plugins=set())
    fields.update(overrides)
    return ToolContext(**fields)


def _name(tool):
    return tool['function']['name']


def _bytes(tool):
    return json.dumps(tool, ensure_ascii=False, separators=(',', ':'))


@pytest.mark.parametrize('native_exposure', ['routed', 'full'])
@pytest.mark.parametrize('capability', ['bare_swarm', 'base', 'project', 'code_exec'])
def test_lifecycle_schemas_are_eager_in_real_assembly(native_exposure, capability):
    ctx = _ctx(
        cfg={'tools.nativeExposure': native_exposure},
        fetch_enabled=capability == 'base',
        project_enabled=capability == 'project',
        project_path='/workspace' if capability == 'project' else '',
        code_exec_enabled=capability == 'code_exec',
        messages=[{'role': 'user', 'content': 'Thanks, continue.'}],
    )
    surface, _ = assemble_tool_list(ctx)
    projected = gateway.local_wire_tools(
        surface, discovery_policy_by_name=ctx.discovery_policy_by_name)
    by_name = {_name(tool): tool for tool in projected}
    for canonical in (POLL_TOOL, KILL_TOOL):
        name = _name(canonical)
        assert _bytes(by_name[name]) == _bytes(canonical)
        assert ctx.discovery_policy_by_name[name] == 'eager'
        assert name in ctx.frontend_selected_tool_names
    assert 'await_agents' not in by_name
    assert 'spawn_agent' not in by_name


@pytest.mark.parametrize('capability', ['bare_swarm', 'base', 'project', 'code_exec'])
def test_storage_free_runtime_does_not_gain_lifecycle_tools(capability):
    ctx = _ctx(
        cfg={'_storageFreeRuntime': True},
        fetch_enabled=capability == 'base',
        project_enabled=capability == 'project',
        project_path='/workspace' if capability == 'project' else '',
        code_exec_enabled=capability == 'code_exec',
    )
    surface, _ = assemble_tool_list(ctx)
    assert not BACKGROUND_TOOL_NAMES.intersection(map(_name, surface))
    assert not BACKGROUND_TOOL_NAMES.intersection(
        map(_name, ctx.executable_tool_catalog))


def test_background_builder_requires_live_launch_capability():
    ctx = _ctx()
    assert _build._build_background(ctx) == []
    ctx.executable_tool_catalog = [SPAWN_AGENTS_TOOL]
    assert _build._build_background(ctx) == [POLL_TOOL, KILL_TOOL]
    ctx.cfg['_storageFreeRuntime'] = True
    assert _build._build_background(ctx) == []


def test_declarative_and_runtime_lifecycle_specs_agree():
    declared = {spec.key: spec for spec in build_builtin_specs()}
    registered = {spec.key: spec for spec in all_specs()}
    for key in ('swarm', 'background'):
        runtime = registered[key]
        assert declared[key].provides == runtime.provides
        assert declared[key].discovery_policy == runtime.discovery_policy
        assert declared[key].pin_on_exposure == runtime.pin_on_exposure
        assert declared[key].idempotent_tools == runtime.idempotent_tools
        assert declared[key].write_tools == runtime.write_tools
        assert declared[key].gate == runtime.gate
        assert declared[key].build is runtime.build
    assert 'spawn_agents' in declared['swarm'].provides
    assert 'spawn_agent' not in declared['swarm'].provides
    assert 'await_agents' in declared['swarm'].provides  # replay dispatch only
    assert declared['background'].idempotent_tools == {'poll'}
    assert declared['background'].write_tools == {'kill'}
    assert declared['background'].discovery_policy == 'eager'
    assert declared['background'].pin_on_exposure

    assert declared['background'].cacheable_tools == frozenset()
    from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
    assert 'kill' in _WRITE_TOOLS
    assert 'poll' not in _WRITE_TOOLS


def test_public_swarm_catalog_teaches_waiting_with_returned_ids():
    assert 'await_agents' not in set(map(_name, MASTER_TOOLS))
    description = SPAWN_AGENTS_TOOL['function']['description']
    assert 'await_agents' not in description
    assert 'poll(ids=[<returned agent ids>], wait_s=60' in description
    assert 'dashboard, not a wait' in description


@pytest.mark.parametrize('gateway_only', [False, True])
@pytest.mark.parametrize('tier', ['program', 'batch'])
def test_gateway_projections_and_tiny_budget_keep_canonical_controls(gateway_only, tier):
    catalog = deepcopy([POLL_TOOL, KILL_TOOL]) + [{
        'type': 'function', 'function': {
            'name': 'optional_lookup', 'description': 'large schema ' * 100,
            'parameters': {'type': 'object', 'properties': {}},
        },
    }]
    before = deepcopy(catalog)
    projected = gateway.local_wire_tools(
        catalog, discovery_policy_by_name={_name(t): 'searchable' for t in catalog},
        discovery_catalog_size=20, searchable_count=20)
    projected = gateway.ptc_local_wire_tools(
        projected, tier=tier, eligible=['optional_lookup'],
        exposure='gateway_only' if gateway_only else 'additive')
    fitted = gateway.fit_tool_schema_budget(projected, budget_tokens=1)
    by_name = {_name(tool): tool for tool in fitted}
    expected = {'poll', 'kill', 'execute_tools'}
    if not gateway_only:
        expected.add('search_tools')
    assert set(by_name) == expected
    for canonical in (POLL_TOOL, KILL_TOOL):
        assert _bytes(by_name[_name(canonical)]) == _bytes(canonical)
    assert catalog == before
    assert gateway.tool_schema_tokens(fitted) > 1


def test_authority_reprojection_revokes_stale_kill_and_deduplicates_poll():
    stale_poll = deepcopy(POLL_TOOL)
    stale_poll['function']['description'] = 'obsolete'
    projected = gateway.preserve_background_wire_tools(
        [stale_poll, KILL_TOOL, stale_poll], authority_catalog=[POLL_TOOL])
    assert projected == [POLL_TOOL]
    assert projected[0] is POLL_TOOL
    assert gateway.preserve_background_wire_tools(
        [POLL_TOOL, KILL_TOOL], authority_catalog=[]) == []
