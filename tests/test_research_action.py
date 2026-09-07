"""Least-authority and evidence gates for agentic research actions."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _schema(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': {}},
    }}


def _epoch(names):
    from lib.paper.tools import PaperToolEpochV2

    schemas = tuple(_schema(name) for name in names)
    return PaperToolEpochV2(
        wire_schemas=schemas, executable_schemas=schemas,
        contract_documents_by_name={name: {'schema_hash': name} for name in names},
        discovery_policy_by_name={name: 'eager' for name in names},
        namespace_by_name={name: 'test' for name in names},
        search_text_by_name={name: name for name in names},
        script_safe_by_name={name: False for name in names},
        schema_tokens=1, gateway_schema_tokens=0, schema_budget_tokens=100,
        result_envelope='v2', epoch_hash='full',
    )


def test_action_epoch_exposes_only_native_allowlist_and_exact_binding(monkeypatch):
    import lib.research.action as action

    bound = 'mcp__any-provider__launch'
    unbound = 'mcp__any-provider__delete_all'
    monkeypatch.setattr(action, 'build_paper_full_tool_epoch', lambda **_kwargs: _epoch([
        'web_search', 'fetch_url', 'run_command', 'create_memory', bound, unbound,
    ]))
    workspace = {'capability_bindings': [{
        'capability': 'experiment.execute', 'provider': 'mcp',
        'tool': bound, 'schema_hash': 'one', 'enabled': True,
        'argument_defaults': {}, 'notes': '',
    }]}
    catalog = {'tools': [
        {'name': bound, 'read_only': False, 'schema_hash': 'one'},
        {'name': unbound, 'read_only': False, 'schema_hash': 'two'},
    ]}
    epoch, bindings, problems = action.build_action_tool_epoch(
        action='experiment', user_id=1, workspace=workspace,
        confirm_external_writes=True, catalog=catalog)
    names = {row['function']['name'] for row in epoch.executable_schemas}
    assert names == {'web_search', 'fetch_url', 'run_command', bound}
    assert bindings[0]['tool'] == bound and not problems


def test_write_actions_require_explicit_confirmation():
    from lib.research.action import build_action_tool_epoch

    with pytest.raises(ValueError, match='confirm_external_writes'):
        build_action_tool_epoch(
            action='experiment', user_id=1, workspace={},
            confirm_external_writes=False, catalog={'tools': []})


def test_research_context_preserves_abort_signal(monkeypatch):
    import lib.research.action as action
    import lib.research.persistence as persistence
    from lib.llm_errors import AbortedError

    def abort(*_args, **_kwargs):
        raise AbortedError('owner stopped the task')

    monkeypatch.setattr(persistence, 'load_research_artifacts', abort)
    with pytest.raises(AbortedError, match='owner stopped'):
        action._research_context('Causal compression', 'en', 1)


def test_bound_argument_defaults_are_applied_but_call_arguments_win():
    from lib.research.action import _merge_bound_arguments

    got = _merge_bound_arguments(
        'mcp__provider__run', {'seed': 7}, [{
            'tool': 'mcp__provider__run',
            'argument_defaults': {'queue': 'research', 'seed': 3},
        }])
    assert got == {'queue': 'research', 'seed': 7}


def test_experiment_cannot_pass_without_a_successful_tool_receipt():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Causal compression')
    updated = apply_action_result(
        action='experiment', workspace=workspace,
        payload={'run': {'id': 'r1', 'status': 'passed', 'metric': '99'}},
        receipts=[], task_id='task-1', bindings=[])
    assert updated['runs'][0]['status'] == 'planned'
    assert updated['runs'][0]['task_id'] == 'task-1'


def test_experiment_ignores_search_receipts_and_invented_artifact_refs():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Evidence contracts'),
        payload={'run': {
            'id': 'r1', 'status': 'passed',
            'artifact_refs': ['artifact://invented'],
        }},
        receipts=[{
            'tool': 'web_search', 'status': 'done',
            'artifact_ref': 'evidence:search', 'result_excerpt': 'papers only',
        }],
        task_id='task-1', bindings=[])
    assert updated['runs'][0]['status'] == 'planned'
    assert updated['runs'][0]['artifact_refs'] == []


def test_analysis_downgrades_unobserved_supported_claim_and_visual():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    updated = apply_action_result(
        action='analyze', workspace=empty_workspace('Evidence contracts'),
        payload={
            'claims': [{
                'id': 'c1', 'text': 'Large gain', 'status': 'supported',
                'evidence_refs': ['artifact://invented'],
            }],
            'figures': [{
                'id': 'f1', 'status': 'verified',
                'data_ref': 'data://invented', 'script_ref': 'code://invented',
                'output_ref': 'figure://invented',
            }],
        },
        receipts=[], task_id='task-1', bindings=[])
    assert updated['claims'][0]['status'] == 'draft'
    assert updated['claims'][0]['evidence_refs'] == []
    assert updated['figures'][0]['status'] == 'planned'
    assert updated['figures'][0]['output_ref'] == ''


def test_publish_cannot_claim_success_from_model_text_alone():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Evidence contracts')
    workspace['source_files'] = [{'path': 'main.tex', 'content': 'paper'}]
    binding = {'capability': 'publication.push', 'tool': 'mcp__vendor__push'}
    rejected = apply_action_result(
        action='publish', workspace=workspace,
        payload={'status': 'published', 'project_url': 'https://example.test/p'},
        receipts=[], task_id='task-1', bindings=[binding])
    assert rejected['publication']['status'] == 'failed'

    accepted = apply_action_result(
        action='publish', workspace=workspace,
        payload={'status': 'published', 'project_url': 'https://example.test/p'},
        receipts=[{'tool': binding['tool'], 'status': 'done'}],
        task_id='task-2', bindings=[binding])
    assert accepted['publication']['status'] == 'published'
    assert accepted['publication']['source_digest']
    assert accepted['publication']['provider'] == 'vendor'
    assert accepted['publication']['project_url'] == ''
