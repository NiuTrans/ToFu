"""Project continuity wire consumers never fetch remote schemas or dilute refs."""
from copy import deepcopy

import pytest

from werkzeug.exceptions import RequestEntityTooLarge

from lib.project_dispatch import continuity_contract as contract

pytestmark = pytest.mark.unit


@pytest.fixture
def documents(monkeypatch):
    values = {
        'shared_files': {'$defs': {
            'id': {'type': 'string', 'minLength': 3, 'maxLength': 10},
            'request': {'type': 'object', 'additionalProperties': False, 'required': ['id'],
                        'properties': {'id': {'$ref': '#/$defs/id', 'maxLength': 5}}},
            'external': {'$ref': 'project_dispatch_v1.schema.json#/$defs/id'},
        }},
        'dispatch': {'$defs': {'id': {'type': 'string', 'minLength': 1}}},
    }
    monkeypatch.setattr(contract, 'document', lambda domain: deepcopy(values[domain]))
    return values


def test_local_reference_siblings_keep_intersection_not_replacement(documents):
    assert contract.validate('shared_files', 'request', {'id': 'abc'}) == {'id': 'abc'}
    for value in ({'id': 'a'}, {'id': '123456'}, {'id': 'abc', 'owner': 1}, {'id': True}):
        with pytest.raises(ValueError):
            contract.validate('shared_files', 'request', value)
    assert '$ref' not in str(contract.schema('shared_files', 'request'))


def test_declared_external_reference_is_local_and_inline(documents):
    assert contract.schema('shared_files', 'external') == {'type': 'string', 'minLength': 1}


@pytest.mark.parametrize('reference', [
    'https://evil.invalid/schema#/$defs/id', '../secrets.json#/$defs/id',
    'file:///tmp/schema#/$defs/id', 'project_dispatch_v1.schema.json',
    '#', '#/$defs/request',
])
def test_unregistered_or_cyclic_reference_fails(documents, reference):
    documents['shared_files']['$defs']['request'] = {'$ref': reference}
    with pytest.raises(ValueError):
        contract.schema('shared_files', 'request')


def test_unknown_contract_domain_does_not_read_a_path():
    with pytest.raises(ValueError):
        contract.document('../../secrets')


def test_http_request_body_is_derived_from_the_same_schema(documents):
    assert contract.request_body('shared_files', 'request') == {
        'required': True, 'content': {'application/json': {
            'schema': contract.schema('shared_files', 'request')}}}


@pytest.mark.parametrize('domain,names', [
    ('scope_changes', ('request', 'retry', 'listQuery', 'view')),
    ('responsibilities', ('create', 'pause', 'cancel', 'reconcile', 'readQuery', 'view')),
    ('shared_files', ('register', 'read', 'refresh', 'listQuery', 'page', 'content')),
])
def test_committed_contracts_are_closed_strict_and_valid(domain, names):
    from jsonschema import Draft202012Validator
    for name in names:
        value = contract.schema(domain, name)
        Draft202012Validator.check_schema(value)
        assert '$ref' not in str(value)
        assert value['additionalProperties'] is False
        assert value['required']


@pytest.mark.parametrize('failure', [RuntimeError('parser unavailable'), RequestEntityTooLarge()])
def test_strict_parse_does_not_convert_boundary_failures_to_empty_body(documents, monkeypatch, failure):
    from lib import quart_sync
    def broken_parser(**kwargs):
        assert kwargs['silent'] is False
        raise failure
    monkeypatch.setattr(quart_sync, 'request_json', broken_parser)
    with pytest.raises(type(failure)):
        contract.parse('shared_files', 'request')


def test_committed_browser_continuity_dtos_are_fresh():
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / 'scripts/gen_project_continuity_contract.py'), '--check'],
                            cwd=root, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
