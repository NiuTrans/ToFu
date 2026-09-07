#!/usr/bin/env python3
"""tests/test_llm_error_body_display.py — error-body decode + HUD display guard.

Two production display bugs from the 2026-07-25 toio-gateway 400 incident
(yuju-claude-opus-5-evaDaily, request id toio20260725131310115086679KWLlduaj):

  1. Mojibake — the sync ``requests`` path built the error string from
     ``resp.text``. For a ``text/*`` error page without an explicit charset
     requests falls back to ISO-8859-1 (RFC 2616 default), garbling the
     gateway's UTF-8 Chinese body into ``è¯·æ±...`` in BOTH logs and the
     frontend retry HUD (the async httpx path already decoded UTF-8).
  2. Raw-JSON HUD — the retry bubble / error card dumped the whole
     ``{"error":{"message":...}}`` envelope instead of the message text.

Pins ``decode_error_body`` (UTF-8-first decode) and ``summarize_error_body``
(envelope → clean message) plus the ``_classify_http_error`` integration:
exception messages are clean, while the RAW text still drives the pattern
matchers (quota / prompt-too-long / wrapped-overload).
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.llm_errors import (  # noqa: E402
    BadRequestError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    _ERR_BODY_LIMIT,
    _classify_http_error,
    _is_upstream_vendor_transient,
    decode_error_body,
    repair_mojibake,
    summarize_error_body,
)
import lib.llm_errors as _llm_errors  # noqa: E402

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ZH_MESSAGE = '请求失败，请稍后再尝试 (request id: toio20260725131310115086679KWLlduaj)'
_ENVELOPE = ('{"error":{"message":"' + _ZH_MESSAGE + '",'
             '"type":"toio_api_error","param":"","code":null,"status_code":400},'
             '"ext":{"error":{"source":"UPSTREAM_VENDOR","service":"claude-opus-5",'
             '"stage":"downstream_http","upstreamStatus":400}}}')


class _FakeResp:
    """Duck-typed stand-in for the bits of requests.Response the decoder reads."""

    def __init__(self, content, encoding=None, apparent_encoding=None):
        self.content = content
        self.encoding = encoding
        self.apparent_encoding = apparent_encoding


# ══════════════════════════════════════════════════════════
#  decode_error_body
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDecodeErrorBody:

    def test_latin1_declared_utf8_bytes_decodes_chinese(self):
        """THE incident shape: gateway UTF-8 body + latin-1 fallback encoding.

        requests' RFC-2616 default for text/* without charset is ISO-8859-1;
        resp.text then garbles CJK. decode_error_body must prefer UTF-8.
        """
        resp = _FakeResp(_ENVELOPE.encode('utf-8'), encoding='ISO-8859-1')
        out = decode_error_body(resp)
        assert _ZH_MESSAGE in out, (
            f'expected readable Chinese, got mojibake: {out[:80]}')
        # The exact mojibake string seen in production must NOT survive.
        assert 'è¯' not in out

    def test_no_encoding_utf8_bytes(self):
        resp = _FakeResp(_ENVELOPE.encode('utf-8'), encoding=None,
                         apparent_encoding='UTF-8')
        assert _ZH_MESSAGE in decode_error_body(resp)

    def test_declared_utf8(self):
        resp = _FakeResp(_ENVELOPE.encode('utf-8'), encoding='utf-8')
        assert _ZH_MESSAGE in decode_error_body(resp)

    def test_declared_exotic_charset_honored(self):
        body = '网关错误，稍后重试'.encode('gbk')
        resp = _FakeResp(body, encoding='gbk')
        assert decode_error_body(resp) == '网关错误，稍后重试'

    def test_broken_bytes_fall_back_without_raising(self):
        # Not valid UTF-8 (lone high bytes); apparent encoding guess wins.
        resp = _FakeResp(b'caf\xe9 au lait', encoding='ISO-8859-1',
                         apparent_encoding='windows-1252')
        out = decode_error_body(resp)
        assert isinstance(out, str) and out.startswith('caf')

    def test_empty_body(self):
        assert decode_error_body(_FakeResp(b'', encoding='ISO-8859-1')) == ''
        assert decode_error_body(_FakeResp(None, encoding=None)) == ''


# ══════════════════════════════════════════════════════════
#  summarize_error_body
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSummarizeErrorBody:

    def test_openai_envelope_with_api_http_prefix(self):
        raw = f'API HTTP 400: {_ENVELOPE}'
        out = summarize_error_body(raw)
        assert out == f'API HTTP 400: {_ZH_MESSAGE}'
        assert '{' not in out

    def test_bare_envelope(self):
        assert summarize_error_body(_ENVELOPE) == _ZH_MESSAGE

    def test_anthropic_envelope(self):
        raw = ('{"type":"error","error":{"type":"invalid_request_error",'
               '"message":"max_tokens exceeds the model limit: 8192"}}')
        assert summarize_error_body(raw) == 'max_tokens exceeds the model limit: 8192'

    def test_plain_text_passthrough(self):
        assert summarize_error_body('API HTTP 502: <html>Bad Gateway</html>') == \
            'API HTTP 502: <html>Bad Gateway</html>'

    def test_non_envelope_json_passthrough(self):
        # The sankuai 500-wrap shape carries signal in other fields — keep raw.
        raw = '{"status":500,"data":"No matching constant for [529]"}'
        assert summarize_error_body(raw) == raw

    def test_empty_and_none(self):
        assert summarize_error_body('') == ''
        assert summarize_error_body(None) == ''


# ══════════════════════════════════════════════════════════
#  _classify_http_error integration — clean raises, raw patterns
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestClassifyDisplayIntegration:

    def test_vendor_transient_400_escalates_as_gateway_retry(self):
        """The 2026-07-26 yuju opus-5 shape: HTTP 400 with "请稍后再尝试" is
        an UPSTREAM-VENDOR transient, not a client error — it must escalate
        to dispatch as gateway-class rotation, never the non-retryable
        round-killer that fed the 300s consecutive-error slot lockout."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, f'API HTTP 400: {_ENVELOPE}', 'm', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 400
        msg = str(ei.value)
        assert _ZH_MESSAGE in msg
        assert '{"error"' not in msg, f'raw JSON leaked into exception: {msg[:120]}'
        assert 'è¯' not in msg

    def test_prompt_too_long_still_detected_through_envelope(self):
        raw = ('API HTTP 400: {"error":{"message":"prompt is too long: '
               '432286 tokens exceeds the model limit"}}')
        with pytest.raises(PromptTooLongError):
            _classify_http_error(400, raw, 'm', '[t]')

    def test_quota_429_still_detected_through_envelope(self):
        raw = ('API HTTP 429: {"error":{"message":"You exceeded your current '
               'quota, check your plan and billing","code":"insufficient_quota"}}')
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(429, raw, 'm', '[t]')
        assert ei.value.is_quota is True

    def test_shared_project_wire_rejections_are_debug_only(self, monkeypatch):
        """Dispatcher owns bounded INFO records for a repeated family streak."""
        infos, debugs = [], []
        monkeypatch.setattr(
            'lib.llm_errors.logger.info', lambda *a, **k: infos.append(a))
        monkeypatch.setattr(
            'lib.llm_errors.logger.debug', lambda *a, **k: debugs.append(a))

        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(
                429,
                'API HTTP 429: request reached project (kimi-k3) TPM rate limit',
                'kimi-k3',
                '[t]',
            )

        assert ei.value.is_shared_contention is True
        assert infos == []
        assert len(debugs) == 1

    def test_plain_text_message_byte_preserved(self):
        """Non-envelope bodies must raise with the raw text unchanged —
        byte-parity with pre-fix behaviour for non-JSON gateways."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(502, 'API HTTP 502: <html>Bad Gateway</html>',
                                 'm', '[t]')
        assert 'API HTTP 502: <html>Bad Gateway</html>' in str(ei.value)

    def test_permission_error_clean_message(self):
        raw = 'API HTTP 403: {"error":{"message":"key 无权限访问该模型"}}'
        with pytest.raises(PermissionError_) as ei:
            _classify_http_error(403, raw, 'm', '[t]')
        assert str(ei.value) == 'API HTTP 403: key 无权限访问该模型'

    def test_classify_status_error_end_to_end(self):
        """The streaming entry point raises the clean message too — and the
        vendor-transient envelope escalates as gateway-class rotation."""
        from lib.llm._sse_core import classify_status_error

        class _Dumper:
            enabled = False

        with pytest.raises(RateLimitError) as ei:
            classify_status_error(400, _ENVELOPE, body={'model': 'm'},
                                  log_prefix='[t]', raw_dumper=_Dumper())
        assert ei.value.is_gateway is True
        assert str(ei.value) == f'API HTTP 400: {_ZH_MESSAGE}'


# ══════════════════════════════════════════════════════════
#  repair_mojibake — upstream DOUBLE-encoding (2026-07-26 incident)
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRepairMojibake:
    """The toio UPSTREAM_VENDOR wrap layer decodes the vendor's UTF-8 error
    text as latin-1 and re-encodes it as UTF-8, so a CORRECT UTF-8 decode on
    our side still yields mojibake (verified from raw log bytes: each real
    UTF-8 byte itself UTF-8-encoded). repair_mojibake reverses exactly one
    layer, conservatively."""

    def test_double_encoded_chinese_repaired(self):
        mojibake = _ZH_MESSAGE.encode('utf-8').decode('latin-1')
        assert '请求失败' not in mojibake  # sanity: it IS garbled
        assert repair_mojibake(mojibake) == _ZH_MESSAGE

    def test_decode_error_body_end_to_end_on_double_encoded_bytes(self):
        """The exact wire shape from the 17:10 error.log lines."""
        wire = _ENVELOPE.encode('utf-8').decode('latin-1').encode('utf-8')
        out = decode_error_body(_FakeResp(wire, encoding=None))
        assert _ZH_MESSAGE in out
        assert 'è¯' not in out

    def test_mojibake_with_ascii_suffix_repaired_suffix_intact(self):
        raw = '请求失败，请稍后重试 (request id: toio123)'
        mojibake = raw.encode('utf-8').decode('latin-1')
        out = repair_mojibake(mojibake)
        assert out == raw
        assert out.endswith('toio123)')

    def test_legit_latin1_text_never_repaired(self):
        # 'café' → latin-1 bytes are NOT valid UTF-8 → must pass through.
        assert repair_mojibake('café au lait') == 'café au lait'

    def test_proper_chinese_and_ascii_unchanged(self):
        assert repair_mojibake(_ZH_MESSAGE) == _ZH_MESSAGE
        assert repair_mojibake('plain ascii 500 Overloaded') == 'plain ascii 500 Overloaded'
        assert repair_mojibake('') == ''
        assert repair_mojibake(None) is None

    def test_repair_is_idempotent(self):
        once = repair_mojibake(_ZH_MESSAGE.encode('utf-8').decode('latin-1'))
        assert repair_mojibake(once) == once


# ══════════════════════════════════════════════════════════
#  Upstream-vendor transient 4xx classification + BadRequestError
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUpstreamTransientClassification:

    def test_predicate_matches_transient_phrases_only(self):
        assert _is_upstream_vendor_transient('API HTTP 403: 请求失败，请稍后再尝试')
        assert _is_upstream_vendor_transient('overloaded')
        assert not _is_upstream_vendor_transient('signature: Field required')
        assert not _is_upstream_vendor_transient('invalid api key')
        assert not _is_upstream_vendor_transient('')
        assert not _is_upstream_vendor_transient(None)

    def test_403_transient_is_gateway_retry_not_auth(self):
        """THE 17:10:37 incident line: HTTP 403 + 请稍后再尝试 must NOT be a
        PermissionError_ (which excluded the pair and poisoned slot health)."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, f'API HTTP 403: {_ENVELOPE}', 'm', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 403

    def test_401_transient_also_escalates(self):
        raw = 'API HTTP 401: {"error":{"message":"请稍后重试"}}'
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(401, raw, 'm', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 401

    def test_403_real_auth_error_stays_permission(self):
        """Dead keys are a real shape (pt_8f6cbc75) — an auth-phrased 403
        must keep the PermissionError_ path unchanged."""
        raw = 'API HTTP 403: {"error":{"message":"invalid api key"}}'
        with pytest.raises(PermissionError_):
            _classify_http_error(403, raw, 'm', '[t]')

    def test_missing_key_claim_with_wire_credential_is_gateway_anomaly(self):
        """A header-present request cannot prove key death by claiming the
        header is missing; preserve it as bounded gateway evidence."""
        raw = 'API HTTP 401: {"error":{"message":"missing api key"}}'
        with pytest.raises(RateLimitError) as raised:
            _classify_http_error(
                401, raw, 'kimi-k3', '[t]', credential_present=True)
        error = raised.value
        assert error.is_gateway is True
        assert error.is_credential_delivery_anomaly is True
        assert error.status_code == 401
        assert error.reason == 'credential_delivery_anomaly'

    def test_missing_key_claim_without_wire_credential_is_permission(self):
        raw = 'API HTTP 401: missing api key'
        with pytest.raises(PermissionError_):
            _classify_http_error(
                401, raw, 'kimi-k3', '[t]', credential_present=False)

    def test_invalid_key_stays_permission_even_when_header_was_sent(self):
        raw = 'API HTTP 401: invalid api key provided'
        with pytest.raises(PermissionError_):
            _classify_http_error(
                401, raw, 'kimi-k3', '[t]', credential_present=True)

    def test_wire_credential_evidence_reads_final_header_values_only(self):
        assert _llm_errors._has_outbound_credential({
            'Content-Type': 'application/json',
            'Authorization': 'Bearer configured',
        })
        assert _llm_errors._has_outbound_credential({
            'x-api-key': 'configured',
        })
        assert not _llm_errors._has_outbound_credential({
            'Authorization': '',
            'Content-Type': 'application/json',
        })
        assert not _llm_errors._has_outbound_credential({
            'Authorization': 'Bearer   ',
        })
        assert not _llm_errors._has_outbound_credential({
            'Authorization': 'Bearer configured',
            'authorization': '',
        })

    def test_stream_request_plan_carries_boolean_wire_evidence(self):
        from lib.llm._sse_core import prepare_request

        body = {
            'model': 'unit-model',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 8,
            'stream': True,
        }
        plan = prepare_request(
            body,
            api_key='unit-test-key',
            base_url='https://example.invalid/v1',
        )
        overridden = prepare_request(
            body,
            api_key='unit-test-key',
            base_url='https://example.invalid/v1',
            extra_headers={'authorization': ''},
        )

        assert plan.credential_present is True
        assert overridden.credential_present is False

    def test_400_deterministic_rejection_raises_bad_request(self):
        """signature: Field required — deterministic payload rejection. NOT
        a transient (no retry-later phrasing), NOT slot poison: typed
        BadRequestError so dispatch releases the slot instead of feeding
        consecutive_errors."""
        raw = ('API HTTP 400: {"error":{"message":"Invalid request: '
               'signature: Field required","type":"invalid_request_error"}}')
        with pytest.raises(BadRequestError) as ei:
            _classify_http_error(400, raw, 'm', '[t]')
        assert 'signature: Field required' in str(ei.value)
        assert '{"error"' not in str(ei.value)

    def test_400_specific_matchers_still_win(self):
        """prompt-too-long / image / stream-only must keep their typed paths —
        the transient check runs AFTER them."""
        raw = 'API HTTP 400: {"error":{"message":"prompt is too long: 500k tokens"}}'
        with pytest.raises(PromptTooLongError):
            _classify_http_error(400, raw, 'm', '[t]')

    def test_status_code_carried_on_429_and_throttle(self):
        with pytest.raises(RateLimitError) as e429:
            _classify_http_error(429, 'API HTTP 429: slow down', 'm', '[t]')
        assert e429.value.status_code == 429
        assert e429.value.is_gateway is False
        with pytest.raises(RateLimitError) as e503:
            _classify_http_error(503, 'API HTTP 503: upstream', 'm', '[t]')
        assert e503.value.status_code == 503
        assert e503.value.is_gateway is True


# ══════════════════════════════════════════════════════════
#  Mojibake 403 transient classification — 2026-07-26 10:21 incident
# ══════════════════════════════════════════════════════════

# The exact production shape (task f8045792, round 41, logs/app.log.2026-07-26
# 10:21:09): the toio wrap layer double-encoded the Chinese message (each real
# UTF-8 byte re-encoded as UTF-8 via a cp1252 print layer), so every Chinese
# transient phrase missed and the 403 fell through to PermissionError_ — model
# fallback fired with reason "permission" while ext.error.source sat intact in
# the same body. 72h fallout: 34 permission-kind kimi fallbacks + 4 task deaths
# (07-25 21:42) wearing the bogus "API Key 被拒绝 → Settings→Keys" envelope.
_MOJI_ZH_MESSAGE = ('请求失败，请稍后再尝试 (request id: '
                    'toio20260726022045530344569oeWynb4k)'
                    ).encode('utf-8').decode('latin-1')


def _moji_envelope(*, status=403, source='UPSTREAM_VENDOR', extra_field=''):
    """Build the double-encoded production envelope (mojibake message)."""
    src = f'"source":"{source}","service":"claude-opus-5"' if source else ''
    ext = f'"ext":{{"error":{{{src},"stage":"downstream_http"{extra_field}}}}}' if source else ''
    comma = ',' if ext else ''
    return ('{"error":{"message":"' + _MOJI_ZH_MESSAGE + '",'
            '"type":"toio_api_error","param":"","code":null,'
            f'"status_code":{status}}}{comma}{ext}')


@pytest.mark.unit
class TestMojibake403Classification:
    """Double-encoded (mojibake) bodies must converge with decoded bodies on
    the vendor-transient branch — keyed on encoding-stable ASCII markers, with
    message text only as corroborating evidence."""

    def test_mojibake_403_with_vendor_marker_is_gateway_retry(self):
        """THE 07-26 10:21 production body. ext.error.source=UPSTREAM_VENDOR
        is ASCII and survives any message mojibake — the 403 must escalate to
        gateway-class rotation, never PermissionError_."""
        assert 'è¯' in _MOJI_ZH_MESSAGE  # sanity: the message IS garbled
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, f'API HTTP 403: {_moji_envelope()}',
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 403

    def test_mojibake_403_marker_survives_when_repair_refuses(self):
        """repair_mojibake is conservative BY DESIGN: a body mixing mojibake
        with any non-cp1252 char (here a proper-CJK note in ext) cannot
        round-trip and is returned unrepaired. The ASCII source marker is the
        ONLY layer that still classifies this shape — it is the neuter target:
        deleting the marker branch must turn this test red."""
        env = _moji_envelope(extra_field=',"note":"供应商抖动稍候"')
        # Pin the premise: repair really does refuse this mixed string.
        assert repair_mojibake(f'API HTTP 403: {env}') == f'API HTTP 403: {env}'
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, f'API HTTP 403: {env}',
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True

    def test_mojibake_403_without_marker_caught_by_repair_layer(self):
        """No ext tail at all (truncated/other-gateway shape): the mojibake-
        repaired message still carries the transient phrase — rotate."""
        env = _moji_envelope(source='')
        assert 'upstream_vendor' not in env.lower()
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, f'API HTTP 403: {env}', 'm', '[t]')
        assert ei.value.is_gateway is True

    def test_mojibake_bare_text_converges(self):
        """The qwen-plus bare-text shape (07-26 18:07: 'API HTTP 403: 请求失败，
        请稍后再尝试' — no JSON envelope) in its mojibake form must also
        converge to rotation, not auth."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, f'API HTTP 403: {_MOJI_ZH_MESSAGE}',
                                 'qwen-plus', '[t]')
        assert ei.value.is_gateway is True

    def test_toio_type_alone_does_not_flip_real_403(self):
        """toio_api_error is the gateway's GENERIC error type — a genuine
        auth 403 carries it too. Without the UPSTREAM_VENDOR source marker
        (and no transient phrasing) it must stay PermissionError_."""
        raw = ('API HTTP 403: {"error":{"message":"Forbidden: key has no '
               'access to this model","type":"toio_api_error","status_code":403}}')
        with pytest.raises(PermissionError_):
            _classify_http_error(403, raw, 'yuju-claude-opus-5-evaDaily', '[t]')

    def test_real_401_ascii_auth_stays_permission(self):
        raw = ('API HTTP 401: {"error":{"message":"Invalid API key provided",'
               '"type":"authentication_error"}}')
        with pytest.raises(PermissionError_):
            _classify_http_error(401, raw, 'yuju-claude-opus-5-evaDaily', '[t]')

    def test_mojibake_400_with_vendor_marker_is_gateway_retry(self):
        """The 400 branch shares the predicate — a double-encoded toio 400
        transient must rotate, not die as deterministic BadRequestError."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, f'API HTTP 400: {_moji_envelope(status=400)}',
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 400


# ══════════════════════════════════════════════════════════
#  Gateway routing-resolution transient — 2026-07-26 19:15–19:54 incident
# ══════════════════════════════════════════════════════════

# The exact production body (logs/app.log.2026-07-26, 22 hits in one 40-minute
# window 19:15–19:54, 10 of them misclassified PermissionError_ — e.g. task
# b4a01fd9 round 7 "LLM call failed" → task death / kimi fallback wearing the
# bogus "API Key 被拒绝 → Settings→Keys" envelope). The SAME keys then served
# 43 successful opus-5 rounds 19:55–21:20 — a real auth rejection does not
# heal in minutes. This is the gateway's own routing layer flapping during a
# vendor storm: a 503 wearing a 403 costume. Pure-ASCII phrasing, so it is
# encoding-stable and needs no mojibake handling.
_RESOLVE_GROUPS_403 = ('API HTTP 403: resolve groups failed: model unsupported '
                       'by selected groups: claude-opus-5 (request id: '
                       'toio202607261115134830414AoZHCTYl)')


@pytest.mark.unit
class TestGatewayRoutingTransient:

    def test_resolve_groups_403_is_gateway_retry_not_auth(self):
        """THE 07-26 19:15–19:54 production body must rotate slots, never die
        as PermissionError_."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, _RESOLVE_GROUPS_403,
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 403

    def test_resolve_groups_400_wrap_is_gateway_retry(self):
        """The same gateway layer can wrap the routing failure in a 400 —
        same predicate, same outcome (rotation, not BadRequestError)."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, _RESOLVE_GROUPS_403.replace('403', '400', 1),
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 400

    def test_each_routing_phrase_alone_suffices(self):
        assert _is_upstream_vendor_transient('API HTTP 403: resolve groups failed')
        assert _is_upstream_vendor_transient(
            'model unsupported by selected groups: claude-opus-5')
        # Inside a JSON envelope too (future toio wrap shape).
        env = ('{"error":{"message":"resolve groups failed: model unsupported '
               'by selected groups: claude-opus-5","type":"toio_api_error"}}')
        assert _is_upstream_vendor_transient(env)

    def test_routing_phrases_are_encoding_stable(self):
        """ASCII phrasing classifies even when the SAME body carries mojibake
        elsewhere — the failure mode that killed the Chinese-phrase layer."""
        assert _is_upstream_vendor_transient(
            'è¯·æ±: resolve groups failed: model unsupported by selected groups')

    def test_genuine_access_denied_stays_permission(self):
        """Control: a real access-denied 403 (no routing-resolution phrasing)
        must keep the PermissionError_ path unchanged."""
        raw = ('API HTTP 403: {"error":{"message":"Forbidden: key has no '
               'access to this model","type":"toio_api_error"}}')
        with pytest.raises(PermissionError_):
            _classify_http_error(403, raw, 'yuju-claude-opus-5-evaDaily', '[t]')


# ══════════════════════════════════════════════════════════
#  Wrapped upstream-status transient — 2026-07-28 12:47 incident
# ══════════════════════════════════════════════════════════

# The exact production body (owner probe of the Anthropic-native surface
# /v1/anthropic/v1/messages, request id toio20260728044734160442739SC7sFnGZ):
# the toio gateway wraps ANY upstream non-200 as HTTP 400 with an Anthropic-
# style envelope whose error.type is literally "<nil>" and whose message is
# "bad response status code <NNN> (request id: …)" — no UPSTREAM_VENDOR
# marker, no ext tail, no retry-later phrasing. The SAME adaptive+effort body
# then went 200 in 8/8 samples minutes later, so at least some of these wraps
# are TRANSIENT upstream blips — but _classify_http_error fell through every
# transient layer into BadRequestError (deterministic, pair-excluding round-
# killer). The ASCII marker 'bad response status code' is encoding-stable and
# never appears in a deterministic payload rejection ("Field required",
# "invalid …"), so it is safe to rotate on: a genuinely bad payload still
# dies — just after bounded gateway-class rotation instead of fail-fast.
_WRAPPED_STATUS_400 = ('API HTTP 400: {"type":"error","error":{"type":"<nil>",'
                       '"message":"bad response status code 400 (request id: '
                       'toio20260728044734160442739SC7sFnGZ)"}}')


@pytest.mark.unit
class TestWrappedUpstreamStatusTransient:

    def test_wrapped_status_400_is_gateway_retry_not_bad_request(self):
        """THE 2026-07-28 12:47 production body must rotate slots gateway-
        class, never die as deterministic BadRequestError."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, _WRAPPED_STATUS_400,
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 400
        assert 'bad response status code' in str(ei.value)

    def test_wrapped_status_403_variant_also_rotates(self):
        """The same wrap layer can surface on a 403 (the 2026-07-26 vendor
        storm wrapped both ways) — same predicate, same rotation."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(403, _WRAPPED_STATUS_400.replace('400', '403', 1),
                                 'yuju-claude-opus-5-evaDaily', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.status_code == 403

    def test_wrapped_status_predicate_ascii_stable(self):
        assert _is_upstream_vendor_transient(_WRAPPED_STATUS_400)
        assert _is_upstream_vendor_transient(
            'bad response status code 503 (request id: toioX)')

    def test_deterministic_400_never_matches(self):
        """Control: real payload rejections must stay BadRequestError — the
        pattern is a gateway wrap phrase, not a request-shape verdict."""
        assert not _is_upstream_vendor_transient(
            'API HTTP 400: bad request: signature: Field required')
        assert not _is_upstream_vendor_transient('API HTTP 400: invalid api key')
        raw = ('API HTTP 400: {"error":{"message":"Invalid request: '
               'signature: Field required","type":"invalid_request_error"}}')
        with pytest.raises(BadRequestError):
            _classify_http_error(400, raw, 'm', '[t]')


# ══════════════════════════════════════════════════════════
#  _ERR_BODY_LIMIT — logs keep the diagnostic tail
# ══════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════
#  _ERR_BODY_LIMIT — logs keep the diagnostic tail
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestErrBodyLimit:

    class _RecLogger:
        def __init__(self):
            self.records = []
        def warning(self, fmt, *a, **k):
            self.records.append(fmt % a if a else fmt)
        def error(self, fmt, *a, **k):
            self.records.append(fmt % a if a else fmt)
        def debug(self, *a, **k):
            pass

    def test_log_keeps_beyond_the_old_300_cut(self, monkeypatch):
        """The pasted 17:10 line died at `\"stage\":\"downstr` — the 300-char
        cap amputated the ext.error tail that carries the diagnosis. Pin the
        tail's survival."""
        rec = self._RecLogger()
        monkeypatch.setattr(_llm_errors, 'logger', rec)
        tail_marker = 'UPSTREAM_VENDOR/service=claude-opus-5/stage=downstream_http'
        body = ('{"error":{"message":"' + 'x' * 600 + '","type":"t"},'
                '"ext":{"error":{"note":"' + tail_marker + '"}}}')
        with pytest.raises(BadRequestError):
            _classify_http_error(400, f'API HTTP 400: {body}', 'm', '[t]')
        joined = '\n'.join(rec.records)
        assert tail_marker in joined, (
            f'log amputated the diagnostic tail: ...{joined[-160:]}')

    def test_summarize_parses_beyond_the_old_800_cap(self):
        """classify_status_error's 800-char cap cut the envelope mid-JSON,
        breaking summarize_error_body → raw envelope leaked to the HUD."""
        long_envelope = ('{"error":{"message":"' + _ZH_MESSAGE + ' '
                         + 'pad' * 400 + '","type":"toio_api_error"}}')
        assert len(long_envelope) > 800
        out = summarize_error_body(long_envelope)
        assert out.startswith('请求失败')
        assert '{' not in out


# ══════════════════════════════════════════════════════════
#  Static wire pins — the sync call sites must use the decoder
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCallSitePins:

    def test_stream_py_uses_decode_error_body(self):
        src = open(os.path.join(_ROOT, 'lib', 'llm', 'stream.py'),
                   encoding='utf-8').read()
        assert 'decode_error_body(resp)' in src, (
            'stream.py must decode the error body via decode_error_body, '
            'not resp.text (latin-1 mojibake source)')

    def test_chat_py_uses_decode_error_body(self):
        src = open(os.path.join(_ROOT, 'lib', 'llm', 'chat.py'),
                   encoding='utf-8').read()
        assert 'decode_error_body(resp)' in src

    def test_fallback_call_py_uses_err_body_limit(self):
        """The 17:10:37 'LLM call failed at round 3' line was amputated at
        the ext.error tail by err_str[:200] — the fallback path must cap
        with _ERR_BODY_LIMIT like every other error log."""
        src = open(os.path.join(_ROOT, 'lib', 'tasks_pkg', 'llm_fallback',
                                '_call.py'), encoding='utf-8').read()
        assert '_ERR_BODY_LIMIT' in src
        assert 'err_str = str(e)[:200]' not in src


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
