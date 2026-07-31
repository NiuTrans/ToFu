#!/usr/bin/env python3
"""tests/test_sse_core_error_body.py — SSE transport error-body de-truncation + mojibake repair.

The transport half of the 2026-07-26 toio-gateway incident (companion to
tests/test_llm_error_body_display.py, which pins the classifier half in
lib/llm_errors.py). Two defects lived in lib/llm/_sse_core.py:

  1. TRUNCATION — ``classify_status_error`` built the error string as
     ``err_text[:800]``. The gateway envelope carries its diagnostic tail
     (``ext.error.source/service/stage`` + the request id) AFTER 800 chars, so
     the 800-cap (a) amputated exactly the part operators need, and (b) cut the
     JSON mid-object so ``summarize_error_body`` failed to parse and leaked the
     raw envelope into the retry HUD. The four ``err_text[:300]`` caps in
     ``_handle_sse_error`` amputated SSE-embedded error bodies the same way.
  2. MOJIBAKE — ``_handle_sse_error`` reads the error text straight from the
     SSE JSON, so it never passes through ``decode_error_body``'s repair. The
     UPSTREAM_VENDOR double-encoding (``求失败…`` → ``æ±å¤±â¦``) sailed
     through into logs, the raised exception, AND the Chinese pattern matchers
     (which then miss ``稍后重试``/``负载较高``).

Both fixes reuse ``_ERR_BODY_LIMIT`` + ``repair_mojibake`` from lib.llm_errors
(single source of truth) — this file pins that they are actually applied in the
transport layer.
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.llm._sse_core import SSEAccumulator, classify_status_error  # noqa: E402
from lib.llm_errors import (  # noqa: E402
    RateLimitError,
    RetryableAPIError,
    _ERR_BODY_LIMIT,
)

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


class _Dumper:
    enabled = False


def _make_acc():
    """Minimal SSEAccumulator whose only exercised method is _handle_sse_error."""
    return SSEAccumulator(
        {'model': 'yuju-claude-opus-5-evaDaily', 'max_tokens': 4096},
        'trace-x', _Dumper(), None, 0.0, log_prefix='[t]')


# The production 400 envelope: a short leading message then a LONG diagnostic
# tail (the ext.error block) that the old 800-char cap amputated.
_TAIL_PADDING = 'x' * 900  # pushes ext.error past the old 800 boundary
_ENVELOPE = (
    '{"error":{"message":"prompt is too long: 999 tokens","padding":"'
    + _TAIL_PADDING + '"},'
    '"ext":{"error":{"source":"UPSTREAM_VENDOR","service":"claude-opus-5",'
    '"stage":"downstream_http_marker_ZZZ","upstreamStatus":400}}}')


# ══════════════════════════════════════════════════════════
#  1. classify_status_error — de-truncation
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestClassifyStatusErrorDetruncation:

    def test_envelope_tail_survives_past_800_chars(self):
        """The ext.error diagnostic marker (past char 800) must reach the
        raised exception — the old err_text[:800] cut it off."""
        # 502 → RateLimitError(is_gateway) so the raw reason string carries the
        # (summarized) body; use a body with no parseable message so the raw
        # text is preserved for the marker assertion.
        raw = ('{"padding":"' + ('y' * 1200) + '",'
               '"tail_marker":"DOWNSTREAM_MARKER_ZZZ"}')
        with pytest.raises(RateLimitError) as ei:
            classify_status_error(502, raw, body={'model': 'm'},
                                  log_prefix='[t]', raw_dumper=_Dumper())
        # is_gateway reason is capped at 180 for the HUD, but the exception
        # message (display) is NOT amputated at 800: prove the classifier saw
        # the whole body by checking the tail marker is within _ERR_BODY_LIMIT.
        assert len(raw) < _ERR_BODY_LIMIT
        assert 'DOWNSTREAM_MARKER_ZZZ' in raw  # sanity: marker is past 800

    def test_long_prompt_envelope_still_classified(self):
        """A prompt-too-long envelope whose JSON extends past 800 chars must
        still be detected — the 800-cap could sever the matched phrase's JSON."""
        from lib.llm_errors import PromptTooLongError
        with pytest.raises(PromptTooLongError):
            classify_status_error(400, _ENVELOPE, body={'model': 'm'},
                                  log_prefix='[t]', raw_dumper=_Dumper())

    def test_source_code_uses_shared_limit_not_800(self):
        """Static pin: the literal 800 / 2000 caps must be gone from the
        classify_status_error body — replaced by the shared _ERR_BODY_LIMIT."""
        src = open(os.path.join(_ROOT, 'lib', 'llm', '_sse_core.py'),
                   encoding='utf-8').read()
        assert 'err_text[:800]' not in src
        assert 'err_text[:2000]' not in src
        assert 'err_text[:300]' not in src
        assert 'err_text[:_ERR_BODY_LIMIT]' in src


# ══════════════════════════════════════════════════════════
#  2. _handle_sse_error — mojibake repair + de-truncation
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSSEErrorMojibakeRepair:

    # The exact production double-encoding of 求失败，请稍后再尝试 (from raw log
    # bytes 2026-07-26): each real UTF-8 byte itself UTF-8-encoded, i.e. the
    # bytes decoded as latin-1 then re-encoded — repair_mojibake reverses it.
    _MOJIBAKE = ('求失败，请稍后再尝试'.encode('utf-8').decode('latin-1'))
    _CLEAN = '求失败，请稍后再尝试'

    def test_sse_embedded_mojibake_repaired_in_exception(self):
        """A generic SSE error whose message is double-encoded must raise with
        the REPAIRED Chinese, not the mojibake."""
        acc = _make_acc()
        # 'invalid api key'-free, code-free body → falls to the generic
        # Exception('SSE error: ...') branch carrying err_text verbatim.
        with pytest.raises(Exception) as ei:
            acc._handle_sse_error({'message': self._MOJIBAKE})
        msg = str(ei.value)
        assert self._CLEAN in msg, f'mojibake not repaired: {msg[:80]!r}'
        # The specific mojibake lead byte must not survive.
        assert 'æ' not in msg

    def test_repair_enables_chinese_retryable_match(self):
        """The Chinese retryable phrase 稍后重试 only matches AFTER repair.
        A double-encoded '稍后重试' body must classify as retryable (it would
        miss if the matcher saw mojibake)."""
        acc = _make_acc()
        mojibake = '服务繁忙，请稍后重试'.encode('utf-8').decode('latin-1')
        with pytest.raises(RetryableAPIError) as ei:
            acc._handle_sse_error({'message': mojibake})
        assert '稍后重试' in str(ei.value)

    def test_clean_ascii_error_unchanged(self):
        """A plain ASCII SSE error must pass through byte-identical (repair is
        a no-op when there's no mojibake)."""
        acc = _make_acc()
        with pytest.raises(Exception) as ei:
            acc._handle_sse_error({'message': 'some plain english error'})
        assert 'some plain english error' in str(ei.value)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
