"""Executable admission boundaries for rule-only and mixed Agent automations."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.errors import AutomationError
from lib.automations.registry import ActionContract, ActionRegistry, builtin_registry

from tests.support.automation_fixtures import clock_definition, skill_definition

pytestmark = pytest.mark.unit


def test_rule_only_definition_needs_no_agent_or_token_budget():
    prepared = prepare_definition(clock_definition())
    assert prepared.ordered_step_ids == ('show',)
    assert prepared.action_contracts['task.show_time']['effect'] == 'presentation'


def test_mixed_definition_has_verified_order_and_frozen_contracts():
    value = skill_definition()
    prepared = prepare_definition(value)
    assert prepared.ordered_step_ids == ('check', 'activate', 'improve', 'verify')
    value['steps'][2]['arguments']['objective'] = 'changed after admission'
    assert prepared.definition['steps'][2]['arguments']['objective'].startswith('Improve')
    returned = prepared.action_contracts
    returned['agent.run']['version'] = 500
    assert prepared.action_contracts['agent.run']['version'] == 1
    assert prepare_definition(skill_definition()).digest == prepared.digest


@pytest.mark.parametrize(('mutate', 'code'), [
    (lambda d: d.update(enabled='yes'), 'invalid_definition'),
    (lambda d: d.update(name='  '), 'invalid_definition'),
    (lambda d: d['steps'][0].update(action='subprocess.run'), 'unknown_action'),
    (lambda d: d['steps'][0]['arguments'].update(command='rm -rf /'), 'invalid_action_arguments'),
    (lambda d: d['steps'][0].update(after=['absent']), 'unknown_dependency'),
    (lambda d: d['steps'][0].update(after=['show']), 'dependency_cycle'),
    (lambda d: d['steps'].append(deepcopy(d['steps'][0])), 'duplicate_step'),
    (lambda d: d['policy'].update(maxRuntimeSeconds=0), 'invalid_definition'),
])
def test_invalid_definition_is_not_admitted(mutate, code):
    value = clock_definition()
    mutate(value)
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == code


@pytest.mark.parametrize('schedule', ['0 0 31 2 *', '99 * * * *', '0 24 * * *', '* * 0 * *',
                                       '* * * 13 *', '* * * * 7', '*/0 * * * *',
                                       '*/-1 * * * *', '* * * *', '* * 9-2 * *'])
def test_invalid_schedules_fail_before_persistence(schedule):
    value = skill_definition()
    value['trigger']['schedule'] = schedule
    with pytest.raises(AutomationError, match='Cron'):
        prepare_definition(value)


def test_unknown_timezone_and_zero_agent_budget_fail_closed():
    value = skill_definition()
    value['trigger']['timezone'] = 'Mars/Olympus'
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'invalid_timezone'
    value['trigger']['timezone'] = 'UTC'
    value['policy']['maxModelTokens'] = 0
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'agent_budget_missing'


def test_step_cannot_read_evidence_from_future_or_unordered_work():
    value = skill_definition()
    value['steps'][1]['arguments']['snapshotId']['$source']['from'] = 'steps/improve'
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'unbound_source'


def test_catalogue_is_closed_and_schema_copies_cannot_change_execution():
    registry = builtin_registry()
    contract = registry.get('agent.run')
    changed = contract.input_schema
    changed['additionalProperties'] = True
    with pytest.raises(AutomationError):
        contract.validate({'objective': 'x', 'conversationPolicy': 'new', 'shell': 'anything'})
    with pytest.raises(AutomationError):
        ActionRegistry([contract, contract])
    invalid = contract.document()
    invalid['recovery'] = 'replay'
    with pytest.raises(AutomationError) as caught:
        ActionContract.from_document(invalid)
    assert caught.value.code == 'unsafe_action_recovery'


def test_non_json_and_oversized_definitions_are_rejected():
    value = clock_definition()
    value['policy']['maxRuntimeSeconds'] = float('nan')
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'invalid_json'
    value = clock_definition()
    value['description'] = 'a' * (128 * 1024)
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'definition_too_large'


def test_remote_action_schema_cannot_be_fetched_during_authoring():
    document = builtin_registry().get('task.show_time').document()
    document['input'] = {'$ref': 'https://untrusted.invalid/schema'}
    with pytest.raises(AutomationError) as caught:
        ActionContract.from_document(document)
    assert caught.value.code == 'invalid_action_contract'


def test_action_local_schema_references_support_runtime_bindings():
    document = builtin_registry().get('task.show_time').document()
    document['input'] = {
        'type': 'object', 'additionalProperties': False,
        'properties': {'nested': {'$ref': '#/$defs/nested'}}, 'required': ['nested'],
        '$defs': {'nested': {'type': 'object', 'additionalProperties': False,
                             'properties': {'time': {'type': 'number'}}, 'required': ['time']}},
    }
    registry = ActionRegistry([ActionContract.from_document(document)])
    value = clock_definition()
    value['steps'][0]['arguments'] = {'nested': {'time': {'$source': {
        'from': 'event', 'pointer': '/startedAt'}}}}
    assert prepare_definition(value, registry).ordered_step_ids == ('show',)


def test_browser_contract_is_generated_from_current_authorities():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, 'scripts/gen_automation_contract.py', '--check'],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout


def test_direct_descriptor_construction_cannot_bypass_registry_admission():
    contract = builtin_registry().get('agent.run')
    with pytest.raises(AutomationError) as caught:
        ActionRegistry([replace(contract, recovery='replay')])
    assert caught.value.code == 'unsafe_action_recovery'


@pytest.mark.parametrize('name', ['--all', '../outside', 'name,other', 'https://untrusted.invalid/repo', 'citadel\n'])
def test_cli_skill_identity_cannot_smuggle_flags_paths_or_sources(name):
    value = skill_definition()
    value['steps'][0]['arguments']['names'] = [name]
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'invalid_action_arguments'


def test_identifier_pattern_matches_the_whole_json_string():
    value = clock_definition()
    value['id'] += '\n'
    with pytest.raises(AutomationError) as caught:
        prepare_definition(value)
    assert caught.value.code == 'invalid_definition'
