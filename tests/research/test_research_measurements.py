"""Measured observations preserve seed identities and refuse protocol mismatches."""
from copy import deepcopy
import pytest
from lib.research.measurements import build_paired_observation
from lib.research.observation_audit import verify_observations
from tests.support.research_observations import observation_payload
pytestmark = pytest.mark.unit


@pytest.fixture
def inputs():
    protocol = {'dataset': 'held-out', 'primary_metric': 'accuracy', 'baseline': 'reference',
                'random_seeds': [1,2,3], 'observation_format': 'tofu.scientific-observations/v2',
                'decision_rule': {'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}}
    obs = observation_payload(protocol, 'synthetic')['scientific_observations']
    return protocol, dict(comparison=obs['comparison'], partitions=obs['partitions'],
        method_samples=[{'seed':3,'value':8},{'seed':1,'value':4},{'seed':2,'value':6}],
        baseline_samples=[{'seed':2,'value':5},{'seed':3,'value':7},{'seed':1,'value':3}])


def test_identified_samples_align_and_do_not_claim_execution(inputs):
    protocol, values = inputs; before = deepcopy(values)
    result = build_paired_observation(protocol, **values)
    samples = result['scientific_observations']['samples']
    assert samples['method_values'] == [4,6,8] and samples['baseline_values'] == [3,5,7]
    assert samples['mean_difference'] == 1 and samples['standard_error'] == 0
    assert all(verify_observations(result, protocol).values())
    assert 'scientific_audit' not in result and 'evidence_refs' not in result
    assert values == before


@pytest.mark.parametrize('fault', ['duplicate','foreign','boolean','nan','partition','context'])
def test_invalid_measurement_is_not_repaired(inputs, fault):
    protocol, values = inputs
    if fault == 'duplicate': values['method_samples'][0]['seed'] = 1
    elif fault == 'foreign': values['baseline_samples'][0]['seed'] = 99
    elif fault == 'boolean': values['method_samples'][0]['value'] = True
    elif fault == 'nan': values['method_samples'][0]['value'] = float('nan')
    elif fault == 'partition': values['partitions']['dataset_size'] += 1
    else: values['comparison']['baseline']['dataset'] = 'other data'
    with pytest.raises(ValueError): build_paired_observation(protocol, **values)


def test_default_v1_retains_positional_contract(inputs):
    protocol, values = inputs; protocol.pop('observation_format')
    result = build_paired_observation(protocol, **values)
    samples = result['scientific_observations']['samples']
    assert result['scientific_observations']['format'] == 'tofu.scientific-observations/v1'
    assert 'method_seeds' not in samples and 'baseline_seeds' not in samples
    assert all(verify_observations(result, protocol).values())


def test_schema_error_and_missing_protocol_refuse(inputs):
    protocol, values = inputs
    with pytest.raises(ValueError): build_paired_observation(None, **values)
    values['comparison']['method']['dataset_digest'] = 'invalid identity'
    with pytest.raises(ValueError): build_paired_observation(protocol, **values)
