"""Loaded contracts remain one process snapshot under cache pressure."""
import json
import pytest
from lib.evolution import contracts

pytestmark = pytest.mark.unit


@pytest.fixture
def catalogue(monkeypatch, tmp_path):
    monkeypatch.setattr(contracts, 'CONTRACTS_DIR', tmp_path)
    for index in range(40):
        (tmp_path / f'contract-{index}.schema.json').write_text(json.dumps({
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'type': 'string', 'const': 'original'}))
    def clear():
        contracts.load_schema.cache_clear()
        contracts.contract_validator.cache_clear()
        catalog = getattr(contracts, '_schema_catalog', None)
        if catalog is not None: catalog.cache_clear()
    clear()
    yield tmp_path
    clear()


def test_loaded_schema_and_validator_cannot_split_after_cache_eviction(catalogue):
    original = contracts.load_schema('contract-0.schema.json')
    validator = contracts.contract_validator('contract-0.schema.json')
    path = catalogue / 'contract-0.schema.json'
    changed = {**original, 'const': 'changed'}
    path.write_text(json.dumps(changed))
    for index in range(1, 40):
        contracts.load_schema(f'contract-{index}.schema.json')
        contracts.contract_validator(f'contract-{index}.schema.json')
    assert contracts.load_schema('contract-0.schema.json') is original
    validator.validate('original')
    contracts.contract_validator('contract-0.schema.json').validate('original')
    with pytest.raises(contracts.jsonschema.ValidationError): validator.validate('changed')


def test_new_catalogue_members_require_a_new_snapshot(catalogue):
    contracts.load_schema('contract-0.schema.json')
    (catalogue / 'new.schema.json').write_text('{"type":"string"}')
    with pytest.raises(FileNotFoundError): contracts.load_schema('new.schema.json')
    assert contracts.load_schema.cache_info().currsize == 1
    with pytest.raises(FileNotFoundError): contracts.load_schema('../outside.schema.json')


def test_not_yet_parsed_contract_retains_the_captured_original(catalogue):
    contracts.load_schema('contract-0.schema.json')
    path = catalogue / 'contract-39.schema.json'
    path.write_text('{"type":"string","const":"changed"}')
    assert contracts.load_schema('contract-39.schema.json')['const'] == 'original'
