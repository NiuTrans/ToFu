"""Regression: an OAuth-subscription session expiry must classify as
``oauth_reauth`` — an actionable "re-login" — NOT ``generic`` with the
"check the server logs" dead-end hint.

Root cause (2026-09-23 incident)
--------------------------------
A Codex ``refresh_token_invalidated`` left the subscription logged out. The
catalogue/usage refresh raised ``RuntimeError('Codex subscription not logged
in (no valid OAuth token)')`` / ``'... refresh is unavailable; sign-in
required'``. None of those phrases contain "401"/"unauthorized", so
``_classify_exception`` fell through to ``generic`` — whose hint sends the
user to ``logs/error.log``. The user got an opaque 500 + "check the logs"
for a problem whose entire recovery is "open Settings → Subscription
sign-in". The ``oauth_reauth`` kind matches the real sign-in phrasing and
carries a re-login hint (never a key/quota misdirection).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestOauthReauthClassification:

    @pytest.mark.parametrize('msg', [
        'Codex subscription not logged in (no valid OAuth token)',
        'Access token expired and refresh is unavailable; sign-in required',
        'Codex OAuth token expired; sign in again',
        'refresh_invalidated: refresh_token_invalidated',
        'no valid oauth token stored',
    ])
    def test_session_expiry_phrases_classify_oauth_reauth(self, msg):
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(RuntimeError(msg)) == 'oauth_reauth'

    @pytest.mark.parametrize('msg,expected', [
        # Unrelated failures must NOT be swept into oauth_reauth.
        ('HTTP 429 too many requests', 'ratelimit'),
        ('HTTP 401 unauthorized', 'permission'),
        ('model-routing authority is temporarily unavailable', 'generic'),
    ])
    def test_no_false_positives(self, msg, expected):
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(RuntimeError(msg)) == expected

    def test_kind_is_registered_and_has_hint(self):
        from lib.error_envelope._constants import KINDS, _TITLES, _WARNING_KINDS
        assert 'oauth_reauth' in KINDS
        assert 'oauth_reauth' in _WARNING_KINDS
        cn_title, en_title, cn_hint, en_hint = _TITLES['oauth_reauth']
        for text in (en_hint, cn_hint):
            assert 'sign' in text.lower() or '登录' in text
            assert 'logs/error.log' not in text

    def test_envelope_builds_with_reauth_kind_and_hint(self):
        from lib.error_envelope import from_exception
        env = from_exception(
            RuntimeError('Codex subscription not logged in (no valid OAuth token)'),
            model='gpt-6-astra')
        assert env['kind'] == 'oauth_reauth'
        assert env['severity'] == 'warning'
        assert env['retryable'] is False


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_missing_subscription_survives_request_preparation(monkeypatch, provider):
    from lib.error_envelope import from_exception
    from lib.llm_errors import LocalRequestPreparationError, SubscriptionLoginRequiredError
    from lib.oauth.outbound import resolve_oauth_request
    monkeypatch.setattr(f'lib.oauth.{provider}.{provider}_get_valid_token', lambda **kwargs: None)
    try:
        try:
            resolve_oauth_request(provider, {}, None, user_id='1')
        except Exception as cause:
            assert isinstance(cause, SubscriptionLoginRequiredError)
            raise LocalRequestPreparationError(f'local request preparation failed: {cause}') from cause
    except LocalRequestPreparationError as error:
        env = from_exception(error, model='gpt-6.1-sol')
        assert env['kind'] == 'oauth_reauth'
        assert env['retryable'] is False
        assert env['severity'] == 'warning'
        assert error.provider_reached is False
        assert 'logs/error.log' not in env['hint']


def test_preparation_programming_error_keeps_internal_guidance():
    from lib.error_envelope import from_exception
    from lib.llm_errors import LocalRequestPreparationError
    error = LocalRequestPreparationError('synthetic body failure')
    error.__cause__ = ValueError('Codex subscription not logged in')
    assert from_exception(error)['kind'] == 'internal'
