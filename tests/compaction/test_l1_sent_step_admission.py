"""L1 rewrites already-sent prompt bytes only when the next request is cheaper.

The previous request's api-form messages are the provider-cached prefix.
Editing any of them makes the next request re-send its last tool step: the
region from the last assistant message inside the sent prefix to the end of
that prefix (the request before that one cached everything earlier). Keeping
the step reads it back (``before_tokens * cacheReadMul``); editing writes it
again (``after_tokens * cacheWriteMul``). New content after the sent prefix is
written in both cases and cancels. ``micro_compact`` admits the edit only when
it is strictly cheaper on the main route's verified price card; otherwise the
whole previous request stays byte-identical.

Incident (conversation mvywcu5qqy5ocy, round 3, Claude via the Anthropic face):
the previous request was ``[user, assistant(4 tool_calls), tool(load_skill),
tool(img1), tool(img2), tool(img3)]``. A fixed "last two api-form messages are
editable" tail let ``strip_cold_images`` replace img2 (a 778x76 crop, ~80
tokens) with a ~60-token stub. On the Anthropic wire the four results are one
user message, so 3,624 cached tokens were re-written at 1.25x to remove ~20.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import math
import struct
import threading
import zlib

import pytest

from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens

pytestmark = pytest.mark.unit

_PROVIDER = 'sent-step-anthropic-face'
_MODEL = 'claude-sent-step-test'
_USER = 7
_LAYER1_LOGGER = 'lib.tasks_pkg.compaction._layer1'


def _png_data_url(width: int, height: int) -> str:
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    chunk = (struct.pack('>I', len(ihdr)) + b'IHDR' + ihdr
             + struct.pack('>I', zlib.crc32(b'IHDR' + ihdr)))
    # Only the header is ever read; the padding stands in for pixel data.
    body = b'\x89PNG\r\n\x1a\n' + chunk + b'\x00' * 900
    return 'data:image/png;base64,' + base64.b64encode(body).decode()


def _call(call_id: str, name: str) -> dict:
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps({'id': call_id})}}


def _assistant(*call_ids: str, name: str = 'read_files') -> dict:
    return {'role': 'assistant', 'content': '',
            'tool_calls': [_call(call_id, name) for call_id in call_ids]}


def _text_result(call_id: str, lines: int) -> dict:
    body = ''.join(f'{call_id} line {i}: value = compute({i}) + offset\n'
                   for i in range(lines))
    return {'role': 'tool', 'tool_call_id': call_id, 'name': 'read_files',
            'content': body}


def _image_result(call_id: str, caption: str) -> dict:
    return {'role': 'tool', 'tool_call_id': call_id, 'name': 'inspect_image',
            'content': [{'type': 'text', 'text': caption},
                        {'type': 'image_url',
                         'image_url': {'url': _png_data_url(778, 76)}}]}


def _task(conv: str, provider_id: str = _PROVIDER) -> dict:
    return {'id': f'task-{conv}', '_userId': _USER, 'convId': conv,
            'model': _MODEL, 'provider_id': provider_id,
            'config': {'model': _MODEL}}


def _wire(messages: list) -> str:
    return json.dumps(messages, sort_keys=True, ensure_ascii=False)


@pytest.fixture
def priced_route(monkeypatch):
    """Anthropic-like verified card: writes 1.25x, reads 0.10x of input."""
    from lib.pricing import _provider
    monkeypatch.setitem(_provider.PROVIDER_PRICING, _PROVIDER, {
        _MODEL: {'input': 3.0, 'output': 15.0, 'currency': 'USD',
                 'cacheWriteMul': 1.25, 'cacheReadMul': 0.10}})


@pytest.fixture
def recorder(monkeypatch):
    """Capture cache notices and request-local evidence; isolate cache state."""
    from lib.tasks_pkg.cache_tracking import _state
    notices, events = [], []
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.notify_compaction',
                        lambda conv, **kw: notices.append({'conv': conv, **kw}))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: events.append(event))
    monkeypatch.setattr('lib.context_telemetry.record_compaction_event',
                        lambda *a, **k: None)
    with _state._cache_lock:
        _state._cache_states.clear()
    yield notices, events
    with _state._cache_lock:
        _state._cache_states.clear()


def _previous_request_sent(conv: str, messages: list, *, thread_id=None) -> None:
    """Record that ``messages`` were the previous request on this conversation."""
    from lib.tasks_pkg.cache_tracking import _state
    state = _state.CacheState()
    state.message_count = len(messages)
    state.call_count = 1
    state.last_cache_read_tokens = 40_968
    key = (_USER, conv, thread_id) if thread_id else _state._state_key(conv, user_id=_USER)
    with _state._cache_lock:
        _state._cache_states[key] = state


def _micro_compact(messages: list, task: dict) -> int:
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    return micro_compact(messages, conv_id=task['convId'], task=task)


# ── (a) the incident shape ─────────────────────────────────────────────


def test_incident_image_batch_leaves_previous_request_byte_identical(
        priced_route, recorder, caplog):
    notices, events = recorder
    conv = 'conv-sent-step-incident'
    skill = ('Skill loaded: frontend-design\nid: frontend-design\n'
             'content_hash: 5f1c\n' + 'Use the shared design tokens.\n' * 120)
    previous = [
        {'role': 'user', 'content': 'Compare the three header crops.'},
        _assistant('call_skill', 'call_img1', 'call_img2', 'call_img3',
                   name='inspect_image'),
        {'role': 'tool', 'tool_call_id': 'call_skill', 'name': 'load_skill',
         'content': skill},
        _image_result('call_img1', 'crop 1'),
        _image_result('call_img2', 'crop 2'),
        _image_result('call_img3', 'crop 3'),
    ]
    sent = copy.deepcopy(previous)
    _previous_request_sent(conv, previous)
    messages = previous + [_assistant('call_img4', name='inspect_image'),
                           _image_result('call_img4', 'crop 4')]
    caplog.set_level(logging.INFO, logger=_LAYER1_LOGGER)

    _micro_compact(messages, _task(conv))

    assert _wire(messages[:6]) == _wire(sent)
    assert notices == [] and events == []
    refusals = [r.getMessage() for r in caplog.records
                if r.name == _LAYER1_LOGGER and 'refused' in r.getMessage()]
    assert len(refusals) == 1
    assert 'msgs[1:6]' in refusals[0]


# ── (b) a big consumed result in the last sent step is still compacted ──


def _big_result_conversation():
    previous = [
        {'role': 'system', 'content': 'You are a coding agent.'},
        {'role': 'user', 'content': 'Find the regression.'},
        _assistant('call_a'), _text_result('call_a', 20),
        _assistant('call_b'), _text_result('call_b', 1400),
    ]
    newest = [_assistant('call_c'), _text_result('call_c', 2100)]
    return previous, newest


def test_big_consumed_result_in_last_sent_step_is_compacted_and_advertised(
        priced_route, recorder):
    notices, events = recorder
    conv = 'conv-sent-step-big'
    previous, newest = _big_result_conversation()
    assert _estimate_msg_tokens(previous[5]) > 15_000
    # The newest unconsumed batch fills the 48K hot tail, so L1 deems the
    # previous step's result cold by its own rules.
    assert _estimate_msg_tokens(previous[5]) + _estimate_msg_tokens(newest[1]) > 48_000
    sent = copy.deepcopy(previous)
    _previous_request_sent(conv, previous)
    messages = previous + copy.deepcopy(newest)

    saved = _micro_compact(messages, _task(conv))

    assert saved > 0
    assert _wire(messages[:4]) == _wire(sent[:4])  # before the step: frozen
    assert messages[4] == sent[4]
    assert messages[5]['content'].startswith('[read_files result compacted')
    assert messages[6:] == newest
    assert notices == [{'conv': conv, 'user_id': _USER, 'intended_rewrite_index': 4}]
    assert [event.get('toolCallId') for event in events] == ['call_b']


# ── (c) unverified pricing protects the whole sent prefix ──────────────


def test_unverified_pricing_protects_the_sent_step(recorder, caplog):
    notices, events = recorder
    conv = 'conv-sent-step-unpriced'
    previous, newest = _big_result_conversation()
    sent = copy.deepcopy(previous)
    _previous_request_sent(conv, previous)
    messages = previous + copy.deepcopy(newest)
    caplog.set_level(logging.INFO, logger=_LAYER1_LOGGER)

    _micro_compact(messages, _task(conv, provider_id='sent-step-unpriced'))

    assert _wire(messages[:6]) == _wire(sent)
    assert notices == [] and events == []
    assert any('refused' in r.getMessage() and 'pricing unverified' in r.getMessage()
               for r in caplog.records if r.name == _LAYER1_LOGGER)


# ── (d) a parallel step is decided and edited as one unit ──────────────


def _parallel_conversation(newest_lines: int):
    previous = [
        {'role': 'system', 'content': 'You are a coding agent.'},
        {'role': 'user', 'content': 'Read the three modules.'},
        _assistant('call_b1', 'call_b2', 'call_b3'),
        _text_result('call_b1', 1400),
        _text_result('call_b2', 1400),
        _text_result('call_b3', 1400),
    ]
    newest = [_assistant('call_c'), _text_result('call_c', newest_lines)]
    return previous, newest


def test_parallel_step_compacts_every_cold_result_together(priced_route, recorder):
    notices, events = recorder
    conv = 'conv-sent-step-parallel'
    previous, newest = _parallel_conversation(newest_lines=3600)
    assert _estimate_msg_tokens(newest[1]) > 48_000  # every earlier result is cold
    sent = copy.deepcopy(previous)
    _previous_request_sent(conv, previous)
    messages = previous + copy.deepcopy(newest)

    _micro_compact(messages, _task(conv))

    assert _wire(messages[:3]) == _wire(sent[:3])
    assert all(m['content'].startswith('[read_files result compacted')
               for m in messages[3:6])
    assert notices == [{'conv': conv, 'user_id': _USER, 'intended_rewrite_index': 2}]
    assert sorted(event.get('toolCallId') for event in events) == [
        'call_b1', 'call_b2', 'call_b3']


def test_partially_cold_parallel_step_is_never_split(priced_route, recorder):
    """Compacting one of three results re-sends the other two at write price."""
    notices, events = recorder
    conv = 'conv-sent-step-partial'
    previous, newest = _parallel_conversation(newest_lines=20)
    sent = copy.deepcopy(previous)
    _previous_request_sent(conv, previous)
    messages = previous + copy.deepcopy(newest)

    _micro_compact(messages, _task(conv))

    assert _wire(messages[:6]) == _wire(sent)
    assert notices == [] and events == []


# ── (e) a stale high-water boundary after an L2 shrink ─────────────────


def test_history_shrink_clamp_keeps_l1_working_after_l2(priced_route, recorder, monkeypatch):
    notices, events = recorder
    conv = 'conv-sent-step-shrink'
    import lib.tasks_pkg.cache_tracking._persist as persist
    monkeypatch.setattr(persist, 'read_persisted_boundary', lambda *a, **k: 398)
    # Pre-shrink request on an earlier thread: 400 api-form messages.
    _previous_request_sent(conv, [{}] * 400, thread_id=threading.get_ident() + 1)
    shrunk = [
        {'role': 'system', 'content': 'You are a coding agent.'},
        {'role': 'user', 'content': 'Find the regression.'},
        {'role': 'user', 'content': 'Summary of earlier work: modules read.'},
        _assistant('call_b'), _text_result('call_b', 1400),
        _assistant('call_c'), _text_result('call_c', 3600),
    ]
    from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
    assert get_cache_prefix_count(conv, current_msg_count=len(shrunk), user_id=_USER) == 7

    # Fresh-thread round: everything that exists counts as sent; the newest
    # batch is unconsumed, so L1 has nothing it may edit yet.
    round_a = copy.deepcopy(shrunk)
    _micro_compact(round_a, _task(conv))
    assert round_a == shrunk and notices == []

    # The shrunk request was sent; the next round's boundary is that request,
    # not the stale 398/400 high-water marks, and its big consumed result in
    # the last sent step is compacted again.
    _previous_request_sent(conv, round_a)
    round_b = round_a + [_assistant('call_d'), _text_result('call_d', 20)]
    _micro_compact(round_b, _task(conv))
    assert round_b[:6] == shrunk[:6]
    assert round_b[6]['content'].startswith('[read_files result compacted')
    assert notices == [{'conv': conv, 'user_id': _USER, 'intended_rewrite_index': 5}]


def test_trial_leaves_live_history_to_the_real_pass(priced_route, recorder):
    """Reactive L1 adds compact_cold_assistant, which ignores the prefix.

    The detached trial must not edit live messages: the real pass then makes
    (and reports) exactly the edits that step makes on its own.
    """
    import lib.tasks_pkg.compaction._constants as constants
    from lib.tasks_pkg.compaction._builtin_steps import compact_cold_assistant
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    from lib.tasks_pkg.compaction._steps import CompactionContext

    conv = 'conv-sent-step-reactive'
    previous = [{'role': 'system', 'content': 'You are a coding agent.'},
                {'role': 'user', 'content': 'Fix the bug.'}]
    for index in range(8):
        call_id = f'call_{index}'
        previous += [{**_assistant(call_id), 'content': f'analysis {index} ' * 200},
                     _text_result(call_id, 2)]
    _previous_request_sent(conv, previous)
    messages = previous + [_assistant('call_8'), _text_result('call_8', 2)]
    expected = compact_cold_assistant(CompactionContext(
        messages=copy.deepcopy(messages), constants=constants))
    assert expected > 0

    saved = micro_compact(messages, conv_id=conv, task=_task(conv),
                          enable_assistant_compact=True)

    assert saved == expected


# ── The admission rule itself ──────────────────────────────────────────


@pytest.mark.parametrize(('before', 'after', 'admitted'), [
    (20_100, 300, True),     # [assistant 100, tool 20K] -> 200-token stub
    (3_624, 3_604, False),   # the incident: ~20 tokens removed from 3.6K
    (1_000, 80, False),      # 100 == 100: equal cost is not cheaper
])
def test_admission_compares_rewrite_price_with_readback_price(
        priced_route, before, after, admitted):
    from lib.tasks_pkg.compaction._cache_economics import sent_step_rewrite_economics
    verdict = sent_step_rewrite_economics(
        _task('conv-rule'), tokens_before=before, tokens_after=after,
        prompt_tokens=40_000)
    assert verdict['pricing_known'] is True
    assert verdict['admitted'] is admitted
    assert verdict['keep_cost_tokens'] == pytest.approx(before * 0.10)
    assert verdict['rewrite_cost_tokens'] == pytest.approx(after * 1.25)


def test_unverified_route_never_admits():
    from lib.tasks_pkg.compaction._cache_economics import sent_step_rewrite_economics
    verdict = sent_step_rewrite_economics(
        _task('conv-rule', provider_id='sent-step-unpriced'),
        tokens_before=20_100, tokens_after=300, prompt_tokens=40_000)
    assert verdict['pricing_known'] is False
    assert verdict['admitted'] is False


# ── Claude image tokens from the data-URL header ───────────────────────


def _jpeg_data_url(width: int, height: int) -> str:
    app1 = b'Exif\x00\x00' + b'\x00' * 3000
    body = (b'\xff\xd8'
            + b'\xff\xe1' + struct.pack('>H', len(app1) + 2) + app1
            + b'\xff\xdb' + struct.pack('>H', 67) + b'\x00' * 65
            + b'\xff\xc0' + struct.pack('>HBHHB', 17, 8, height, width, 3) + b'\x00' * 9
            + b'\x00' * 500)
    return 'data:image/jpeg;base64,' + base64.b64encode(body).decode()


def _gif_data_url(width: int, height: int) -> str:
    return 'data:image/gif;base64,' + base64.b64encode(
        b'GIF89a' + struct.pack('<HH', width, height) + b'\x00' * 64).decode()


def _webp_data_url(width: int, height: int, chunk: bytes) -> str:
    if chunk == b'VP8X':
        payload = (b'\x00' * 4 + (width - 1).to_bytes(3, 'little')
                   + (height - 1).to_bytes(3, 'little'))
    elif chunk == b'VP8L':
        bits = (width - 1) | ((height - 1) << 14)
        payload = b'\x2f' + bits.to_bytes(4, 'little')
    else:
        payload = (b'\x00' * 3 + b'\x9d\x01\x2a'
                   + struct.pack('<HH', width, height))
    data = chunk + struct.pack('<I', len(payload)) + payload + b'\x00' * 64
    body = b'RIFF' + struct.pack('<I', len(data) + 4) + b'WEBP' + data
    return 'data:image/webp;base64,' + base64.b64encode(body).decode()


def _image_block(url: str) -> dict:
    return {'type': 'image_url', 'image_url': {'url': url}}


def _claude_tokens(width: int, height: int) -> int:
    scale = min(1.0, 1568 / max(width, height),
                math.sqrt(1_150_000 / (width * height)))
    return math.ceil(width * height * scale * scale / 750)


@pytest.mark.parametrize('url', [
    _png_data_url(778, 76), _jpeg_data_url(778, 76), _gif_data_url(778, 76),
    _webp_data_url(778, 76, b'VP8X'), _webp_data_url(778, 76, b'VP8L'),
    _webp_data_url(778, 76, b'VP8 '),
], ids=['png', 'jpeg', 'gif', 'webp-vp8x', 'webp-vp8l', 'webp-vp8'])
def test_claude_image_tokens_follow_pixel_size(url):
    from lib.tasks_pkg.compaction._tokens import claude_image_tokens
    assert claude_image_tokens(_image_block(url), _MODEL) == 79


def test_claude_image_tokens_apply_the_documented_downscale():
    from lib.tasks_pkg.compaction._tokens import claude_image_tokens
    for width, height in ((4000, 3000), (3000, 200), (1092, 1092)):
        tokens = claude_image_tokens(
            _image_block(_png_data_url(width, height)), _MODEL)
        assert tokens == _claude_tokens(width, height)
        assert tokens <= math.ceil(1_150_000 / 750)


def test_other_routes_and_unreadable_images_keep_the_flat_estimate():
    from lib.tasks_pkg.compaction._constants import _IMAGE_TOKENS_DEFAULT
    from lib.tasks_pkg.compaction._tokens import (
        claude_image_tokens, estimate_msg_tokens_for_model)
    png = _image_block(_png_data_url(778, 76))
    assert claude_image_tokens(png, 'gpt-5.6') is None
    assert claude_image_tokens(_image_block('tofu-media:abc123'), _MODEL) is None
    assert claude_image_tokens(
        _image_block('data:image/png;base64,bm90IGFuIGltYWdl'), _MODEL) is None
    message = _image_result('call_img', 'crop')
    assert estimate_msg_tokens_for_model(message, 'gpt-5.6') == _estimate_msg_tokens(message)
    assert estimate_msg_tokens_for_model(message, _MODEL) == (
        _estimate_msg_tokens(message) - _IMAGE_TOKENS_DEFAULT + 79)


def test_strip_cold_images_reports_route_billed_image_tokens():
    import lib.tasks_pkg.compaction._constants as constants
    from lib.tasks_pkg.compaction._steps import CompactionContext, run_steps
    import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401

    def conversation():
        messages = [{'role': 'user', 'content': 'Compare the crops.'}]
        for index in range(4):
            call_id = f'call_img{index}'
            messages += [_assistant(call_id, name='inspect_image'),
                         _image_result(call_id, f'crop {index}')]
        return messages

    def strip(model):
        return run_steps(['strip_cold_images'], CompactionContext(
            messages=conversation(), conv_id='conv-img', task={'model': model},
            constants=constants, dry_run=True))

    assert strip(_MODEL) == 2 * 79
    assert strip('gpt-5.6') == 2 * constants._IMAGE_TOKENS_DEFAULT
