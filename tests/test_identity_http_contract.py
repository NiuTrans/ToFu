"""HTTP identity contract from account login to repository owner binding."""

from __future__ import annotations

import asyncio

import pytest
from quart import Quart

pytest_plugins = ('tests._billing_user_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]


def _app() -> Quart:
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    from routes.api_v1.auth import (
        attach_rate_headers, bearer_auth_before_request,
    )
    from routes.api_v1.users import api_v1_users_bp

    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)
    app.register_blueprint(api_v1_users_bp)
    return app


def test_login_exposes_account_and_distinct_numeric_owner():
    from lib.billing.users import create_user

    account = create_user(
        'identity-http@example.com', password='correct-password')
    assert account.id.startswith('usr_')
    assert account.owner_user_id >= 2

    async def exercise():
        client = _app().test_client()
        login = await client.post('/api/v1/users/login', json={
            'email': account.email,
            'password': 'correct-password',
        })
        assert login.status_code == 200
        login_body = await login.get_json()
        token = login_body['token']

        me = await client.get(
            '/api/v1/users/me',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert me.status_code == 200
        me_body = await me.get_json()
        assert me_body['ownerId'] == account.owner_user_id
        assert me_body['user']['id'] == account.id
        assert me_body['user']['owner_id'] == account.owner_user_id
        assert str(me_body['ownerId']) != account.id

    asyncio.run(exercise())


def test_suspending_account_invalidates_all_bound_credentials():
    from lib.billing.users import create_user, set_user_status

    account = create_user(
        'identity-suspend@example.com', password='correct-password')

    async def exercise():
        client = _app().test_client()
        login = await client.post('/api/v1/users/login', json={
            'email': account.email,
            'password': 'correct-password',
        })
        token = (await login.get_json())['token']
        set_user_status(account.id, 'suspended')

        rejected = await client.get(
            '/api/v1/users/me',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert rejected.status_code == 401

    asyncio.run(exercise())
