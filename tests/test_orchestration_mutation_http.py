"""Contract tests for shared orchestration mutation HTTP ingress/projection."""

from __future__ import annotations

import pytest

import lib.orchestration.human_gate_request_contract as gate_contract_module
from lib.orchestration_mutation import (
    MUTATION_ACTION_APPROVE_GATE,
    OrchestrationMutationResult,
)
from lib.request_parser import BadRequest
import routes.api_v1.orchestration_mutation_http as mutation_http
import routes.api_v1.orchestration_mutation_service_http as mutation_service_http


pytestmark = pytest.mark.unit


def test_mutation_http_does_not_publish_a_parallel_service_call_wrapper():
    assert not hasattr(mutation_http, 'mutation_service_call')


def test_mutation_service_http_unifies_compatibility_and_success_hook(
    monkeypatch,
):
    projected = []
    successes = []

    def service_response(context, operation, projector):
        projected.append(context)
        return projector(operation())

    def http_response(result, *, compatibility=None):
        projected.append((result, compatibility))
        return 'response'

    monkeypatch.setattr(
        mutation_service_http,
        'orchestration_service_response',
        service_response,
    )
    monkeypatch.setattr(
        mutation_service_http,
        'mutation_http_response',
        http_response,
    )
    accepted = OrchestrationMutationResult(
        True,
        action=MUTATION_ACTION_APPROVE_GATE,
        target_id='gate-1',
    )

    response = mutation_service_http.orchestration_mutation_service_response(
        'api.test.approve',
        lambda: accepted,
        endpoint='human-approve',
        target_id='gate-1',
        approved=True,
        on_success=lambda: successes.append('logged'),
    )

    assert response == 'response'
    assert projected == [
        'api.test.approve',
        (accepted, {'requestId': 'gate-1', 'approved': True}),
    ]
    assert successes == ['logged']


def test_human_gate_schemas_match_typed_request_parsers():
    contract = gate_contract_module.human_gate_request_contract()
    assert contract == {
        'requestIdField': 'requestId',
        'approvalField': 'approved',
        'approvalDefault': False,
        'inputField': 'response',
        'inputMaxLength': mutation_http.MAX_HUMAN_INPUT_LENGTH,
    }
    assert mutation_http.human_approval_request_schema is \
        gate_contract_module.human_approval_request_schema
    assert mutation_http.human_input_request_schema is \
        gate_contract_module.human_input_request_schema
    assert 'def human_approval_request_schema' not in open(
        'routes/api_v1/orchestration_mutation_http.py',
        encoding='utf-8',
    ).read()
    approval = mutation_http.human_approval_request_schema()
    guidance = mutation_http.human_input_request_schema()
    assert approval['required'] == ['requestId']
    assert approval['properties']['approved'] == {
        'type': 'boolean', 'default': False,
    }
    assert guidance['required'] == ['requestId', 'response']
    assert guidance['properties']['response'] == {
        'type': 'string',
        'minLength': 1,
        'maxLength': mutation_http.MAX_HUMAN_INPUT_LENGTH,
    }


def test_human_gate_ingress_consumes_canonical_field_identity():
    source = open(
        'routes/api_v1/orchestration_mutation_http.py',
        encoding='utf-8',
    ).read()
    contract_source = open(
        'lib/orchestration/human_gate_request_contract.py',
        encoding='utf-8',
    ).read()
    assert "body, 'requestId'" not in source
    assert "body, 'approved'" not in source
    assert "body, 'response'" not in source
    assert len(contract_source.splitlines()) < 70


def test_human_gate_requests_share_id_and_typed_value_parsing():
    approval, failure = mutation_http.prepare_human_approval_request({
        'requestId': '  gate-1  ',
        'approved': 'false',
    })
    assert failure is None
    assert approval is not None
    assert approval.request_id == 'gate-1'
    assert approval.approved is False

    guidance, failure = mutation_http.prepare_human_input_request({
        'requestId': ' gate-2 ',
        'response': '  continue  ',
    })
    assert failure is None
    assert guidance is not None
    assert guidance.request_id == 'gate-2'
    assert guidance.response_text == 'continue'


def test_human_gate_request_rejects_missing_id_and_invalid_boolean(
        monkeypatch):
    sentinel = object()
    captured = {}

    def fake_bad_request(message, **fields):
        captured.update(message=message, fields=fields)
        return sentinel

    monkeypatch.setattr(mutation_http, 'api_bad_request', fake_bad_request)
    prepared, failure = mutation_http.prepare_human_input_request({})
    assert prepared is None
    assert failure is sentinel
    assert captured == {
        'message': 'requestId is required',
        'fields': {'field': 'requestId'},
    }

    with pytest.raises(BadRequest, match='approved must be a boolean') as error:
        mutation_http.prepare_human_approval_request({
            'requestId': 'gate-3', 'approved': {},
        })
    assert error.value.field == 'approved'


def test_human_input_request_rejects_missing_or_blank_response(monkeypatch):
    sentinel = object()
    captured = []

    def fake_bad_request(message, **fields):
        captured.append((message, fields))
        return sentinel

    monkeypatch.setattr(mutation_http, 'api_bad_request', fake_bad_request)
    for body in (
        {'requestId': 'gate-4'},
        {'requestId': 'gate-4', 'response': '   '},
    ):
        prepared, failure = mutation_http.prepare_human_input_request(body)
        assert prepared is None
        assert failure is sentinel

    assert captured == [
        ('response is required', {'field': 'response'}),
        ('response is required', {'field': 'response'}),
    ]


def test_mutation_http_response_is_the_only_wire_status_projection(monkeypatch):
    result = object()
    payload = {'ok': False, 'mutation': {'reason': 'active'}}
    captured = {}
    sentinel = object()

    def fake_mutation_response(value, *, compatibility=None):
        captured.update(result=value, compatibility=compatibility)
        return payload, 409

    def fake_api_payload(value, status=200, **fields):
        captured.update(payload=value, status=status, fields=fields)
        return sentinel

    monkeypatch.setattr(
        mutation_http, 'mutation_response', fake_mutation_response)
    monkeypatch.setattr(mutation_http, 'api_payload', fake_api_payload)

    response = mutation_http.mutation_http_response(
        result, compatibility={'run_id': 'run-1'})

    assert response is sentinel
    assert captured == {
        'result': result,
        'compatibility': {'run_id': 'run-1'},
        'payload': payload,
        'status': 409,
        'fields': {},
    }
