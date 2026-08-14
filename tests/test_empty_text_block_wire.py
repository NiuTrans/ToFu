#!/usr/bin/env python3
"""tests/test_empty_text_block_wire.py — phantom empty-text-block wire guard.

The 2026-07-31 kimi-k3 incident (tasks 93b60577 / 76d686cb, 4,337 wasted
retries over 4+ hours):

  * A virtual-user (VU) turn whose user row carries ``content=''`` hit the
    volatile-tail injection seams (``_refresh_tail_block`` /
    ``_refresh_detail_block`` / ``_append_user_profile_block``), which wrapped
    the empty string into a
    ``[{'type': 'text', 'text': ''}]`` block and then appended the reminder
    blocks. The R1 wire snapshot (task_events) shows block[0] =
    ``{'text': '', 'type': 'text'}`` followed by 5 reminder blocks.
  * Moonshot rejects the phantom block deterministically:
    ``Invalid request: text content is empty`` (upstreamStatus 400).
  * ``_fix_empty_user_messages`` never saw it — that healer only fires when
    EVERY block is empty, and the reminder blocks were non-empty.
  * ``_is_upstream_vendor_transient`` then misread the deterministic
    payload rejection as a vendor transient (the ext tail's
    ``source: UPSTREAM_VENDOR`` marker matched unconditionally), so
    dispatch rotated keys forever on a shape EVERY key rejects identically.

Three defence layers pinned here:
  1. Producers stop fabricating phantom blocks (4 wrap-seam guards).
  2. ``_strip_empty_text_blocks`` — the single build_body chokepoint that
     heals EVERY producer, present and future.
  3. The classifier demotes a deterministic upstream 4xx /
     ``invalid_request_error`` from "vendor transient" to BadRequestError,
     so the next payload bug fails fast instead of spinning 4,337 times.
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.llm.body import build_body  # noqa: E402
from lib.llm_errors import (  # noqa: E402
    BadRequestError,
    RateLimitError,
    _classify_http_error,
    _is_upstream_vendor_transient,
)
from lib.llm_sanitize import (  # noqa: E402
    _fix_empty_user_messages,
    _strip_empty_text_blocks,
)
from lib.tasks_pkg.system_context._profile import (  # noqa: E402
    _append_user_profile_block,
    _refresh_detail_block,
)
from lib.tasks_pkg.system_context._reminders import _refresh_tail_block  # noqa: E402

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


# The exact production body from logs/app.log 2026-07-31 (task 93b60577 R1).
_KIMI_EMPTY_TEXT_400 = (
    'API HTTP 400: {"error":{"message":"Invalid request: text content is '
    'empty","type":"invalid_request_error"},"ext":{"error":{"source":'
    '"UPSTREAM_VENDOR","service":"kimi-k3","stage":"downstream_http",'
    '"upstreamStatus":400}}}'
)


def _empty_text_blocks(messages):
    """Count empty/whitespace-only text blocks across all list contents."""
    n = 0
    for m in messages:
        c = m.get('content')
        if not isinstance(c, list):
            continue
        for b in c:
            if (isinstance(b, dict) and b.get('type') == 'text'
                    and not (b.get('text') or '').strip()):
                n += 1
    return n


# ══════════════════════════════════════════════════════════
#  Layer 2 — _strip_empty_text_blocks (the build_body chokepoint)
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStripEmptyTextBlocks:

    def test_mixed_list_drops_only_the_phantom(self):
        """THE incident shape: phantom block[0] + live reminder blocks."""
        msgs = [{'role': 'user', 'content': [
            {'type': 'text', 'text': ''},
            {'type': 'text', 'text': '<system-reminder>\n[PROJECT BOARD]\n</system-reminder>'},
        ]}]
        _strip_empty_text_blocks(msgs)
        assert _empty_text_blocks(msgs) == 0
        assert len(msgs[0]['content']) == 1
        assert 'PROJECT BOARD' in msgs[0]['content'][0]['text']

    def test_whitespace_only_block_dropped(self):
        msgs = [{'role': 'user', 'content': [
            {'type': 'text', 'text': '   \n '},
            {'type': 'text', 'text': 'real'},
        ]}]
        _strip_empty_text_blocks(msgs)
        assert [b['text'] for b in msgs[0]['content']] == ['real']

    def test_non_text_blocks_preserved(self):
        img = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}
        msgs = [{'role': 'user', 'content': [
            {'type': 'text', 'text': ''}, img]}]
        _strip_empty_text_blocks(msgs)
        assert msgs[0]['content'] == [img]

    def test_all_empty_list_on_tool_call_assistant_drops_content_key(self):
        """assistant(tool_calls) must end in the proven no-content shape
        (build_assistant_tool_call_message's canonical form), never '' or []."""
        msgs = [{'role': 'assistant',
                 'content': [{'type': 'text', 'text': ''}],
                 'tool_calls': [{'id': 'tc1', 'type': 'function',
                                 'function': {'name': 'f', 'arguments': '{}'}}]}]
        _strip_empty_text_blocks(msgs)
        assert 'content' not in msgs[0]
        assert msgs[0]['tool_calls']

    def test_all_empty_list_on_plain_user_collapses_for_healer(self):
        """Collapse to '' so _fix_empty_user_messages claims it downstream."""
        msgs = [{'role': 'user',
                 'content': [{'type': 'text', 'text': ''}]}]
        _strip_empty_text_blocks(msgs)
        assert msgs[0]['content'] == ''
        _fix_empty_user_messages(msgs)
        assert msgs[0]['content'] == '[empty message]'

    def test_non_empty_shapes_untouched(self):
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hi'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]},
            {'role': 'assistant', 'content': ''},  # whole-content healer's turf
        ]
        snapshot = [dict(m) for m in msgs]
        _strip_empty_text_blocks(msgs)
        assert msgs == snapshot

    def test_empty_and_none_inputs(self):
        assert _strip_empty_text_blocks([]) == []
        assert _strip_empty_text_blocks(None) is None

    def test_warning_log_names_block_locations(self, monkeypatch):
        """§2 logging discipline: when the strip fires, the log must name WHERE
        each phantom block lived (message index + role + block index) — the
        NEXT producer must be findable with one grep, not a guess. (Task
        76d686cb's producer was never directly proven — no snapshot — so
        location logging is the only way the next occurrence identifies its
        seam.)"""
        import lib.llm_sanitize._messages as _m

        class _Rec:
            def __init__(self):
                self.records = []

            def warning(self, fmt, *a, **k):
                self.records.append(fmt % a if a else fmt)

            def info(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        rec = _Rec()
        monkeypatch.setattr(_m, 'logger', rec)
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': ''},
                {'type': 'text', 'text': 'reminder'}]},
            {'role': 'assistant',
             'content': [{'type': 'text', 'text': ''}],
             'tool_calls': [{'id': 't', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{}'}}]},
        ]
        _strip_empty_text_blocks(msgs)
        joined = '\n'.join(rec.records)
        assert '#1/user/block0' in joined, f'user location missing: {joined}'
        assert '#2/assistant/block0' in joined, (
            f'assistant location missing: {joined}')


# ══════════════════════════════════════════════════════════
#  Layer 2 — build_body end-to-end on the production wire
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBuildBodyHealsVuShape:

    def test_kimi_k3_vu_turn_has_no_empty_text_block(self):
        """THE production R1 wire (task 93b60577): VU user row content=''
        wrapped + 5 reminder blocks appended. build_body must emit zero
        empty text blocks and keep every reminder."""
        reminders = [{'type': 'text', 'text': f'<system-reminder>\nblock {i}\n</system-reminder>'}
                     for i in range(5)]
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'assistant', 'content': 'previous answer'},
            {'role': 'user', 'content': [{'type': 'text', 'text': ''}] + reminders},
        ]
        body = build_body('kimi-k3', messages, thinking_enabled=True,
                          thinking_depth='max')
        assert _empty_text_blocks(body['messages']) == 0
        last = body['messages'][-1]
        assert last['role'] == 'user'
        texts = [b.get('text', '') for b in last['content']
                 if isinstance(b, dict) and b.get('type') == 'text']
        assert sum('block' in t for t in texts) == 5

    def test_tool_history_assistant_content_none_shape_stays_valid(self):
        """The OTHER half of the production wire: 26 assistant messages with
        tool_calls and no content — the accepted no-content shape must pass
        through unchanged (no fabricated content key)."""
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'q'},
            {'role': 'assistant',
             'tool_calls': [{'id': 'tc1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tc1', 'content': 'result'},
            {'role': 'user', 'content': 'next'},
        ]
        body = build_body('kimi-k3', messages, thinking_enabled=True)
        asst = [m for m in body['messages'] if m.get('tool_calls')]
        assert asst and 'content' not in asst[0]

    def test_frontend_image_with_empty_caption_block_healed(self):
        """Future-producer coverage: a multimodal user message whose caption
        block is empty must not leak the phantom block either."""
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': ''},
                {'type': 'text', 'text': 'what is in this image?'},
            ]},
        ]
        body = build_body('kimi-k3', messages)
        assert _empty_text_blocks(body['messages']) == 0


# ══════════════════════════════════════════════════════════
#  Layer 1 — producer seams stop fabricating phantom blocks
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProducerGuards:

    def test_refresh_tail_block_on_empty_user(self):
        msgs = [{'role': 'user', 'content': ''}]
        action = _refresh_tail_block(
            msgs, '<system-reminder>\n[PROJECT BOARD] v1\n</system-reminder>',
            '[PROJECT BOARD]')
        assert action == 'added'
        assert _empty_text_blocks(msgs) == 0
        assert any('PROJECT BOARD' in (b.get('text') or '')
                   for b in msgs[0]['content'])

    def test_refresh_tail_block_on_nonempty_user_unchanged(self):
        msgs = [{'role': 'user', 'content': 'real question'}]
        _refresh_tail_block(
            msgs, '<system-reminder>\n[PROJECT BOARD] v1\n</system-reminder>',
            '[PROJECT BOARD]')
        blocks = msgs[0]['content']
        assert blocks[0] == {'type': 'text', 'text': 'real question'}
        assert 'PROJECT BOARD' in blocks[1]['text']

    def test_refresh_detail_block_on_empty_user(self):
        msgs = [{'role': 'user', 'content': ''}]
        _refresh_detail_block(msgs, '<system-reminder>\ndetail\n</system-reminder>')
        assert _empty_text_blocks(msgs) == 0

    def test_append_user_profile_block_on_empty_user(self):
        msgs = [{'role': 'user', 'content': ''}]
        ok = _append_user_profile_block(msgs, '[USER PREFERENCE PROFILE]\nx')
        assert ok is True
        assert _empty_text_blocks(msgs) == 0

# ══════════════════════════════════════════════════════════
#  Layer 3 — deterministic vendor 4xx is NOT a transient
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeterministicVendor400:

    def test_production_kimi_body_is_not_transient(self):
        """THE pasted log line: rotating keys can never heal a payload the
        vendor rejected on merits — the predicate must say deterministic."""
        assert not _is_upstream_vendor_transient(_KIMI_EMPTY_TEXT_400)

    def test_production_kimi_body_raises_bad_request(self):
        """… and the classifier must fail fast (typed BadRequestError → slot
        released, pair-excluded) instead of gateway-rotating 4,337 times."""
        with pytest.raises(BadRequestError) as ei:
            _classify_http_error(400, _KIMI_EMPTY_TEXT_400, 'kimi-k3', '[t]')
        assert 'text content is empty' in str(ei.value)

    def test_transient_upstream_statuses_still_rotate(self):
        """A genuine vendor blip (5xx / 429 / 408 at the upstream) stays
        gateway-class rotation — the marker's original contract."""
        for up in (429, 500, 502, 503, 529):
            env = ('API HTTP 400: {"error":{"message":"vendor error","type":'
                   '"toio_api_error"},"ext":{"error":{"source":"UPSTREAM_VENDOR",'
                   f'"service":"m","stage":"downstream_http","upstreamStatus":{up}}}}}')
            assert _is_upstream_vendor_transient(env), f'upstreamStatus={up}'

    def test_marker_without_status_still_rotates(self):
        """The pre-ext-tail wrap shape (marker only, no upstreamStatus) keeps
        the established transient verdict — no behaviour change there."""
        env = ('API HTTP 403: {"error":{"message":"err","type":"toio_api_error"},'
               '"ext":{"error":{"source":"UPSTREAM_VENDOR","stage":"x"}}}')
        assert _is_upstream_vendor_transient(env)

    def test_phrase_layer_still_wins_over_status(self):
        """The 2026-07-26 toio shape: Chinese retry-later phrase + marker +
        upstreamStatus 400 — phrasing is the stronger evidence, still rotates."""
        env = ('API HTTP 400: {"error":{"message":"请求失败，请稍后再尝试 '
               '(request id: toioX)","type":"toio_api_error"},"ext":{"error":'
               '{"source":"UPSTREAM_VENDOR","service":"claude-opus-5","stage":'
               '"downstream_http","upstreamStatus":400}}}')
        assert _is_upstream_vendor_transient(env)
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(400, env, 'm', '[t]')
        assert ei.value.is_gateway is True

    def test_invalid_request_error_type_alone_is_deterministic(self):
        """The vendor's own type vocabulary says "your request is invalid" —
        deterministic even when the ext tail lacks upstreamStatus."""
        env = ('API HTTP 400: {"error":{"message":"Invalid request: bad shape",'
               '"type":"invalid_request_error"},"ext":{"error":{"source":'
               '"UPSTREAM_VENDOR","stage":"downstream_http"}}}')
        assert not _is_upstream_vendor_transient(env)
        with pytest.raises(BadRequestError):
            _classify_http_error(400, env, 'm', '[t]')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
