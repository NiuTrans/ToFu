"""Identity normalization preserves an exact owner and never rounds a number."""

import asyncio
from decimal import Decimal
from enum import IntEnum
from fractions import Fraction
from types import SimpleNamespace

import pytest
from quart import Quart

from lib.api_keys import AuthContext
from lib.identity import PrincipalContext, principal_from_auth_context, require_user_id

pytestmark = pytest.mark.unit


class Owner(IntEnum):
    SEVEN = 7


class IntegerWithConversion(int):
    def __int__(self):
        return 19


class TextWithConversion(str):
    def __int__(self):
        return 19


class NumericObject:
    def __int__(self):
        return 7


@pytest.mark.parametrize('value,expected', [
    (1, 1), (7, 7), ('7', 7), (' +007 ', 7), ('1_007', 1007),
    ('٧', 7), (2**128, 2**128), (str(2**128), 2**128),
    (Owner.SEVEN, 7), (IntegerWithConversion(7), 7), (TextWithConversion('7'), 7),
])
def test_owner_identity_uses_exact_integer_or_existing_integer_text(value, expected):
    normalized = require_user_id(value, context='test owner')
    assert type(normalized) is int and normalized == expected


INVALID = [
    2.9, 2.0, float('nan'), float('inf'), float('-inf'),
    Decimal('2.9'), Decimal('2'), Fraction(5, 2), Fraction(2, 1),
    True, False, None, 0, -1, '', '2.9', '2e1', 'usr_7',
    b'7', bytearray(b'7'), memoryview(b'7'), [], {}, NumericObject(),
]


def _principal(value):
    return PrincipalContext.user(subject_id='key', owner_user_id=value)


def _restored_principal(value):
    return PrincipalContext.from_payload(dict(
        kind='user', subject_id='key', owner_user_id=value, tenant_id=None, scopes=[]))


def _authentication(value):
    return AuthContext(key_id='key', account_user_id='usr_account', owner_user_id=value)


@pytest.mark.parametrize('factory', [require_user_id, _principal, _restored_principal, _authentication])
@pytest.mark.parametrize('value', INVALID)
def test_invalid_owner_never_becomes_another_identity(factory, value):
    with pytest.raises(ValueError):
        factory(value)


@pytest.mark.parametrize('value', [2.9, 2.0, float('inf'), b'2'])
def test_request_binding_rejects_non_integer_owner(value):
    context = SimpleNamespace(key_id='key', owner_user_id=value, scopes={'chat'})
    with pytest.raises(ValueError):
        principal_from_auth_context(context, allow_personal_owner=False)


def test_invalid_owner_never_reaches_a_credential_query(monkeypatch):
    from lib.api_keys import _crud

    calls = []
    class Client:
        def query(self, *args, **kwargs):
            calls.append((args, kwargs))
            return None

    monkeypatch.setattr(_crud, 'get_storage_client', lambda: Client())
    with pytest.raises(ValueError):
        _crud.get_key_by_id('credential', owner_user_id=2.9)
    assert not calls


def test_task_admission_does_not_round_the_requested_owner():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('exact-owner-test')
    with pytest.raises(ValueError):
        runtime.create(user_id=2.9)


@pytest.mark.parametrize('owner', [2.9, 2.0, float('inf')])
def test_http_gate_refuses_a_malformed_authenticated_owner(monkeypatch, owner):
    from routes.api_v1 import auth

    context = AuthContext(key_id='key', name='key', owner_user_id=2,
                          scopes=frozenset({'chat'}))
    context.owner_user_id = owner
    monkeypatch.setattr(auth, '_mode_requires_credential', lambda: True)
    monkeypatch.setattr(auth, 'validate_token', lambda _token: context)
    monkeypatch.setattr(auth, 'audit_log', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth, 'record_usage', lambda *_args, **_kwargs: None)
    reached = []
    app = Quart(__name__)
    app.before_request(auth.auth_before_request)

    @app.get('/api/owner-boundary')
    async def owned_resource():
        reached.append(True)
        return {'owner': auth.request_user_id()}

    async def request():
        response = await app.test_client().get('/api/owner-boundary', headers={
            'Authorization': 'Bearer tofu_live_owner'})
        assert response.status_code == 403

    asyncio.run(request())
    assert not reached
