"""Settings auth-health projection: expired/burned tokens must not read green."""

from __future__ import annotations

import time
from unittest import mock

import pytest

from lib.oauth.manager import _flow

pytestmark = pytest.mark.unit


def _status_with_token(token):
    with mock.patch('lib.oauth.token_store.load_token',
                    return_value=token), \
         mock.patch.object(_flow, '_active_flows', {}), \
         mock.patch('lib.oauth.outbound.managed_oauth_provider_status',
                    return_value={'provider_ready': True,
                                  'provider_id': 'oauth_codex',
                                  'model_ids': ['gpt-6-astra'],
                                  'model_count': 1}):
        return _flow.get_oauth_status('codex', owner_user_id=1)


def test_live_token_is_authenticated():
    token = {'access_token': 'tok', 'refresh_token': 'rt',
             'expire': time.time() + 3600, 'email': 'a@b.c'}
    status = _status_with_token(token)
    assert status['authenticated'] is True
    assert status['auth_issue'] == ''
    assert status['access_token_expired'] is False


def test_expired_access_token_with_refresh_still_authenticated():
    token = {'access_token': 'tok', 'refresh_token': 'rt',
             'expire': time.time() - 10}
    status = _status_with_token(token)
    assert status['authenticated'] is True
    assert status['access_token_expired'] is True
    assert status['auth_issue'] == 'access_expired'


def test_expired_access_token_without_refresh_is_not_authenticated():
    # The exact failure from the incident: refresh burned, access_token kept.
    token = {'access_token': 'tok', 'refresh_token': '',
             'expire': time.time() - 10}
    status = _status_with_token(token)
    assert status['authenticated'] is False
    assert status['auth_issue'] == 'access_expired_no_refresh'
    assert 'sign in' in (status['error'] or '').lower()


def test_terminal_refresh_rejection_is_not_authenticated():
    token = {'access_token': 'tok', 'refresh_token': '',
             'expire': time.time() + 3600,
             'refresh_invalidated_at': time.time(),
             'refresh_invalidated_reason': 'refresh_token_invalidated'}
    status = _status_with_token(token)
    assert status['authenticated'] is False
    assert status['refresh_invalidated'] is True
    assert status['auth_issue'] == 'refresh_invalidated'
    assert 'sign in' in (status['error'] or '').lower()
