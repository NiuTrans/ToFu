"""Tests for B — streaming the manual /compact summary to the compaction card.

The summary LLM call is ~96% of a manual /compact's wall clock (measured). B
makes the wait FEEL faster: instead of a single blocking dispatch_chat, the
summary is streamed and each delta is pushed on an independent ('compaction',
conv_id) push channel so the frontend card can "grow" the summary live.

Invariants:
  1. _generate_query_aware_summary(on_delta=fn) streams via dispatch_stream and
     forwards every content delta to on_delta, returning the full accumulated
     text (identical result to the non-streaming path).
  2. With NO on_delta it keeps the non-streaming dispatch_chat path (back-compat).
  3. compact_conversation_now pushes summary_start → summary_delta* →
     summary_done on channel 'compaction' keyed by conv_id, so an idle-state
     compaction (no live task) still drives a live card.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -p no:napari \
        tests/test_summary_streaming.py
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


# ── (1) streaming path: on_delta receives every delta, returns full text ──

def test_summary_on_delta_streams_and_accumulates(monkeypatch):
    import lib.tasks_pkg.compaction._layer2._summary as summ

    chunks = ['Hello ', 'streamed ', 'summary.']

    def fake_dispatch_stream(messages, *, on_content=None, **kw):
        for c in chunks:
            on_content(c)
        # Real dispatch_stream returns the assistant message as a DICT
        # ({'role':'assistant','content':...}), NOT a bare string — mirror
        # that contract so this test guards the unwrap in _summary.py.
        return ({'role': 'assistant', 'content': ''.join(chunks)}, 'stop',
                {'prompt_tokens': 10, 'completion_tokens': 3})

    # dispatch_stream is imported lazily inside the fn from lib.llm_dispatch
    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_stream', fake_dispatch_stream, raising=False)

    got = []
    result = summ._generate_query_aware_summary(
        [{'role': 'user', 'content': 'q'}], 'q',
        conv_id='c1', on_delta=lambda t: got.append(t))

    assert got == chunks, f'deltas not forwarded verbatim: {got}'
    assert result == 'Hello streamed summary.'


def test_unverified_stream_prefix_never_becomes_durable_summary(monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.tasks_pkg.compaction._layer2._summary as summ
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

    calls = []

    def malformed_stream(_messages, *, on_content=None, **_kwargs):
        calls.append(1)
        if on_content:
            on_content('unsafe half-summary')
        return ProviderStreamResult(
            message={'role': 'assistant', 'content': 'unsafe half-summary'},
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.MALFORMED_STREAM,
            malformed_frame_count=1,
        )

    monkeypatch.setattr(dispatch, 'dispatch_stream', malformed_stream)

    result = summ._generate_query_aware_summary(
        [{'role': 'user', 'content': 'q'}],
        'q',
        conv_id='cut-summary',
        on_delta=lambda _text: None,
    )

    assert result is None
    assert len(calls) == 2  # cheap tier, then the existing text-tier fallback


def test_summary_no_on_delta_uses_nonstreaming(monkeypatch):
    """Back-compat: without on_delta, the non-streaming dispatch_chat path is
    used and no streaming occurs."""
    import lib.tasks_pkg.compaction._layer2._summary as summ
    import lib.llm_dispatch as ld

    called = {'chat': 0, 'stream': 0}

    def fake_chat(messages, **kw):
        called['chat'] += 1
        return ('NONSTREAM SUMMARY', {'prompt_tokens': 5, 'completion_tokens': 2})

    def fake_stream(*a, **k):
        called['stream'] += 1
        return ('x', 'stop', {})

    monkeypatch.setattr(ld, 'dispatch_chat', fake_chat, raising=False)
    monkeypatch.setattr(ld, 'dispatch_stream', fake_stream, raising=False)

    result = summ._generate_query_aware_summary(
        [{'role': 'user', 'content': 'q'}], 'q', conv_id='c1')
    assert result == 'NONSTREAM SUMMARY'
    assert called['chat'] == 1 and called['stream'] == 0, called


def test_codex_subscription_auto_summary_streams_and_pins(monkeypatch):
    """Automatic L2 compaction must not escape a stream-only Codex slot.

    The old non-streaming path excluded every managed Codex model, then paid a
    different provider tagged ``cheap``.  A task already served by
    ``oauth_codex`` must instead use streaming dispatch under a provider pin.
    """
    import lib.llm_dispatch as ld
    import lib.tasks_pkg.compaction._layer2._summary as summ
    from lib.llm_dispatch.provider_pin import (
        clear_pinned_provider, get_pinned_provider)

    seen = {'chat': 0, 'stream': 0, 'pin': None, 'on_content': 'unset'}

    def fake_chat(*args, **kwargs):
        seen['chat'] += 1
        raise AssertionError('Codex subscription summary used non-stream chat')

    def fake_stream(messages, *, on_content=None, **kwargs):
        seen['stream'] += 1
        seen['pin'] = get_pinned_provider()
        seen['on_content'] = on_content
        return ({'role': 'assistant', 'content': 'CODEX SUMMARY'}, 'stop',
                {'prompt_tokens': 11, 'completion_tokens': 2})

    monkeypatch.setattr(ld, 'dispatch_chat', fake_chat)
    monkeypatch.setattr(ld, 'dispatch_stream', fake_stream)
    clear_pinned_provider()
    try:
        result = summ._generate_query_aware_summary(
            [{'role': 'user', 'content': 'q'}], 'q', conv_id='codex-conv',
            task={'convId': 'codex-conv', 'provider_id': 'oauth_codex',
                  'config': {'model': 'gpt-5.6-luna'}})
    finally:
        clear_pinned_provider()

    assert result == 'CODEX SUMMARY'
    assert seen == {
        'chat': 0, 'stream': 1, 'pin': 'oauth_codex', 'on_content': None,
    }


def test_non_codex_provider_pin_is_never_overridden(monkeypatch):
    import lib.llm_dispatch as ld
    import lib.tasks_pkg.compaction._layer2._summary as summ
    from lib.llm_dispatch.provider_pin import (
        clear_pinned_provider, get_pinned_provider, set_pinned_provider)

    seen = {'pin': None}

    def fake_chat(messages, **kwargs):
        seen['pin'] = get_pinned_provider()
        return 'LOCAL SUMMARY', {'prompt_tokens': 3, 'completion_tokens': 1}

    monkeypatch.setattr(ld, 'dispatch_chat', fake_chat)
    set_pinned_provider('ephemeral:user-owned')
    try:
        result = summ._generate_query_aware_summary(
            [{'role': 'user', 'content': 'q'}], 'q', conv_id='c',
            task={'provider_id': 'oauth_codex', 'config': {'model': 'gpt-5.6-luna'}})
    finally:
        clear_pinned_provider()

    assert result == 'LOCAL SUMMARY'
    assert seen['pin'] == 'ephemeral:user-owned'


def test_recovered_task_infers_unique_codex_provider_from_live_slots(
        monkeypatch):
    """No task provider/sticky key after restart must not force non-stream IO."""
    import lib.llm_dispatch as ld
    import lib.tasks_pkg.compaction._layer2._summary as summ
    from lib.llm_dispatch.provider_pin import (
        clear_pinned_provider, get_pinned_provider)
    from lib.llm_dispatch.slot import Slot

    slot = Slot(
        key_name='oauth-key', api_key='managed', model='gpt-wire',
        logical_model='gpt-logical', capabilities={'text'},
        base_url='https://example.invalid/codex',
        provider_id='oauth_codex', oauth='codex', stream_only=True,
    )

    class _Dispatcher:
        slots = [slot]

        @staticmethod
        def initialize():
            return None

    monkeypatch.setattr(ld, 'get_dispatcher', lambda: _Dispatcher())
    monkeypatch.setattr(
        'lib.llm_dispatch.conv_affinity.get_preferred_key', lambda _cid: None)
    monkeypatch.setattr(
        ld, 'dispatch_chat',
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError('recovered Codex task used non-stream chat')))
    seen = {'pin': None}

    def _stream(*_args, **_kwargs):
        seen['pin'] = get_pinned_provider()
        return ({'role': 'assistant', 'content': 'RECOVERED SUMMARY'},
                'stop', {'prompt_tokens': 3, 'completion_tokens': 2})

    monkeypatch.setattr(ld, 'dispatch_stream', _stream)
    clear_pinned_provider()
    try:
        result = summ._generate_query_aware_summary(
            [{'role': 'user', 'content': 'q'}], 'q',
            conv_id='recovered-codex',
            task={'convId': 'recovered-codex',
                  'config': {'model': 'gpt-logical'}})
    finally:
        clear_pinned_provider()

    assert result == 'RECOVERED SUMMARY'
    assert seen['pin'] == 'oauth_codex'


# ── (3) compact_conversation_now pushes start/delta/done on 'compaction' ──

class _Store:
    def __init__(self, msgs):
        self.messages = []
        for index, message in enumerate(msgs):
            projected = dict(message)
            projected.update({
                '_turnId': f'turn-{index}',
                '_projectionRevision': 1,
                '_turnActor': ('human' if message.get('role') == 'user'
                               else 'assistant'),
            })
            self.messages.append(projected)
        self.updated_at = 1000
        self.rev = 0
    def load_transcript(self, cid, *, user_id):
        return (list(self.messages), self.updated_at, self.rev)
    def compact_turn_transcript(
            self, cid, current, compacted, expected_rev, *, command_id,
            user_id):
        if expected_rev != self.rev:
            return 0
        self.messages = list(compacted); self.rev += 1; return 1
    def update_archive_summary(self, *a, **k): pass
    def notify_conversation_changed(self, *a, **k): pass


def _long_conv():
    # Big enough to clear _MANUAL_COMPACT_MIN_TOKENS (4000): ~30 turns of
    # ~2k-char assistants → well above the floor so a plan is produced.
    msgs = [{'role': 'user', 'content': '原始目标：修复 bug', 'timestamp': 1000},
            {'role': 'assistant', 'content': '好的', 'timestamp': 1001}]
    for t in range(1, 30):
        msgs.append({'role': 'user', 'content': f'第{t}步指令', 'timestamp': 1000 + t * 10})
        msgs.append({'role': 'assistant', 'content': 'done ' + ('y' * 2000),
                     'timestamp': 1000 + t * 10 + 1})
    return msgs


def test_compact_pushes_streaming_events(monkeypatch):
    import lib.tasks_pkg.compaction._manual as man

    store = _Store(_long_conv())
    monkeypatch.setattr(man, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(man, '_archive_transcript', lambda *a, **k: '7')
    monkeypatch.setattr(man, '_extract_recently_accessed_files', lambda m: [])

    # streaming summary: emit two deltas via the on_delta the engine passes in
    def fake_summary(messages, current_query, *a, on_delta=None, **k):
        if on_delta:
            on_delta('partial one ')
            on_delta('partial two')
        return 'partial one partial two'
    monkeypatch.setattr(man, '_generate_query_aware_summary', fake_summary)

    events = []
    monkeypatch.setattr(man, 'push_event',
                        lambda channel, task_id, payload, *, user_id:
                        events.append((channel, task_id, payload.get('type'),
                                       payload, user_id)))

    res = man.compact_conversation_now('convX', user_id=1, config={}, task={'convId': 'convX'})
    assert res['ok'] is True

    # all pushes on the 'compaction' channel keyed by conv id
    assert events, 'no push events emitted'
    assert all(ch == 'compaction' and tid == 'convX' and uid == 1
               for ch, tid, _t, _p, uid in events), events
    types = [t for _c, _t2, t, _p, _uid in events]
    assert types[0] == 'summary_start', types
    assert 'summary_delta' in types, types
    assert types[-1] == 'summary_done', types

    # deltas carry the streamed text; done carries the final stats
    deltas = [p['text'] for _c, _t, t, p, _uid in events
              if t == 'summary_delta']
    assert deltas == ['partial one ', 'partial two'], deltas
    done = [p for _c, _t, t, p, _uid in events if t == 'summary_done'][0]
    assert done.get('archiveId') == '7'
    assert done.get('tokensAfter') == res['tokensAfter']


def test_compact_push_failure_never_breaks_compaction(monkeypatch):
    """A push failure (no client, hub down) must NOT fail the compaction —
    the DB rewrite is the source of truth; the live card is best-effort."""
    import lib.tasks_pkg.compaction._manual as man
    store = _Store(_long_conv())
    monkeypatch.setattr(man, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(man, '_archive_transcript', lambda *a, **k: '7')
    monkeypatch.setattr(man, '_extract_recently_accessed_files', lambda m: [])
    monkeypatch.setattr(man, '_generate_query_aware_summary',
                        lambda *a, on_delta=None, **k: (on_delta and on_delta('x')) or 'SUM')

    def boom(*a, **k):
        raise RuntimeError('hub exploded')
    monkeypatch.setattr(man, 'push_event', boom)

    res = man.compact_conversation_now('convX', user_id=1, config={}, task={'convId': 'convX'})
    assert res['ok'] is True, 'push failure must not break the compaction'
