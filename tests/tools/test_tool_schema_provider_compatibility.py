"""Registered tool schemas reach every provider without per-request repair.

Provider tool-parameter validators reject a combinator at the parameters root:
OpenAI requires ``type: object`` without top-level ``oneOf``/``anyOf``/
``allOf``/``enum``/``const``/``not``; Anthropic rejects "oneOf, allOf, or anyOf
at the top level"; Moonshot MFJS needs a plain object root. The gateway keeps
dynamic (MCP/plugin) schemas callable by relaxing such a root on a copy, but a
built-in that depends on it pays that projection, and a WARNING line, on every
request. Built-in schemas therefore ship a plain object root; a choice between
arguments is registered in ``ARGUMENT_ALTERNATIVES`` and enforced by the
execution contract at admission. Request-local narrowings of a canonical
declaration (the project-dispatch file tools) are built-ins too.
"""
from __future__ import annotations

import pytest

from lib.model_info import is_kimi
from lib.tools.contracts import (
    ARGUMENT_ALTERNATIVES,
    ToolContractError,
    compile_execution_contract_documents,
    validate_tool_arguments_from_documents,
)
from lib.tools.gateway import _normalize_provider_tool_schema

pytestmark = pytest.mark.unit

# One model per provider family the gateway projects. Moonshot also rewrites
# its MFJS dialect (for example dropping maxLength) by design, so only its
# root projection counts as a repair here.
PROJECTED_MODELS = ('gpt-5.5', 'claude-opus-5.5', 'kimi-k2.5')
_ROOT = '$.function.parameters'
ROOT_REPAIRS = frozenset(
    {_ROOT, f'{_ROOT}.oneOf', f'{_ROOT}.anyOf', f'{_ROOT}.allOf'})

# Root combinators awaiting migration to ARGUMENT_ALTERNATIVES, by owner.
# Shrink-only: once an owner migrates, the ratchet test fails until its entry
# is deleted here.
PENDING_ROOT_COMBINATORS = {
    'ask_human': 'lib/tools/human_guidance.py (response_type-conditional anyOf)',
    'send_agent_message': 'contracts/swarm_messages_v1.json (grouped target forms)',
    'get_agent_messages': 'contracts/swarm_messages_v1.json (grouped target forms)',
}

# A files-mode dispatch assignment sees narrowed copies of the canonical file
# tools under the same names; they are keyed apart from the canonical ones.
DISPATCH_FILE_PROFILE = {'mode': 'files', 'tools': ['read_files', 'write_file', 'edit_file'],
                         'readPaths': ['src'], 'writePaths': ['src']}
DISPATCH_PREFIX = 'project_dispatch:'
# Surfaces that must declare and enforce their tool's registered argument rule.
ALTERNATIVE_SURFACES = sorted(ARGUMENT_ALTERNATIVES) + [f'{DISPATCH_PREFIX}read_files']


@pytest.fixture(scope='module')
def registered_tools(tmp_path_factory):
    """Every built-in schema: static owners plus a fully enabled registry build."""
    import lib.tasks_pkg.handlers  # noqa: F401 — handler-complete registry
    from lib.tool_input_repair._schema import _build_schema_index
    from lib.tools.registry import ToolContext, all_specs

    tools = {
        name: {'type': 'function',
               'function': {'name': name, 'parameters': parameters}}
        for name, parameters in _build_schema_index().items()
    }
    ctx = ToolContext(
        cfg={}, task_id='schema-compat',
        project_path=str(tmp_path_factory.mktemp('project')),
        project_enabled=True, search_mode='multi', search_enabled=True,
        fetch_enabled=True, code_exec_enabled=True, browser_enabled=True,
        desktop_enabled=True, image_gen_enabled=True,
        human_guidance_enabled=True, scheduler_enabled=True, messages=[],
        enabled_plugins=None)
    # Mirror assemble_tool_list's phase order (capability families gate on
    # the base phase). Third-party plugins vary per host and are excluded.
    for phase in ('base', 'capability'):
        for spec in all_specs():
            if spec.phase != phase or (
                    spec.source != 'builtin' and not spec.host_owned):
                continue
            for tool in spec.build(ctx) or []:
                tools[tool['function']['name']] = tool
                ctx.current_count += 1
        ctx.has_base_tools = ctx.current_count > 0
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import project_dispatch_file_tools
    for tool in project_dispatch_file_tools(DISPATCH_FILE_PROFILE):
        tools[DISPATCH_PREFIX + tool['function']['name']] = tool
    assert set(ARGUMENT_ALTERNATIVES) | set(PENDING_ROOT_COMBINATORS) <= set(tools)
    assert set(ALTERNATIVE_SURFACES) <= set(tools)
    return tools


@pytest.mark.parametrize('model', PROJECTED_MODELS)
def test_registered_tool_schemas_need_no_provider_projection(
        registered_tools, model):
    offenders = {}
    for name, tool in sorted(registered_tools.items()):
        if name in PENDING_ROOT_COMBINATORS:
            continue
        _projected, repairs, error = _normalize_provider_tool_schema(
            tool, model=model)
        counted = ROOT_REPAIRS.intersection(repairs) if is_kimi(model) else repairs
        if error or counted:
            offenders[name] = error or sorted(counted)
    assert not offenders, (
        f'{model}: built-in schemas need a per-request provider projection: '
        f'{offenders}. Declare a plain object root and register an argument '
        'choice in lib.tools.contracts.ARGUMENT_ALTERNATIVES instead of a '
        'root oneOf/anyOf/allOf.')


def test_pending_root_combinators_are_shrink_only(registered_tools):
    for name, owner in PENDING_ROOT_COMBINATORS.items():
        _projected, repairs, _error = _normalize_provider_tool_schema(
            registered_tools[name], model='gpt-5.5')
        assert ROOT_REPAIRS.intersection(repairs), (
            f'{name} ({owner}) no longer needs a root projection; delete it '
            'from PENDING_ROOT_COMBINATORS.')


@pytest.mark.parametrize('surface', ALTERNATIVE_SURFACES)
def test_argument_alternatives_are_optional_declared_properties(
        registered_tools, surface):
    tool = registered_tools[surface]
    rule = ARGUMENT_ALTERNATIVES[tool['function']['name']]
    parameters = tool['function']['parameters']
    assert len(rule.names) >= 2 and rule.next_action
    assert set(rule.names) <= set(parameters['properties'])
    assert not set(rule.names) & set(parameters.get('required') or ())


def _example(schema):
    if schema.get('type') == 'object':
        return {key: _example(schema['properties'][key])
                for key in schema.get('required') or ()}
    if schema.get('type') == 'array':
        return [_example(schema['items'])]
    return {'string': 'x', 'integer': 1, 'boolean': True}[schema['type']]


@pytest.mark.parametrize('surface', ALTERNATIVE_SURFACES)
def test_admission_enforces_argument_alternatives(registered_tools, surface):
    tool = registered_tools[surface]
    name = tool['function']['name']
    rule = ARGUMENT_ALTERNATIVES[name]
    properties = tool['function']['parameters']['properties']
    documents = compile_execution_contract_documents([tool])
    base = {key: _example(properties[key])
            for key in tool['function']['parameters'].get('required') or ()}
    choices = {key: _example(properties[key]) for key in rule.names}

    for key, value in choices.items():
        validate_tool_arguments_from_documents(
            documents, name, {**base, key: value})
    with pytest.raises(ToolContractError) as missing:
        validate_tool_arguments_from_documents(documents, name, base)
    if rule.exactly_one:
        with pytest.raises(ToolContractError) as mixed:
            validate_tool_arguments_from_documents(
                documents, name, {**base, **choices})
    else:
        validate_tool_arguments_from_documents(
            documents, name, {**base, **choices})

    if name in PENDING_ROOT_COMBINATORS:
        return  # The legacy root combinator still rejects first.
    assert missing.value.code == 'missing_required_arguments'
    assert ', '.join(rule.names) in str(missing.value)
    assert missing.value.next_action == rule.next_action
    if rule.exactly_one:
        assert mixed.value.code == 'invalid_argument_value'
        assert mixed.value.next_action == rule.next_action
