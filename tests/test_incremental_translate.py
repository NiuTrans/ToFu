"""Tests for lib.translate.incremental — per-round incremental translation.

Covers the parts that are pure logic and don't need a real LLM:
  • gating (kill switch / autoTranslate / endpoint / autopilot)
  • segment assembly in round order
  • coverage-based fallback to whole-content translation
  • finalize ownership semantics

The actual LLM call (``_translate_freetext``) is monkeypatched to a
deterministic fake so tests run offline.
"""

import time

import lib.translate.incremental as inc


def _fake_translate(text, system_prompt, source='', target='', **kw):
    """Deterministic fake: prefix each line so output != input but is derivable."""
    return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}


def _wait_idle(acc, timeout=5.0):
    """Wait until the accumulator's queue is drained + thread settled."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if acc.q.empty():
            return
        time.sleep(0.02)


def _make_task(task_id='t-abc', auto=True, **cfg_extra):
    cfg = {'autoTranslate': auto}
    cfg.update(cfg_extra)
    return {'id': task_id, 'convId': 'conv-1', 'config': cfg}


def test_gate_respects_autotranslate_off(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '1')
    assert inc._gate(_make_task(auto=True)) is True
    assert inc._gate(_make_task(auto=False)) is False


def test_gate_kill_switch(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '0')
    assert inc._gate(_make_task(auto=True)) is False
    monkeypatch.setenv(inc._KILL_ENV, '1')
    assert inc._gate(_make_task(auto=True)) is True


def test_gate_excludes_endpoint_and_autopilot(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '1')
    t = _make_task()
    t['_endpoint_managed'] = True
    assert inc._gate(t) is False
    t2 = _make_task()
    t2['endpoint_mode'] = True
    assert inc._gate(t2) is False
    t3 = _make_task()
    t3['_autopilot_kick'] = True
    assert inc._gate(t3) is False
    t4 = _make_task()
    t4['_inline_messages'] = True
    assert inc._gate(t4) is False


def test_gate_requires_conv_id(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '1')
    t = _make_task()
    t['convId'] = ''
    assert inc._gate(t) is False


def test_assemble_joins_segments_in_order(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    # Avoid real DB commits / push frames.
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-order')
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'Second segment.')
    inc.submit_round_segment(task, 2, 'Third and final.')

    content = 'First segment.\n\nSecond segment.\n\nThird and final.'
    owned = inc.finalize_incremental(task, 'conv-1', 5, content, msg_id='m-1')
    assert owned is True

    # Wait for the worker to drain + finalize.
    with inc._acc_lock:
        acc = inc._accumulators.get('t-order')
    # acc may already be cleaned up; if so, the commit happened.
    deadline = time.time() + 5
    while time.time() < deadline and 'args' not in committed:
        time.sleep(0.02)
    assert 'args' in committed, 'commit was never called'
    # _commit_translation_to_db(conv_id, msg_idx, field, translated, ...)
    translated = committed['args'][3]
    assert translated == 'ZH:First segment.\n\nZH:Second segment.\n\nZH:Third and final.'


def test_assemble_low_coverage_falls_back_to_whole(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-cover')
    # Only submit a tiny segment, but the final content is much larger →
    # coverage below threshold → whole-content fallback.
    inc.submit_round_segment(task, 0, 'tiny')
    big_content = 'tiny ' + ('extra prose ' * 200)
    owned = inc.finalize_incremental(task, 'conv-1', 0, big_content, msg_id='m-2')
    assert owned is True

    deadline = time.time() + 5
    while time.time() < deadline and 'args' not in committed:
        time.sleep(0.02)
    assert 'args' in committed
    translated = committed['args'][3]
    # Whole-content fallback translates the ENTIRE content (ZH: + big_content),
    # then .strip()s the result (so a trailing space in big_content is dropped).
    assert translated == ('ZH:' + big_content).strip()


def test_finalize_without_accumulator_declines():
    # No submit_round_segment call → no accumulator → caller must fall back.
    task = _make_task(task_id='t-none')
    owned = inc.finalize_incremental(task, 'conv-1', 0, 'whatever', msg_id='m-3')
    assert owned is False


def test_submit_noop_when_gate_off(monkeypatch):
    monkeypatch.setenv(inc._KILL_ENV, '0')
    task = _make_task(task_id='t-off')
    inc.submit_round_segment(task, 0, 'hello')
    with inc._acc_lock:
        assert 't-off' not in inc._accumulators
