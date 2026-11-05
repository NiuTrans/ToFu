"""Query-parse 400s stay client faults; faults behind the parse are 500s.

A malformed numeric or enumerated query value is the caller's fault and keeps
its authored 400. A builtin exception raised by the service or by owner
resolution behind that parse is a server fault: it reaches the logged,
redacted 500 boundary instead of being reported back as a bad request that
carries its raw text.
"""

from __future__ import annotations

import importlib
import json

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

_INTERNAL_DETAIL = 'tenant row metadata is corrupt at /srv/private/state.db'


def _raise_internal(*args, **kwargs):
    raise ValueError(_INTERNAL_DETAIL)


def _problem(response):
    return json.loads(response.get_data(as_text=True))


def _record_user_list(monkeypatch):
    import routes.api_v1.users as users_route

    calls = []
    monkeypatch.setattr(users_route, 'list_users',
                        lambda **kwargs: calls.append(kwargs) or [])
    return calls


def test_user_list_service_fault_is_a_redacted_500(flask_client, monkeypatch):
    import routes.api_v1.users as users_route

    monkeypatch.setattr(users_route, 'list_users', _raise_internal)
    response = flask_client.get('/api/v4/users?status=active')

    assert response.status_code == 500
    assert _INTERNAL_DETAIL not in response.get_data(as_text=True)


def test_user_list_unknown_status_is_rejected_before_the_service(
        flask_client, monkeypatch):
    calls = _record_user_list(monkeypatch)
    response = flask_client.get('/api/v4/users?status=bogus')

    assert response.status_code == 400
    assert _problem(response)['detail'] == "Invalid status: 'bogus'"
    assert calls == []


@pytest.mark.parametrize('query', ['limit=many', 'offset=1.5'])
def test_user_list_malformed_paging_keeps_its_400(
        flask_client, monkeypatch, query):
    calls = _record_user_list(monkeypatch)
    response = flask_client.get('/api/v4/users?' + query)

    assert response.status_code == 400
    assert _problem(response)['detail'] == 'limit/offset must be integers'
    assert calls == []


def test_user_list_known_status_reaches_the_service(flask_client, monkeypatch):
    calls = _record_user_list(monkeypatch)
    response = flask_client.get('/api/v4/users?status=suspended&limit=5&offset=2')

    assert response.status_code == 200
    assert calls == [{'limit': 5, 'offset': 2, 'status': 'suspended'}]


_RESEARCH_PAGES = [
    ('lib.research.action_observations', 'load_action_observation_page',
     'observations'),
    ('lib.research.action_observations', 'load_action_effect_page', 'effects'),
    ('lib.research.model_accounting', 'load_model_calls_page', 'model-calls'),
]


def _record_research_page(monkeypatch, module, function):
    calls = []
    monkeypatch.setattr(importlib.import_module(module), function,
                        lambda *args, **kwargs: calls.append(kwargs) or {})
    return calls


@pytest.mark.parametrize(('module', 'function', 'page'), _RESEARCH_PAGES)
def test_research_page_owner_resolution_fault_is_a_redacted_500(
        flask_client, monkeypatch, module, function, page):
    import routes.api_v1.research as research_route

    calls = _record_research_page(monkeypatch, module, function)
    monkeypatch.setattr(research_route, 'request_user_id', _raise_internal)
    response = flask_client.get(
        f'/api/v4/research/actions/research_action_observed/{page}')

    assert response.status_code == 500
    assert _INTERNAL_DETAIL not in response.get_data(as_text=True)
    assert calls == []


@pytest.mark.parametrize(('module', 'function', 'page'), _RESEARCH_PAGES)
@pytest.mark.parametrize('limit', ['bad', '1.5', ''])
def test_research_page_malformed_limit_keeps_its_400(
        flask_client, monkeypatch, module, function, page, limit):
    calls = _record_research_page(monkeypatch, module, function)
    response = flask_client.get(
        f'/api/v4/research/actions/research_action_observed/{page}',
        query_string={'limit': limit})

    assert response.status_code == 400
    assert calls == []
