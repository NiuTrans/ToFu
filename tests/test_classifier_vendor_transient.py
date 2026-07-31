#!/usr/bin/env python3
"""tests/test_classifier_vendor_transient.py — classifier-level guards for the
2026-07-26 yuju claude-opus-5 vendor-4xx storm (pt_48f29db9).

Companion to tests/test_vendor_transient_dispatch.py (which pins the dispatch
-layer behaviour with hand-raised exceptions). This suite pins the SOURCE of
those exception types — ``_classify_http_error`` branch ORDER and the
transient-pattern boundary:

  1. Transient-worded 4xx ("请求失败,请稍后再尝试" / "try again later" /
     "temporarily unavailable" / "overloaded") → RateLimitError(is_gateway=
     True) carrying the REAL status — never PermissionError_ (auth) and
     never BadRequestError (deterministic).
  2. Deterministic residual 400 (vendor invalid_request — e.g. "signature:
     Field required") → BadRequestError, so dispatch releases the slot
     instead of feeding the 300s consecutive-error lockout.
  3. The transient pattern list stays CONSERVATIVE: bare "try again" (no
     "later") must NOT match — a deterministic "invalid …, try again" body
     is still BadRequestError.
  4. The pre-existing specific 400 shapes (token-limit / image / prompt-
     too-long / stream-only) and 429/402 quota classification are unchanged.
  5. The retry-HUD reasonKeys the dispatcher emits ('Upstream error' /
     'Waiting for model (retry backoff)') have i18n strings — otherwise the
     missing-translation tripwire fires in production.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_classifier_vendor_transient.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_VENDOR_TRANSIENT_ZH = '{"error":{"message":"请求失败,请稍后再尝试"},' \
                       '"ext":{"error":{"source":"UPSTREAM_VENDOR"}}}'


@pytest.mark.unit
class TestTransient4xxReclassification:

    def test_transient_400_becomes_gateway_ratelimit(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, _VENDOR_TRANSIENT_ZH, 'm', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 400, 'the REAL status must ride along'

    def test_transient_403_is_not_auth(self):
        """The incident shape: vendor outage wrapped as 403 — must NOT be
        treated as PermissionError_ (pair exclusion as auth failure)."""
        from lib.llm_errors import PermissionError_, RateLimitError, _classify_http_error
        try:
            _classify_http_error(403, _VENDOR_TRANSIENT_ZH, 'm', '[t]')
        except PermissionError_:
            pytest.fail('a vendor-transient 403 was misclassified as an auth failure')
        except RateLimitError as e:
            assert e.is_gateway is True and e.status_code == 403

    @pytest.mark.parametrize('phrase', ['try again later',
                                        'temporarily unavailable',
                                        'overloaded'])
    def test_transient_english_phrases(self, phrase):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, f'{{"error":{{"message":"{phrase}"}}}}', 'm', '[t]')
        assert ei.value.is_gateway is True


@pytest.mark.unit
class TestDeterministic400Boundary:

    def test_signature_400_is_bad_request(self):
        """The epic's original poison: deterministic vendor rejection."""
        from lib.llm_errors import BadRequestError, RateLimitError, _classify_http_error
        try:
            _classify_http_error(
                400, '{"error":{"type":"invalid_request_error",'
                     '"message":"messages.1.content.0.signature: Field required"}}',
                'm', '[t]')
        except RateLimitError:
            pytest.fail('a deterministic 400 was misclassified as transient — '
                        'it would rotate keys forever with a poisoned payload')
        except BadRequestError:
            pass  # the release-the-slot branch

    def test_bare_try_again_does_not_match_transient(self):
        """Pattern conservatism: 'try again' WITHOUT 'later' is not proof of
        a transient — deterministic invalid bodies stay BadRequestError."""
        from lib.llm_errors import BadRequestError, _classify_http_error
        with pytest.raises(BadRequestError):
            _classify_http_error(400, 'invalid request shape, please try again',
                                 'm', '[t]')

    def test_specific_400_shapes_unchanged(self):
        from lib.llm_errors import (InvalidImageError, PromptTooLongError,
                                    StreamOnlyError, _classify_http_error)
        with pytest.raises(PromptTooLongError):
            _classify_http_error(400, 'prompt is too long', 'm', '[t]')
        with pytest.raises(InvalidImageError):
            _classify_http_error(400, 'invalid image content', 'm', '[t]')
        with pytest.raises(StreamOnlyError):
            _classify_http_error(400, 'this model only support stream mode', 'm', '[t]')

    def test_unknown_status_still_generic(self):
        from lib.llm_errors import BadRequestError, _classify_http_error
        with pytest.raises(Exception) as ei:
            _classify_http_error(418, 'teapot', 'm', '[t]')
        assert not isinstance(ei.value, BadRequestError)


@pytest.mark.unit
class TestStatusCodeStamps:

    def test_429_stamps_429(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(429, 'rate limit exceeded', 'm', '[t]')
        assert ei.value.status_code == 429
        assert ei.value.is_gateway is False

    def test_quota_429_still_quota(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(429, 'insufficient_quota', 'm', '[t]')
        assert ei.value.is_quota is True
        assert ei.value.is_gateway is False

    @pytest.mark.parametrize('code', [502, 503, 504])
    def test_gateway_throttle_stamps_real_code(self, code):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(code, 'upstream down', 'm', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == code


@pytest.mark.unit
class TestRetryLabelI18nCoverage:
    """The two reasonKeys the dispatcher emits must have i18n strings, or the
    missing-translation tripwire (_reportMissingTranslation) fires in prod."""

    def test_reasonkey_mapping_exists(self):
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        assert RETRY_REASON_KEYS.get('Upstream error') == 'stream.retryReason.upstreamError'
        assert RETRY_REASON_KEYS.get('Waiting for model (retry backoff)') == \
            'stream.retryReason.waitingBackoff'

    def test_i18n_strings_exist(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / 'static' / 'js' / 'i18n.js').read_text(encoding='utf-8')
        assert "'stream.retryReason.upstreamError'" in src
        assert "'stream.retryReason.waitingBackoff'" in src

    def test_retry_phase_fields_maps_labels(self):
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        f = retry_phase_fields(model='m', attempt=1, reason='Upstream error',
                               status_code=403)
        assert f['detailKey'] == 'stream.phase.retryReason'
        assert f['detailArgs']['reasonKey'] == 'stream.retryReason.upstreamError'
        f = retry_phase_fields(model='m', attempt=1,
                               reason='Waiting for model (retry backoff)',
                               status_code=0)
        assert f['detailArgs']['reasonKey'] == 'stream.retryReason.waitingBackoff'
        # regression pin: a real 429 status still wins the rate-limited branch
        f = retry_phase_fields(model='m', attempt=1, reason='x', status_code=429)
        assert f['detailKey'] == 'stream.phase.retryRateLimited'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
