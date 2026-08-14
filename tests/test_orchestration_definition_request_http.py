"""Contract tests for shared inline-or-stored definition HTTP ingress."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import routes.api_v1.orchestration_definition_request_http as request_http
from lib.orchestration.definition_selection_contract import (
    definition_selection_contract,
)


pytestmark = pytest.mark.unit


def test_definition_selection_schema_matches_shared_parser_axes():
    contract = definition_selection_contract()
    selection = request_http.definition_selection_request_schema()
    run = request_http.definition_selection_request_schema(
        include_input=True)

    assert selection['anyOf'] == [
        {'required': ['definition']},
        {'required': ['id']},
    ]
    assert selection['properties']['definition']['required'] == [
        'name', 'nodes', 'edges',
    ]
    assert selection['properties']['id']['minLength'] == 1
    assert selection['properties']['originId'] == {
        'type': 'string', 'minLength': 1,
    }
    assert 'input' not in selection['properties']
    assert run['properties']['input'] == {
        'type': 'string',
        'maxLength': request_http.MAX_RUN_INPUT_LENGTH,
    }
    assert contract == {
        'inlineField': 'definition', 'storedIdField': 'id',
        'originField': 'originId',
        'inputField': 'input',
        'inputMaxLength': request_http.MAX_RUN_INPUT_LENGTH,
    }


def test_definition_precondition_uses_contract_header_and_canonical_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        request_http,
        'request',
        SimpleNamespace(headers={'If-Match': 'W/"42"'}),
    )
    assert request_http.definition_precondition() == (42, None)

    sentinel = object()
    captured = {}
    monkeypatch.setattr(
        request_http,
        'request',
        SimpleNamespace(headers={'If-Match': '*'}),
    )
    monkeypatch.setattr(
        request_http,
        'api_bad_request',
        lambda message, **fields: (
            captured.update(message=message, **fields) or sentinel
        ),
    )

    assert request_http.definition_precondition() == (None, sentinel)
    assert captured['field'] == 'If-Match'


def test_definition_ingress_preserves_resolver_result_and_provenance():
    body = {'id': 'flow-1'}
    resolved = SimpleNamespace(
        definition={'nodes': [], 'edges': []},
        source='stored',
        stored_id='flow-1',
    )
    seen = []

    prepared, failure = request_http.resolve_definition_request(
        body,
        resolve_definition=lambda value: seen.append(value) or resolved,
    )

    assert failure is None
    assert prepared is resolved
    assert prepared.source == 'stored'
    assert seen == [body]


def test_definition_ingress_owns_missing_selection_failure(monkeypatch):
    sentinel = object()
    captured = {}

    def fake_bad_request(message, **fields):
        captured.update(message=message, fields=fields)
        return sentinel

    monkeypatch.setattr(request_http, 'api_bad_request', fake_bad_request)
    prepared, failure = request_http.resolve_definition_request(
        {},
        resolve_definition=lambda _body: SimpleNamespace(
            definition=None, source='', stored_id=''),
    )

    assert prepared is None
    assert failure is sentinel
    assert captured == {
        'message': 'definition or id is required',
        'fields': {},
    }


def test_definition_ingress_uses_shared_application_service_boundary(
    monkeypatch,
):
    sentinel = object()
    seen = []

    def fake_service_call(context, operation):
        seen.append((context, operation))
        return None, sentinel

    monkeypatch.setattr(
        request_http, 'orchestration_service_call', fake_service_call)
    prepared, failure = request_http.resolve_definition_request(
        {'id': 'flow-1'},
        resolve_definition=lambda _body: pytest.fail(
            'failure projection must skip the resolver result path'),
    )

    assert prepared is None
    assert failure is sentinel
    assert len(seen) == 1
    assert seen[0][0] == 'api_v1.orchestrations.resolve_definition'
