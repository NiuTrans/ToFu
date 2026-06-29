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


def _make_task(task_id='t-abc', auto=True, msg_id=None, **cfg_extra):
    cfg = {'autoTranslate': auto}
    cfg.update(cfg_extra)
    t = {'id': task_id, 'convId': 'conv-1', 'config': cfg}
    if msg_id is not None:
        t['_assistantMsgId'] = msg_id
    return t


def _capture_push(monkeypatch):
    """Capture push_event frames emitted by the incremental worker.

    _Acc._push does ``from lib.push import push_event`` at call time, so we must
    patch the function on the lib.push module (not the inc module alias).
    """
    frames = []
    import lib.push as _push_mod
    monkeypatch.setattr(_push_mod, 'push_event',
                        lambda channel, tid, frame: frames.append((channel, tid, dict(frame))),
                        raising=False)
    return frames


def test_progressive_partial_pushed_before_finalize(monkeypatch):
    """★ The headline live-translation requirement: as each round's segment is
    translated, a running/partial frame is pushed (routed by the assistant
    msgId) BEFORE finalize commits — so the user sees the Chinese fill in
    round-by-round instead of all at once at the end."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    frames = _capture_push(monkeypatch)

    task = _make_task(task_id='t-prog', msg_id='m-live')
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'Second segment.')

    # Wait for both segments to be translated (→ two progressive partials).
    deadline = time.time() + 5
    while time.time() < deadline:
        running = [f for f in frames if f[2].get('status') == 'running' and f[2].get('partial')]
        if len(running) >= 2:
            break
        time.sleep(0.02)

    running = [f for f in frames if f[2].get('status') == 'running' and f[2].get('partial')]
    assert len(running) >= 2, f'expected ≥2 progressive partial frames, got {len(running)}'
    # No 'done' frame yet — finalize hasn't been called.
    assert not [f for f in frames if f[2].get('status') == 'done'], \
        'a done frame was pushed before finalize — partials must precede finalize'
    # Partials are routed by the live assistant msgId, not an index.
    for _, _, fr in running:
        assert fr.get('msgId') == 'm-live'
        assert fr.get('msgIdx') is None
    # The second partial contains BOTH segments (accumulating).
    last_partial = running[-1][2]['partial']
    assert 'ZH:First segment.' in last_partial
    assert 'ZH:Second segment.' in last_partial

    # Now finalize → a done frame lands after the partials.
    inc.finalize_incremental(task, 'conv-1', 5, 'First segment.\n\nSecond segment.', msg_id='m-live')
    deadline = time.time() + 5
    while time.time() < deadline and 'args' not in committed:
        time.sleep(0.02)
    assert 'args' in committed
    assert [f for f in frames if f[2].get('status') == 'done'], 'finalize must push a done frame'


def test_no_progressive_partial_without_msg_id(monkeypatch):
    """When no assistant msgId is known (external / old-frontend start path),
    progressive partials are skipped — but segments still translate and
    finalize still commits (graceful degradation, no regression)."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    frames = _capture_push(monkeypatch)

    task = _make_task(task_id='t-nomid')  # no _assistantMsgId
    inc.submit_round_segment(task, 0, 'Only segment.')
    # Give the worker a moment; no partial should appear.
    time.sleep(0.3)
    assert not [f for f in frames if f[2].get('status') == 'running' and f[2].get('partial')], \
        'no progressive partial should be pushed without an assistant msgId'

    inc.finalize_incremental(task, 'conv-1', 0, 'Only segment.', msg_id='m-z')
    deadline = time.time() + 5
    while time.time() < deadline and 'args' not in committed:
        time.sleep(0.02)
    assert 'args' in committed, 'finalize must still commit without progressive partials'


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


def test_gate_absent_flag_defaults_off_via_resolver(monkeypatch):
    """Phase-2 verification: the gate reads the canonical resolver, so a task
    whose config OMITS autoTranslate defaults OFF — no accumulator is created
    for a stray task, so there is nothing to leak on a later abort."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    t = {'id': 't-noflag', 'convId': 'conv-1', 'config': {'model': 'x'}}  # no autoTranslate key
    assert inc._gate(t) is False
    inc.submit_round_segment(t, 0, 'some english prose')
    with inc._acc_lock:
        assert 't-noflag' not in inc._accumulators, \
            'no accumulator should be created when the resolver defaults OFF'


def test_abort_after_segments_cancel_prevents_leak(monkeypatch):
    """The OFF→abort transition the user called out: a turn with autoTranslate
    ON spun up an accumulator (segments translated), then the turn ABORTS
    mid-stream (no content) — the manager's no-content branch calls
    cancel_incremental, which must tear the worker down so it never sits idle
    to its 300s timeout."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-abort', msg_id='m-abort')
    inc.submit_round_segment(task, 0, 'A segment from before the abort.')
    with inc._acc_lock:
        assert 't-abort' in inc._accumulators   # accumulator is live

    # Simulate the abort path (manager.py no-content/error branch).
    assert inc.cancel_incremental(task) is True
    deadline = time.time() + 5
    while time.time() < deadline:
        with inc._acc_lock:
            if 't-abort' not in inc._accumulators:
                break
        time.sleep(0.02)
    with inc._acc_lock:
        assert 't-abort' not in inc._accumulators, 'aborted accumulator must be torn down'
    assert 'args' not in committed, 'an aborted turn must never commit a translation'


def test_finalize_releases_inflight_guard(monkeypatch):
    """The incremental worker OWNS the in-flight guard the safety net handed it
    (Phase 2); when finalize settles it MUST release the guard so a later
    legitimate re-translate of the same message can claim it."""
    import lib.translate.inflight as ifl
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: None)
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)
    with ifl._lock:
        ifl._inflight.clear()

    # The safety net would have claimed this before handing off to finalize.
    assert ifl.claim_inflight('conv-1', 'm-rel', 4)
    task = _make_task(task_id='t-rel', msg_id='m-rel')
    inc.submit_round_segment(task, 0, 'Segment one.')
    assert inc.finalize_incremental(task, 'conv-1', 4, 'Segment one.', msg_id='m-rel') is True

    deadline = time.time() + 5
    while time.time() < deadline and ifl.is_inflight('conv-1', 'm-rel', 4):
        time.sleep(0.02)
    assert not ifl.is_inflight('conv-1', 'm-rel', 4), \
        'finalize must release the in-flight guard when it settles'


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


def test_cancel_without_accumulator_returns_false():
    # No accumulator was ever created → cancel is a cheap no-op.
    task = _make_task(task_id='t-cancel-none')
    assert inc.cancel_incremental(task) is False


def test_cancel_tears_down_accumulator_without_commit(monkeypatch):
    """A cancelled accumulator drains its worker, removes itself from the
    registry, and NEVER commits — the core orphan-prevention guarantee."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-cancel')
    inc.submit_round_segment(task, 0, 'A segment that gets translated.')
    # Accumulator must exist now.
    with inc._acc_lock:
        assert 't-cancel' in inc._accumulators

    assert inc.cancel_incremental(task) is True

    # Worker should drain the cancel item and self-remove from the registry.
    deadline = time.time() + 5
    while time.time() < deadline:
        with inc._acc_lock:
            if 't-cancel' not in inc._accumulators:
                break
        time.sleep(0.02)
    with inc._acc_lock:
        assert 't-cancel' not in inc._accumulators, 'accumulator not cleaned up after cancel'
    # Cancel must NOT commit anything.
    assert 'args' not in committed, 'cancel must never commit a translation'


def test_finalize_then_cancel_is_harmless(monkeypatch):
    """After finalize takes ownership the accumulator self-cleans; a late
    cancel (the finally-block belt-and-suspenders) must not raise or commit
    twice."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    commits = []
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: commits.append(a))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-fin-cancel')
    inc.submit_round_segment(task, 0, 'Only segment.')
    assert inc.finalize_incremental(task, 'conv-1', 0, 'Only segment.', msg_id='m-x') is True

    # Wait for finalize to commit + clean up.
    deadline = time.time() + 5
    while time.time() < deadline and not commits:
        time.sleep(0.02)
    assert len(commits) == 1
    # A late cancel finds no accumulator → returns False, no second commit.
    assert inc.cancel_incremental(task) is False
    time.sleep(0.1)
    assert len(commits) == 1, 'late cancel must not trigger a second commit'
