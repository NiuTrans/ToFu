"""evolution_teach registration: scoped exposure, bound-handler authority."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _ctx(cfg):
    from lib.tools.registry._spec import ToolContext
    return ToolContext(
        cfg=cfg, task_id='t1', project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False)


def _assembled_names(ctx):
    from lib.tools.registry._spec import assemble_tool_list
    tools, _has_base = assemble_tool_list(ctx)
    return {t['function']['name'] for t in tools if t.get('function')}


def test_coordinate_schema_is_visible_and_requires_assignment_source():
    import jsonschema
    from lib.evolution.coordination import tool_definition
    from lib.tools.gateway import sanitize_wire_tools

    tool = tool_definition()
    schema = tool['function']['parameters']
    for model in ('', 'kimi-k3'):
        isolated = []
        wire = sanitize_wire_tools(
            [tool], model=model, on_tool_isolated=isolated.append)
        assert len(wire) == 1
        assert isolated == []

    jsonschema.validate({'action': 'assign', 'source_seq': 1}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'action': 'assign'}, schema)


def test_tool_visible_only_for_evolution_bound_tasks(store):
    bound = _ctx({'evolution': {'goal_id': 'g1', 'plan_id': 'p1'}})
    assert 'evolution_teach' in _assembled_names(bound)
    for cfg in ({}, {'evolution': {}}, {'evolution': {'episode_id': 'e'}},
                {'search_enabled': True}):
        assert 'evolution_teach' not in _assembled_names(_ctx(cfg))


def test_handler_rechecks_binding_and_teaches_with_owner(store):
    from lib.evolution.tool_handler import handle_evolution_teach
    task = {'_userId': USER}
    refused = handle_evolution_teach(
        task, None, 'evolution_teach', 'tc1', {'name': 'x'}, 0, {}, {},
        '', False)
    assert refused[0]['fetched'] is False
    assert 'evolution-bound' in refused[0]['snippet']

    ok = handle_evolution_teach(
        task, None, 'evolution_teach', 'tc2', {
            'name': 'hop-training', 'description': 'submit GPU jobs',
            'knowledge': 'hop MCP submit_job; queue=gpu-a100',
            'examples': ['submit 4-GPU job'],
            'code': 'def submit():\n    return "ok"\n',
            'tests': ('from skill_impl import submit\n\n\n'
                      'def test_ok():\n    assert submit() == "ok"\n')},
        0, {}, {'evolution': {'goal_id': 'g1'}}, '', False)
    assert ok[0]['title'] == 'taught: hop-training'
    assert 'proposed to the evolution gate' in ok[0]['snippet']
    from lib import evolution
    skills = evolution.list_forged_skills(user_id=USER)
    assert [s['name'] for s in skills] == ['hop-training']


def test_handler_surfaces_sandbox_failure_as_refinement_signal(store):
    from lib.evolution.tool_handler import handle_evolution_teach
    task = {'_userId': USER}
    result = handle_evolution_teach(
        task, None, 'evolution_teach', 'tc3', {
            'name': 'broken', 'description': 'd',
            'code': 'def f():\n    return 1\n',
            'tests': ('from skill_impl import f\n\n\n'
                      'def test_f():\n    assert f() == 2\n')},
        0, {}, {'evolution': {'goal_id': 'g1'}}, '', False)
    assert result[0]['fetched'] is False
    assert 'refine and retry' in result[0]['snippet']


def test_coordinator_can_coordinate_but_review_and_unbound_tasks_cannot(store):
    coordinator=_assembled_names(_ctx({'evolution':{'goal_id':'g','run_id':'r','stage':'coordinator'}}))
    assert 'evolution_coordinate' in coordinator
    assert 'evolution_material' in coordinator
    assert 'evolution_teach' not in coordinator
    review=_assembled_names(_ctx({'evolution':{'goal_id':'g','run_id':'r','stage':'reviewing'}}))
    assert not {'evolution_coordinate','evolution_material','evolution_teach'} & review
    assert 'evolution_coordinate' not in _assembled_names(_ctx({}))


def test_internal_spec_policy_narrows_executable_catalog_not_only_visibility(store):
    from lib.tools.registry._spec import assemble_tool_list
    context=_ctx({'evolution':{'goal_id':'g','run_id':'r','stage':'coordinator'},'toolSpecAllowlist':['evolution']})
    assemble_tool_list(context)
    names={tool['function']['name'] for tool in context.executable_tool_catalog}
    # The coordinator surface deliberately carries the index-rebuild tool too
    # (REINDEX_TOOL_DEFINITION in _build_evolution_teach).
    assert names=={'evolution_coordinate','evolution_material','evolution_reindex'}
    context=_ctx({'evolution':{'goal_id':'g','run_id':'r','stage':'reviewing'},'toolSpecAllowlist':[]})
    assert assemble_tool_list(context)==([],False)
    assert context.executable_tool_catalog==[]


def test_large_evolution_catalog_defers_schemas_without_revoking_authority(store):
    from lib.tools.registry._spec import assemble_tool_list
    from lib.llm._sse_core import prepare_request
    ctx = _ctx({'evolution': {'goal_id': 'g1', 'run_id': 'r1', 'stage': 'executing'}})
    tools, _ = assemble_tool_list(ctx)
    deferred = {'evolution_explore', 'evolution_material',
                'evolution_teach', 'evolution_reindex'}
    catalog = ctx.executable_tool_catalog
    authority_names = {tool['function']['name'] for tool in catalog}
    assert deferred <= authority_names
    assert all(ctx.discovery_policy_by_name[name] == 'searchable' for name in deferred)
    assert 'evolution_coordinate' in ctx.direct_wire_tool_names
    body = {
        'model': 'glm-5.3-flash',
        'messages': [{'role': 'user', 'content': 'research task'}],
        'tools': tools, '_tool_wire_catalog': tools,
        '_executable_tool_catalog': catalog,
        '_tool_discovery_policy_by_name': ctx.discovery_policy_by_name,
        '_tool_search_catalog_size': len(catalog),
        '_tool_searchable_count': sum(value == 'searchable'
                                      for value in ctx.discovery_policy_by_name.values()),
        '_required_direct_tool_names': list(ctx.direct_wire_tool_names),
        '_tool_search_mode': 'local', '_programmatic_tool_calling': 'off',
        '_multi_agent_mode': 'off',
    }
    plan = prepare_request(body, api_key='test', base_url='https://example.invalid/v1')
    visible = {tool['function']['name'] for tool in plan.body['tools']}
    assert not (deferred & visible)
    assert {'evolution_coordinate', 'search_tools', 'execute_tools'} <= visible
    assert {tool['function']['name'] for tool in catalog} == authority_names
