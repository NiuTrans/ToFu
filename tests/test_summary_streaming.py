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


# ── (3) compact_conversation_now pushes start/delta/done on 'compaction' ──

class _Store:
    def __init__(self, msgs):
        self.messages = list(msgs)
        self.updated_at = 1000
    def load_conversation_messages(self, cid):
        return (list(self.messages), self.updated_at)
    def cas_sync_conversation_with_search(self, cid, m, u):
        self.messages = list(m); return 1
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
    monkeypatch.setattr(man, '_archive_transcript', lambda *a, **k: 7)
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
                        lambda channel, task_id, payload: events.append(
                            (channel, task_id, payload.get('type'), payload)))

    res = man.compact_conversation_now('convX', config={}, task={'convId': 'convX'})
    assert res['ok'] is True

    # all pushes on the 'compaction' channel keyed by conv id
    assert events, 'no push events emitted'
    assert all(ch == 'compaction' and tid == 'convX' for ch, tid, _t, _p in events), events
    types = [t for _c, _t2, t, _p in events]
    assert types[0] == 'summary_start', types
    assert 'summary_delta' in types, types
    assert types[-1] == 'summary_done', types

    # deltas carry the streamed text; done carries the final stats
    deltas = [p['text'] for _c, _t, t, p in events if t == 'summary_delta']
    assert deltas == ['partial one ', 'partial two'], deltas
    done = [p for _c, _t, t, p in events if t == 'summary_done'][0]
    assert done.get('archiveId') == 7
    assert done.get('tokensAfter') == res['tokensAfter']


def test_compact_push_failure_never_breaks_compaction(monkeypatch):
    """A push failure (no client, hub down) must NOT fail the compaction —
    the DB rewrite is the source of truth; the live card is best-effort."""
    import lib.tasks_pkg.compaction._manual as man
    store = _Store(_long_conv())
    monkeypatch.setattr(man, 'get_conversation_store', lambda: store)
    monkeypatch.setattr(man, '_archive_transcript', lambda *a, **k: 7)
    monkeypatch.setattr(man, '_extract_recently_accessed_files', lambda m: [])
    monkeypatch.setattr(man, '_generate_query_aware_summary',
                        lambda *a, on_delta=None, **k: (on_delta and on_delta('x')) or 'SUM')

    def boom(*a, **k):
        raise RuntimeError('hub exploded')
    monkeypatch.setattr(man, 'push_event', boom)

    res = man.compact_conversation_now('convX', config={}, task={'convId': 'convX'})
    assert res['ok'] is True, 'push failure must not break the compaction'
