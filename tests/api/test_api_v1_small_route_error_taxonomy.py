"""Error taxonomy for small application routes.

Only the typed client fault answers 400; an unexpected builtin failure is a
redacted 500. The service functions are patched at the route boundary, so no
credential, auth-mode or host configuration is read or written.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize(('module', 'function', 'method', 'path', 'body'), [
    ('lib.credentials_vault', 'set_entry', 'post', '/api/v4/credentials',
     {'name': 'github_token', 'value': 'not-a-real-token'}),
    ('lib.private_hosts', 'upsert_host', 'post', '/api/v4/private-hosts',
     {'host': 'good.example.com'}),
    ('lib.auth_sources', 'upsert_source', 'post', '/api/v4/auth-sources',
     {'domain': 'good.example.com'}),
])
def test_internal_service_fault_is_not_a_client_400(
    flask_client, monkeypatch, module, function, method, path, body,
):
    import importlib

    service = importlib.import_module(module)
    monkeypatch.setattr(service, function,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            TypeError('service wiring is broken')))
    response = getattr(flask_client, method)(path, json=body)
    assert response.status_code == 500
    assert 'service wiring is broken' not in response.get_data(as_text=True)


@pytest.mark.parametrize(('module', 'function', 'method', 'path', 'body', 'message'), [
    ('lib.credentials_vault', 'set_entry', 'post', '/api/v4/credentials',
     {'name': 'github_token', 'value': 'not-a-real-token'}, 'value is required'),
    ('lib.private_hosts', 'upsert_host', 'post', '/api/v4/private-hosts',
     {'host': 'good.example.com'}, 'host is required'),
    ('lib.auth_sources', 'upsert_source', 'post', '/api/v4/auth-sources',
     {'domain': 'good.example.com'}, 'domain is required'),
])
def test_typed_client_fault_keeps_400(
    flask_client, monkeypatch, module, function, method, path, body, message,
):
    import importlib
    from lib.error_envelope import BadRequest

    service = importlib.import_module(module)
    monkeypatch.setattr(service, function,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            BadRequest(message)))
    response = getattr(flask_client, method)(path, json=body)
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
