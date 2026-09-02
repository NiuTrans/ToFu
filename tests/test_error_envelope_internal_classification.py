"""Regression: a Python programming-error builtin (our own code bug) must be
classified as ``internal`` — NOT ``generic`` with the misleading
"Settings → Keys / 429 quota" hint.

Root cause (conv mrova3t92jffm7)
--------------------------------
A deterministic internal ``TypeError``
(``_ContentWithDisplayResults.__new__() missing 1 required positional
argument: 'display_results'``) reached the LLM-fallback error path. Because
``_classify_exception`` had no branch for programming-error builtins, it fell
through to ``generic`` — whose hint tells the user to "check Settings → Keys,
re-enable a 429-disabled key, or switch model / provider". That sent the user
chasing a non-existent quota problem for what was purely our bug.

The fix routes ``TypeError``/``AttributeError``/``KeyError``/… to the already
defined ``internal`` kind (hint: "check the server logs"). Dispatch-layer
``RuntimeError`` / bare ``Exception`` string-shaped errors are deliberately
NOT swept in — the substring heuristics must still classify them.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestInternalErrorClassification:

    def test_typeerror_classifies_internal(self):
        from lib.error_envelope._classify import _classify_exception
        exc = TypeError(
            "_ContentWithDisplayResults.__new__() missing 1 required "
            "positional argument: 'display_results'")
        assert _classify_exception(exc) == 'internal'

    @pytest.mark.parametrize('exc', [
        AttributeError("'NoneType' object has no attribute 'x'"),
        KeyError('missing'),
        IndexError('list index out of range'),
        NameError("name 'foo' is not defined"),
        UnboundLocalError('local var referenced before assignment'),
        ValueError('bad value'),
        AssertionError('invariant broke'),
        ZeroDivisionError('division by zero'),
    ])
    def test_programming_builtins_classify_internal(self, exc):
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(exc) == 'internal'

    @pytest.mark.parametrize('exc', [
        ValueError('HTTP 429 too many requests'),
        KeyError('upstream timed out'),
        AssertionError('403 forbidden'),
    ])
    def test_programming_type_outranks_incidental_provider_text(self, exc):
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(exc) == 'internal'

    def test_internal_envelope_hint_is_logs_not_quota(self):
        """The user-facing hint must point at server logs, NOT the
        Settings→Keys / 429 quota advice."""
        from lib.error_envelope import from_exception
        env = from_exception(
            TypeError('missing 1 required positional argument'),
            model='aws.claude-opus-4.7',
            context='both-failed (opus-4.8→opus-4.7)',
            source='llm-fallback')
        assert env['kind'] == 'internal'
        assert 'logs/error.log' in env['hint']
        assert 'Keys / Providers' not in env['hint']
        assert '429' not in env['hint']

    def test_neuter_dispatch_string_errors_still_classify(self):
        """NEUTER: the new branch must NOT swallow dispatch-layer errors that
        the substring heuristics own. A RuntimeError carrying a rate-limit /
        timeout / unreachable message must keep its specific kind, and a
        generic RuntimeError stays 'generic' (not 'internal')."""
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(RuntimeError('HTTP 429 too many requests')) == 'ratelimit'
        assert _classify_exception(RuntimeError('read timed out')) == 'timeout'
        assert _classify_exception(RuntimeError('all dispatch attempts failed')) == 'dispatch_exhausted'
        # A bare RuntimeError with no recognised substring is NOT a leaf
        # programming-defect builtin → stays generic (dispatch owns it).
        assert _classify_exception(RuntimeError('something opaque upstream')) == 'generic'

    @pytest.mark.parametrize('message', [
        'API HTTP 429: {"error":{"code":"insufficient_quota"}}',
        'HTTP 429: You exceeded your current quota, check billing details',
        'HTTP 403 credit_balance_too_low',
        'HTTP 403: insufficient balance',
    ])
    def test_strong_billing_markers_outrank_http_status(self, message):
        from lib.error_envelope._classify import _classify_exception
        assert _classify_exception(RuntimeError(message)) == 'quota'



@pytest.mark.unit
class TestVendorOutageClassification:
    """Regression: an upstream-VENDOR outage must surface as
    ``upstream_error`` (warning, retryable, 'not your keys') — never as
    per-key ``ratelimit`` / ``permission`` or opaque keys-first ``generic``.

    Root cause (2026-07-26, logs/error.log): the toio gateway wraps vendor
    transients in 4xx bodies (``ext.error.source=UPSTREAM_VENDOR``). The
    raise layer (lib/llm_errors.py) already re-raises them as
    ``RateLimitError(is_gateway=True)`` / ``RetryableAPIError`` so dispatch
    rotates slots — but the DISPLAY classifier mapped a gateway-RL to
    ``ratelimit`` (Settings→Keys hint) and a 5xx-after-retries fell to the
    string heuristics' ``generic`` — both sent the user chasing keys for a
    vendor-side outage. ``BadRequestError`` (deterministic HTTP-400 payload
    rejection) likewise fell through to ``generic``'s keys-first hint.
    """

    def test_gateway_ratelimit_is_upstream_error_not_ratelimit(self):
        from lib.error_envelope._classify import _classify_exception
        from lib.llm import RateLimitError
        exc = RateLimitError('SSE error: {"error":{"message":"请求失败，请稍后再尝试"}}',
                             is_gateway=True, reason='HTTP 403: …')
        assert _classify_exception(exc) == 'upstream_error'

    def test_real_429_and_auth_not_swallowed_by_gateway_branch(self):
        """NEUTER: per-key 429 / quota / real auth failures keep their kinds."""
        from lib.error_envelope._classify import _classify_exception
        from lib.llm import PermissionError_, RateLimitError
        assert _classify_exception(RateLimitError('HTTP 429 too many requests')) == 'ratelimit'
        assert _classify_exception(RateLimitError('insufficient_quota', is_quota=True)) == 'quota'
        assert _classify_exception(PermissionError_('HTTP 403 invalid api key')) == 'permission'

    def test_retryable_api_error_is_upstream_error(self):
        from lib.error_envelope._classify import _classify_exception
        from lib.llm import RetryableAPIError
        assert _classify_exception(
            RetryableAPIError('SSE error: gateway exploded', status_code=503)) == 'upstream_error'

    def test_bad_request_has_own_kind(self):
        from lib.error_envelope._classify import _classify_exception
        from lib.llm import BadRequestError
        assert _classify_exception(
            BadRequestError('API HTTP 400: invalid_request_error: messages.0.content')) == 'bad_request'

    def test_upstream_error_envelope_retryable_no_keys_misdirection(self):
        from lib.error_envelope import from_exception
        from lib.llm import RateLimitError
        env = from_exception(
            RateLimitError('SSE error: …', is_gateway=True, reason='HTTP 502: …'),
            model='yuju-claude-opus-5-evaDaily', context='', source='llm-stream')
        assert env['kind'] == 'upstream_error'
        assert env['severity'] == 'warning'
        assert env['retryable'] is True
        assert 'Keys / Providers' not in env['hint']
        assert '稍后重试' in env['hint']

    def test_bad_request_envelope_explicitly_not_keys(self):
        from lib.error_envelope import from_exception
        from lib.llm import BadRequestError
        env = from_exception(BadRequestError('API HTTP 400: invalid payload'),
                             model='kimi-k3', context='', source='llm-stream')
        assert env['kind'] == 'bad_request'
        assert env['severity'] == 'error'
        assert env['retryable'] is False
        assert '这不是 Key / 配额 / 429 问题' in env['hint']

    def test_generic_hint_detail_first_not_keys_first(self):
        """The generic kind's hint must open with 'expand the error detail /
        check logs', offering Settings→Keys only as a conditional last
        resort — the old keys-first phrasing manufactured quota chases."""
        from lib.error_envelope import from_exception
        env = from_exception(RuntimeError('something opaque upstream'),
                             model='kimi-k3', context='', source='llm-stream')
        assert env['kind'] == 'generic'
        assert '展开下方错误详情' in env['hint']
        # Keys advice, when present, must come AFTER the logs guidance.
        assert env['hint'].index('logs/error.log') < env['hint'].index('Keys / Providers')
