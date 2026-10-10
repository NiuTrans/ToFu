"""Receipt authority: additive legacy compatibility, completion and cost uncertainty."""
from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / 'contracts/compaction_receipt_v1.schema.json').read_text())
FIXTURE = ROOT / 'tests/fixtures/compaction/receipt_v1.json'
VALIDATOR = Draft202012Validator(SCHEMA)


def test_receipt_fixture_has_one_registered_schema_authority():
    Draft202012Validator.check_schema(SCHEMA)
    receipt = json.loads(FIXTURE.read_text())
    VALIDATOR.validate(receipt)
    assert len(json.dumps(receipt).encode()) < 32 * 1024
    catalog = json.loads((ROOT / 'docs/catalog.json').read_text())
    assert {'path': 'contracts/compaction_receipt_v1.schema.json',
            'guard': 'tests/compaction/test_compaction_receipt_contract.py'} in catalog['contracts']
    assert 'contracts/compaction_receipt_v1.schema.json' in (
        ROOT / 'docs/contracts/API_CONTRACT.md').read_text()

    retired = json.loads((ROOT / 'contracts/config_knobs_v1.json').read_text())[
        'taskConfig']['compaction.summaryBudgetSeconds']
    assert retired['status'] == 'retired'
    assert 'ignored' in retired['description'] and 'no wall-clock deadline' in retired['description']

@pytest.mark.parametrize('complete,accepted', [(True, True), (True, False), (False, False)])
def test_current_producer_obeys_receipt_contract(complete, accepted):
    receipt = build_compaction_receipt(
        trigger='working_set', status='completed' if accepted else 'skipped',
        strategy='selective_summary', implementation='model_summary',
        summary_generated=complete, summary_rejected=not accepted,
        summary_text='complete fixture' if complete else '',
        summary_usage={
            'input_tokens': 1500, 'output_tokens': 250,

            '_summaryRoute': {'modelId': 'fixture-model', 'providerId': 'fixture-provider',
                              'policy': 'main', 'reason': 'fixture',
                              'costBasis': {'source': 'subscription_unpriced'}},
            '_summaryCalls': [{
                'model': 'fixture-model', 'provider_id': 'fixture-provider',
                'usage': {},  # No provider usage/cost resolver or credentials needed.
            }],
        },
        context_budget={'routeWindowTokens': 128000, 'hardCeilingTokens': 120000,
                        'economicTargetTokens': 64000, 'renderedPromptTokens': 96000,
                        'messageTokens': 92000, 'toolSchemaTokens': 4000,
                        'decisionKind': 'economic'},
        economics={'pricing_known': False, 'summary_cost_usd': None,
                   'rewrite_cost_usd': 0.001,
                   'payback_rounds': float('inf')},
    )
    VALIDATOR.validate(receipt)
    assert receipt['summary']['complete'] is complete
    assert receipt['summary']['accepted'] is accepted

    # Selector pricing evidence is diagnostic, not the cost ledger's enum.
    assert 'subscription_unpriced' in receipt['summary']['route']['costBasis']
    assert receipt['summary']['cost']['known'] is False
    assert receipt['summary']['cost']['costUsd'] is None
    assert receipt['contextBudget']['reserveTokens'] == 8000
    assert receipt['economics']['paybackRounds'] is None
    json.dumps(receipt, allow_nan=False)


def test_actual_selector_route_serialization_obeys_receipt_contract():
    from lib.tasks_pkg.compaction._summary_routing import select_summary_route

    # The missing-model path is hermetic: no provider or storage lookup.
    route = select_summary_route({'config': {}}, input_tokens=1, output_tokens=1)
    assert route['costBasis'] == {'source': 'unknown'}
    receipt = build_compaction_receipt(
        trigger='force', status='failed', strategy='selective_summary',
        implementation='model_summary', summary_usage={'_summaryRoute': route},
    )
    VALIDATOR.validate(receipt)
    assert receipt['summary']['route']['costBasis'] == "{'source': 'unknown'}"
    assert receipt['summary']['complete'] is False
    assert receipt['summary']['accepted'] is False


def test_legacy_additive_fields_are_optional_but_not_inferred():
    receipt = json.loads(FIXTURE.read_text())
    for key in ('complete', 'route', 'cost'):
        receipt['summary'].pop(key)
    receipt.pop('contextBudget')
    receipt['economics'] = {'summaryCostTokens': 2000, 'paybackRounds': 0.5}
    VALIDATOR.validate(receipt)


@pytest.mark.parametrize('invalid', [
    {'summary': {'complete': False, 'accepted': True}},
    {'summary': {'cost': {'known': False, 'costUsd': 0, 'costBasis': 'metered_api_equivalent'}}},
    {'summary': {'cost': {'known': True, 'costUsd': None, 'costBasis': 'metered_api_equivalent'}}},
    {'summary': {'cost': {'known': True, 'costUsd': -1, 'costBasis': 'metered_api_equivalent'}}},
    {'contextBudget': {'reserveTokens': -1}},
    {'economics': {'paybackRounds': -1}},
])
def test_receipt_contract_rejects_false_certainty_and_invalid_values(invalid):
    receipt = json.loads(FIXTURE.read_text())
    for key, value in invalid.items():
        receipt[key].update(deepcopy(value))
    assert not VALIDATOR.is_valid(receipt)


def _selection(**changes):
    return {'source': 'Artificial Analysis', 'metric': 'intelligence',
            'score': 52.4, 'match': 'exact', 'ranking': 'cost_asc_aa_desc',
            'eligibleRoutes': 3, 'fetchedAt': 1000, 'aaSlug': 'fixture-model',
            **changes}


def test_bounded_aa_selection_evidence_is_additive_and_separate_from_cost():
    evidence = _selection(aaSlug='x' * 500)
    original = deepcopy(evidence)
    receipt = build_compaction_receipt(
        trigger='force', status='completed', strategy='selective_summary',
        implementation='model_summary', summary_generated=True,
        summary_usage={'_summaryRoute': {
            'modelId': 'fixture-model', 'providerId': 'fixture-provider',
            'policy': 'aa-cheapest', 'reason': 'lowest_projected_cost_highest_aa',
            'selectionBasis': evidence}},
    )
    VALIDATOR.validate(receipt)
    basis = receipt['summary']['route']['selectionBasis']
    assert basis['score'] == 52.4
    assert len(basis['aaSlug']) == 200
    assert evidence == original
    assert receipt['summary']['cost']['known'] is False
    assert receipt['summary']['cost']['costUsd'] is None
    json.dumps(receipt, allow_nan=False)


@pytest.mark.parametrize('evidence', [
    None, {}, _selection(source='unknown'), _selection(match='fuzzy'),
    _selection(metric='coding'), _selection(ranking='aa_first'),
    _selection(score=True), _selection(score=-1), _selection(score=float('nan')),
    _selection(score=float('inf')), _selection(eligibleRoutes=True),
    _selection(eligibleRoutes=0),
])
def test_receipt_never_invents_or_serializes_invalid_aa_selection(evidence):
    receipt = build_compaction_receipt(
        trigger='force', status='failed', strategy='selective_summary',
        implementation='model_summary',
        summary_usage={'_summaryRoute': {'selectionBasis': evidence}},
    )
    VALIDATOR.validate(receipt)
    assert 'selectionBasis' not in receipt['summary']['route']
    json.dumps(receipt, allow_nan=False)


@pytest.mark.parametrize('changes', [
    {'score': -1}, {'match': 'fuzzy'}, {'source': 'unknown'},
    {'metric': 'coding'}, {'eligibleRoutes': 0}, {'ranking': 'aa_first'},
])
def test_receipt_schema_rejects_false_aa_selection_claims(changes):
    receipt = json.loads(FIXTURE.read_text())
    receipt['summary']['route']['selectionBasis'] = _selection(**changes)
    assert not VALIDATOR.is_valid(receipt)
