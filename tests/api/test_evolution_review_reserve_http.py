"""Authenticated creation freezes the review policy; reads never launch work."""
import pytest
from tests.api.test_api_v1_evolution import client as client, _run

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit


def test_authenticated_program_policy_is_stored_readable_and_immutable(client):
    http, token, _ = client
    headers = {'Authorization': f'Bearer {token}', 'Idempotency-Key': 'http-review-reserve'}
    body = {'objective': 'Investigate a real failure and complete independent review.',
            'budget': {'max_tokens': 400000, 'max_tool_calls': 40},
            'review_reserve': {'tokens': 100000, 'tool_calls': 12}}
    response = _run(http.post('/api/v4/evolution/programs', headers=headers, json=body))
    assert response.status_code == 201
    program = _run(response.get_json())['program']
    assert program['execution_options']['review_reserve'] == body['review_reserve']
    assert program['status'] == 'ready' and program['current'] is None
    response = _run(http.get('/api/v4/evolution/programs/' + program['program_id'], headers=headers))
    assert response.status_code == 200
    assert _run(response.get_json())['program']['execution_options']['review_reserve'] == body['review_reserve']
    response = _run(http.post('/api/v4/evolution/programs', headers=headers,
                             json={**body, 'review_reserve': {'tokens': 100001, 'tool_calls': 12}}))
    assert response.status_code == 409


def test_impossible_review_allocation_is_a_contract_error_before_creation(client):
    http, token, store = client
    before = store.writes
    response = _run(http.post('/api/v4/evolution/programs',
        headers={'Authorization': f'Bearer {token}', 'Idempotency-Key': 'http-impossible-review'},
        json={'objective': 'Keep the original budget.', 'budget': {'max_tokens': 100},
              'review_reserve': {'tokens': 100}}))
    assert response.status_code == 400 and store.writes == before
