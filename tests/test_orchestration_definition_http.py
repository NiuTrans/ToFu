"""Contract tests for shared definition HTTP projections."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.orchestration.definition_wire_contracts import (
    MAX_DEFINITION_VERSION,
    definition_write_conflict,
    definition_write_contract,
    definition_write_version_token,
)
import routes.api_v1.orchestration_definition_http as definition_http
import routes.api_v1.orchestration_definition_request_http as request_http


pytestmark = pytest.mark.unit


def test_definition_http_does_not_publish_a_parallel_service_call_wrapper():
    assert not hasattr(definition_http, 'definition_service_call')


def test_definition_http_consumes_published_header_and_token_contract():
    contract = definition_write_contract()
    assert definition_write_version_token(123) == '"123"'
    source = open(definition_http.__file__, encoding='utf-8').read()
    request_source = open(request_http.__file__, encoding='utf-8').read()
    assert "request.headers.get('If-Match')" not in source
    assert "http_response.headers['ETag']" not in source
    assert "_DEFINITION_WRITE['preconditionHeader']" in request_source
    assert "_DEFINITION_WRITE['versionResponseHeader']" in source
    assert "_DEFINITION_WRITE['versionField']" in source
    assert 'from quart import request' not in source
    assert contract['tokenSyntax'] == 'quoted-decimal'


def test_definition_conflict_projection_uses_published_field_registry():
    contract = definition_write_contract()
    fields = {
        semantic: spec['name']
        for semantic, spec in contract['conflictFields'].items()
    }

    projected = definition_write_conflict(7, 9, operation='delete')

    assert list(projected['write']) == list(fields.values())
    assert projected['write'] == {
        fields['format']: contract['format'],
        fields['reason']: contract['conflictReason'],
        fields['operation']: 'delete',
        fields['expectedUpdatedAt']: 7,
        fields['currentUpdatedAt']: 9,
    }
    assert projected[fields['currentUpdatedAt']] == 9

    unguarded = definition_write_conflict(None, None, operation='replace')
    assert unguarded['write'][fields['expectedUpdatedAt']] is None
    assert unguarded['write'][fields['currentUpdatedAt']] is None


@pytest.mark.parametrize(('expected', 'current'), (
    (True, 9),
    (-1, 9),
    (7, False),
    (7, MAX_DEFINITION_VERSION + 1),
))
def test_definition_conflict_projection_rejects_invalid_versions(
    expected,
    current,
):
    with pytest.raises(ValueError, match='safe non-negative int'):
        definition_write_conflict(expected, current)


def test_invalid_definition_projection_is_canonical_and_detached(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_bad_request(message, **fields):
        captured.update(message=message, **fields)
        return sentinel

    monkeypatch.setattr(definition_http, 'api_bad_request', fake_bad_request)
    inspection = {
        'format': 'tofu.orchestration.inspection/v1',
        'ok': False,
        'errors': ['missing stop'],
        'warnings': ['unused node'],
        'diagnostics': [],
        'contract': {'projection': 'flow'},
    }

    assert definition_http.invalid_definition_response(inspection) is sentinel
    assert captured['message'] == 'Invalid orchestration definition'
    assert captured['errors'] == ['missing stop']
    assert captured['warnings'] == ['unused node']
    assert captured['contract'] == {'projection': 'flow'}
    assert captured['inspection'] == inspection

    captured['errors'].append('client mutation')
    captured['inspection']['errors'].append('client mutation')
    assert inspection['errors'] == ['missing stop']


def test_impossible_create_result_uses_complete_typed_error(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_typed_error(kind, **fields):
        captured.update(kind=kind, **fields)
        return sentinel

    monkeypatch.setattr(definition_http, 'api_typed_error', fake_typed_error)
    result = SimpleNamespace(
        valid=True,
        conflict=False,
        entry=None,
        inspection={'ok': True},
        current_updated_at=None,
    )

    assert definition_http.definition_write_response(
        result, operation='create') == (sentinel, None)
    assert captured == {
        'kind': 'internal',
        'status': 500,
        'detail': 'Failed to create orchestration definition',
        'context': 'api_v1.orchestrations.create',
        'source': 'orchestration.definition.write',
    }
