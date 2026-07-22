#!/usr/bin/env python3
"""Session-stable TTL latch — chokepoint guarantee suite.

WHY (live evidence, this turn's log analysis):
  144 rounds in one app.log window had ``<ttl-flip>`` as the SOLE break culprit
  (no mid-anchor, no content change) — a pure cache RE-KEY. The stable
  system/tools ``cache_control.ttl`` flipped 1h↔5m between two sends of the same
  conversation because a body reached the wire WITHOUT ``_task_id``, so
  ``add_cache_breakpoints`` fell back to the LIVE GLOBAL ``CACHE_EXTENDED_TTL``
  instead of the per-task latch. When that global differs from the value the
  task latched, the ttl on the stable prefix flips and the ENTIRE prefix is
  re-billed under a new cache key.

THE FIX under test:
  ``stream_llm_response`` (the SINGLE chokepoint every task-based LLM send flows
  through) stamps ``body['_task_id'] = task['id']`` unconditionally before
  dispatch. Individual ``build_body`` call sites set it too, but a
  synthesize-answer / endpoint / future path can forget; stamping at the
  chokepoint makes the latch impossible to bypass.

  Consequence: two sends of the same task ALWAYS resolve the SAME latched TTL,
  even if the live global flips between them → the marker ttl is stable → no
  re-key.

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_ttl_latch_chokepoint.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _marker_ttls(body):
    """The sorted (slot, ttl) list add_cache_breakpoints produced for a body."""
    from lib.tasks_pkg.wire_fingerprint import marker_signature
    return marker_signature(body)['ttls']


def _grow_body(task_id=None):
    """A long OpenAI-shape body that arms the stable system marker."""
    msgs = [{'role': 'system', 'content': 'S' * 40000},
            {'role': 'user', 'content': 'task'}]
    for r in range(8):
        msgs.append({'role': 'assistant', 'content': f'work {r}',
                     'tool_calls': [{'id': f't{r}', 'type': 'function',
                                     'function': {'name': 'rf', 'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'tool_call_id': f't{r}', 'content': 'R' * 1400})
    body = {'model': 'aws.claude-opus-4.8',
            'tools': [{'type': 'function',
                       'function': {'name': 'rf', 'parameters': {}}}],
            'messages': msgs}
    if task_id is not None:
        body['_task_id'] = task_id
    return body


@pytest.mark.unit
def test_no_task_id_reads_volatile_global_and_flips():
    """PRECONDITION (the bug being fixed): a body with NO _task_id resolves the
    LIVE global, so flipping the global between two sends flips the marker ttl —
    the re-key. This proves the latch bypass is real and load-bearing."""
    import lib as _lib
    import lib.llm.cache as C
    from lib.tasks_pkg.cache_tracking import _ttl_latch, latch_extended_ttl  # noqa: F401
    from lib.tasks_pkg.wire_fingerprint import markers_ttl_flipped

    _ttl_latch.clear()
    _lib.CACHE_EXTENDED_TTL = True
    b1 = _grow_body(task_id=None)
    C.add_cache_breakpoints(b1)
    s1 = _marker_ttls(b1)

    _lib.CACHE_EXTENDED_TTL = False  # a settings reload / different resolve
    b2 = _grow_body(task_id=None)
    C.add_cache_breakpoints(b2)
    s2 = _marker_ttls(b2)

    from lib.tasks_pkg.wire_fingerprint import marker_signature
    assert markers_ttl_flipped(marker_signature(b1), marker_signature(b2)) is True, (
        f'without _task_id the ttl must flip with the global (the bug): '
        f's1={s1} s2={s2}')
    _lib.CACHE_EXTENDED_TTL = True  # restore


@pytest.mark.unit
def test_same_task_id_latch_holds_across_global_flip():
    """With a stable _task_id the latch pins the decision — flipping the global
    between sends does NOT flip the marker ttl (the guarantee the chokepoint
    stamp gives every send)."""
    import lib as _lib
    import lib.llm.cache as C
    from lib.tasks_pkg.cache_tracking import _ttl_latch
    from lib.tasks_pkg.wire_fingerprint import marker_signature, markers_ttl_flipped

    _ttl_latch.clear()
    _lib.CACHE_EXTENDED_TTL = True
    b1 = _grow_body(task_id='TASK-STABLE')
    C.add_cache_breakpoints(b1)

    _lib.CACHE_EXTENDED_TTL = False
    b2 = _grow_body(task_id='TASK-STABLE')
    C.add_cache_breakpoints(b2)

    assert markers_ttl_flipped(marker_signature(b1), marker_signature(b2)) is False, (
        'a latched task_id must pin the ttl across a global flip')
    _lib.CACHE_EXTENDED_TTL = True


@pytest.mark.unit
def test_stream_llm_response_stamps_task_id_at_chokepoint():
    """★ THE FIX GUARD (failing-first before the chokepoint stamp). A body that
    arrives at stream_llm_response WITHOUT _task_id must have it stamped from
    task['id'] before dispatch — so no build_body call site can bypass the
    latch. We patch dispatch_stream to capture the body it receives."""
    import lib.tasks_pkg.manager as _mgr

    captured = {}

    def _fake_dispatch(body, **kwargs):
        captured['task_id'] = body.get('_task_id')
        return ({'role': 'assistant', 'content': 'ok'}, 'stop',
                {'prompt_tokens': 10})

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        _thr = __import__('threading')
        task = {'id': 'task-choke-123', 'convId': 'c1', 'content': '',
                'thinking': '', 'config': {}, 'events': [],
                'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}
        body = _grow_body(task_id=None)   # call site FORGOT to set it
        assert '_task_id' not in body, 'precondition: body has no _task_id'
        _mgr.stream_llm_response(task, body, tag='TEST')
    finally:
        _mgr.dispatch_stream = _orig

    assert captured.get('task_id') == 'task-choke-123', (
        f'stream_llm_response must stamp task["id"] onto the body before '
        f'dispatch so the TTL latch cannot be bypassed — got {captured}')


@pytest.mark.unit
def test_stream_llm_response_does_not_clobber_existing_task_id():
    """The stamp must not OVERWRITE a _task_id a call site already set (e.g. the
    swarm agent uses agent_id as its latch key) — only fill it when absent."""
    import lib.tasks_pkg.manager as _mgr

    captured = {}

    def _fake_dispatch(body, **kwargs):
        captured['task_id'] = body.get('_task_id')
        return ({'role': 'assistant', 'content': 'ok'}, 'stop',
                {'prompt_tokens': 10})

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        _thr = __import__('threading')
        task = {'id': 'task-outer', 'convId': 'c1', 'content': '',
                'thinking': '', 'config': {}, 'events': [],
                'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}
        body = _grow_body(task_id='explicit-latch-key')
        _mgr.stream_llm_response(task, body, tag='TEST')
    finally:
        _mgr.dispatch_stream = _orig

    assert captured.get('task_id') == 'explicit-latch-key', (
        f'must not clobber a pre-set _task_id: got {captured}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
