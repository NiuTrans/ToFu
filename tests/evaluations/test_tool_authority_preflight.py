"""Comparison admission binds full executable contracts, not tool counts."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from evaluations.evolution.tool_authority import compare_tool_authority
pytestmark = pytest.mark.unit


def epoch(names=('read',)):
    return SimpleNamespace(executable_schemas=[{'type': 'function', 'function': {
        'name': name, 'parameters': {'type': 'object', 'properties': {}}}} for name in names],
        contract_documents_by_name={name: {'read_only': True} for name in names},
        degraded_reason='', epoch_hash='diagnostic')


def test_wire_independent_contract_match_is_not_activation():
    a, b = epoch(), epoch(); b.epoch_hash = 'different-wire-projection'
    result = compare_tool_authority(a, b)
    assert result['comparison_admitted'] and result['authority_equal']
    assert not result['provider_called'] and not result['activation_eligible']


def test_equal_count_different_tools_refuses():
    result = compare_tool_authority(epoch(), epoch(('write',)))
    assert not result['comparison_admitted']
    assert result['added_names'] == ['write'] and result['removed_names'] == ['read']


@pytest.mark.parametrize('change', ['schema', 'authority'])
def test_same_name_changed_contract_refuses(change):
    a = epoch(); b = deepcopy(a)
    if change == 'schema': b.executable_schemas[0]['function']['parameters']['required'] = ['path']
    else: b.contract_documents_by_name['read']['read_only'] = False
    assert compare_tool_authority(a, b)['changed_names'] == ['read']


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'degraded', 'empty'])
def test_incomplete_authority_cannot_be_admitted(fault):
    a = epoch()
    if fault == 'missing': a.contract_documents_by_name = {}
    elif fault == 'duplicate': a.executable_schemas *= 2
    elif fault == 'degraded': a.degraded_reason = 'assembly_failed'
    else: a.executable_schemas = []
    with pytest.raises(ValueError): compare_tool_authority(a, epoch())


def test_preflight_refuses_existing_evidence_before_catalog_build(tmp_path):
    from evaluations.evolution.tool_authority import main
    output = tmp_path / 'evidence.json'; output.write_text('original evidence')
    with pytest.raises(FileExistsError):
        main(['--owner-id', '1', '--model', 'controlled-model', '--output', str(output)])
    assert output.read_text() == 'original evidence'
