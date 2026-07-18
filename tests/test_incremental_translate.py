"""Tests for lib.translate.incremental — per-round incremental translation.

Covers the parts that are pure logic and don't need a real LLM:
  • gating (kill switch / autoTranslate / endpoint / autopilot)
  • deliverable-only translatedContent (narration carried per-segment)
  • whole-content fallback when the deliverable wasn't cached as a segment
  • per-round narration carry (segment_translations + segmentsByRound frame)
  • finalize ownership semantics

The actual LLM call (``_translate_freetext``) is monkeypatched to a
deterministic fake so tests run offline.
"""

import threading
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


def test_progressive_partial_carries_per_round_keys(monkeypatch):
    """★ Per-round interleave (streaming half): the running/partial push frame
    must ALSO carry `partialByRound` — {str(round_num): 中文} — so the frontend
    can route each round's Chinese into its matching .ptool-turn group instead
    of dumping the joined blob below the whole tool panel. round_num ≡ llmRound
    (tool_dispatch.py stamps round_entry['llmRound']=round_num)."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: None)
    frames = _capture_push(monkeypatch)

    task = _make_task(task_id='t-byround', msg_id='m-live')
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'Second segment.')

    deadline = time.time() + 5
    while time.time() < deadline:
        withkeys = [f for f in frames if f[2].get('status') == 'running'
                    and f[2].get('partialByRound')
                    and len(f[2]['partialByRound']) >= 2]
        if withkeys:
            break
        time.sleep(0.02)

    withkeys = [f for f in frames if f[2].get('status') == 'running' and f[2].get('partialByRound')]
    assert withkeys, 'no running frame carried partialByRound'
    last = withkeys[-1][2]['partialByRound']
    # Keys are STRING round numbers (JSON-safe object keys on the wire).
    assert set(last.keys()) == {'0', '1'}, f'expected round keys 0/1, got {sorted(last.keys())}'
    assert last['0'] == 'ZH:First segment.'
    assert last['1'] == 'ZH:Second segment.'
    # The joined blob is STILL present for graceful degrade.
    assert withkeys[-1][2].get('partial'), 'joined partial blob must remain for fallback'


def test_finalize_stamps_translatedText_onto_segments_by_llmRound(monkeypatch):
    """★ Per-round carry to the SETTLED render: finalize hands the commit a
    {round_num: 中文} map; the commit stamps `translatedText` onto each
    non-deliverable text segment of msg['segments'] whose llmRound matches.
    Deliverable/terminal segments get nothing (rendered via translatedContent).
    Keying is by llmRound — exact, never text-equality."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    captured = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: captured.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-stamp', msg_id='m-s')
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'Second segment.')
    content = 'First segment.\n\nSecond segment.'
    assert inc.finalize_incremental(task, 'conv-1', 5, content, msg_id='m-s') is True

    deadline = time.time() + 5
    while time.time() < deadline and 'kw' not in captured:
        time.sleep(0.02)
    assert 'kw' in captured, 'commit was never called'
    seg_trans = captured['kw'].get('segment_translations')
    assert seg_trans is not None, 'segment_translations must be passed to the commit'
    # int round_num keys (the in-process map — JSON string-ification is a
    # wire concern for the push frame, not this commit-side map).
    assert seg_trans.get(0) == 'ZH:First segment.'
    assert seg_trans.get(1) == 'ZH:Second segment.'

    # ── Now drive the REAL stamping helper with a segments list and assert
    #    only the matching non-deliverable text segments get translatedText. ──
    from lib.translate.commit import _stamp_segment_translations
    segments = [
        {'type': 'text', 'text': 'First segment.', 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 't0', 'llmRound': 0},
        {'type': 'text', 'text': 'Second segment.', 'deliverable': False, 'llmRound': 1},
        {'type': 'tool_use', 'id': 't1', 'llmRound': 1},
        {'type': 'text', 'text': 'The final answer.', 'deliverable': True, 'terminal': True},
    ]
    msg = {'segments': segments}
    _stamp_segment_translations(msg, seg_trans)
    assert segments[0]['translatedText'] == 'ZH:First segment.'
    assert segments[2]['translatedText'] == 'ZH:Second segment.'
    # tool_use + the deliverable/terminal text segment are untouched.
    assert 'translatedText' not in segments[1]
    assert 'translatedText' not in segments[3]
    assert 'translatedText' not in segments[4], \
        'deliverable/terminal segment must not be stamped (rendered via translatedContent)'


def test_stamp_segment_translations_is_noop_without_segments():
    """A pre-v36 message (no segments) or an empty map is a clean no-op —
    never raises, never invents a segments list."""
    from lib.translate.commit import _stamp_segment_translations
    m1 = {'content': 'x'}                       # no segments key
    _stamp_segment_translations(m1, {0: 'ZH:x'})
    assert 'segments' not in m1
    m2 = {'segments': [{'type': 'text', 'text': 'a', 'deliverable': False, 'llmRound': 0}]}
    _stamp_segment_translations(m2, {})         # empty map
    assert 'translatedText' not in m2['segments'][0]


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


def test_translatedContent_is_deliverable_only_not_joined_narration(monkeypatch):
    """★ THE fix: translatedContent is the DELIVERABLE answer ONLY, reused from
    the terminal deliverable's cached round segment — NOT the joined translation
    of every round's narration. The narration is carried separately via
    segment_translations (stamped onto seg.translatedText, rendered inline by the
    settled timeline). Committing the join here painted the narration a SECOND
    time as one block below the turn — the reported "all content clumps at the
    tail after finalize" regression."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-order')
    # Rounds 0/1 = inter-round narration (the model called tools after each);
    # round 2 = the terminal deliverable answer. All three are submitted as
    # round segments by the orchestrator.
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'Second segment.')
    inc.submit_round_segment(task, 2, 'The final answer.')

    # `content` handed to finalize is task['content'] = the DELIVERABLE only
    # (inter-round narration was zeroed by _discard_pretool_prose).
    owned = inc.finalize_incremental(task, 'conv-1', 5, 'The final answer.', msg_id='m-1')
    assert owned is True

    deadline = time.time() + 5
    while time.time() < deadline and 'args' not in committed:
        time.sleep(0.02)
    assert 'args' in committed, 'commit was never called'
    # translatedContent == the deliverable's cached translation ONLY — no
    # narration join, so no clumping at the tail.
    translated = committed['args'][3]
    assert translated == 'ZH:The final answer.', \
        f'translatedContent must be deliverable-only, got: {translated!r}'
    assert 'ZH:First segment.' not in translated
    assert 'ZH:Second segment.' not in translated
    # …but the narration IS carried per-segment for the inline timeline.
    seg_trans = committed['kw'].get('segment_translations') or {}
    assert seg_trans.get(0) == 'ZH:First segment.'
    assert seg_trans.get(1) == 'ZH:Second segment.'
    assert seg_trans.get(2) == 'ZH:The final answer.'


def test_done_frame_carries_segments_by_round(monkeypatch):
    """★ The done frame carries `segmentsByRound` ({str(round): 中文}) so the LIVE
    view can stamp msg.segments[].translatedText at finalize WITHOUT a reload —
    keeping the settled timeline's narration in Chinese immediately."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: None)
    frames = _capture_push(monkeypatch)

    task = _make_task(task_id='t-donebyround', msg_id='m-d')
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'The final answer.')
    assert inc.finalize_incremental(task, 'conv-1', 5, 'The final answer.', msg_id='m-d') is True

    deadline = time.time() + 5
    while time.time() < deadline:
        done = [f for f in frames if f[2].get('status') == 'done']
        if done:
            break
        time.sleep(0.02)
    done = [f for f in frames if f[2].get('status') == 'done']
    assert done, 'finalize must push a done frame'
    sbr = done[-1][2].get('segmentsByRound')
    assert sbr is not None, 'done frame must carry segmentsByRound'
    # String keys (JSON object), all translated rounds present.
    assert sbr.get('0') == 'ZH:First segment.'
    assert sbr.get('1') == 'ZH:The final answer.'


def test_deliverable_not_cached_falls_back_to_whole(monkeypatch):
    """When the deliverable content has no matching cached round segment (e.g. a
    Sources-footer / content-filter override rewrote task['content'] after the
    segment was captured), finalize translates the deliverable content afresh
    via the whole-content fallback — still deliverable-only, never a narration
    join."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    task = _make_task(task_id='t-cover')
    inc.submit_round_segment(task, 0, 'Some narration.')
    # The deliverable content does NOT match the cached narration segment.
    deliverable = 'A rewritten final answer with an appended sources footer.'
    owned = inc.finalize_incremental(task, 'conv-1', 0, deliverable, msg_id='m-2')
    assert owned is True

    deadline = time.time() + 5
    while time.time() < deadline and 'args' not in committed:
        time.sleep(0.02)
    assert 'args' in committed
    translated = committed['args'][3]
    # Fresh whole-content translation of the deliverable ONLY (fake prefixes ZH:).
    assert translated == ('ZH:' + deliverable).strip()
    assert 'ZH:Some narration.' not in translated


def _install_fake_task_registry(monkeypatch, task_id, *, alive):
    """Install a fake manager `tasks` registry so `_task_alive` resolves
    deterministically. `alive=True` → the task is present + running; False →
    absent (the crash/superseded case)."""
    import lib.tasks_pkg.manager as _mgr
    fake = {}
    if alive:
        fake[task_id] = {'id': task_id, 'status': 'running', 'aborted': False}
    monkeypatch.setattr(_mgr, 'tasks', fake, raising=False)
    monkeypatch.setattr(_mgr, 'tasks_lock', threading.Lock(), raising=False)
    return fake


def test_slow_round_past_idle_timeout_preserves_earlier_segments(monkeypatch):
    """★ THE root-cause regression: a single tool round outlasts the worker's
    idle timeout while the task is STILL ALIVE. The already-translated earlier
    rounds must NOT be discarded — the SAME accumulator must survive so a later
    finalize commits their per-round Chinese (segment_translations), instead of
    a fresh empty accumulator translating only the deliverable.

    This is the exact production failure: rounds 0-2 translated, then round-3
    ran ~5min on a FUSE mount, the worker self-destructed at 300s, and finalize
    committed with only the deliverable (all narration translatedText empty)."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    # Shrink the idle window so the "slow round" gap trips it near-instantly.
    monkeypatch.setattr(inc, '_WORKER_IDLE_TIMEOUT', 0.15)
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    tid = 't-slow-round'
    # The task is ALIVE the whole time (simulating the long round-3 tool call).
    _install_fake_task_registry(monkeypatch, tid, alive=True)

    task = _make_task(task_id=tid, msg_id='m-slow')
    # Rounds 0-2 close quickly and get translated.
    inc.submit_round_segment(task, 0, 'First segment.')
    inc.submit_round_segment(task, 1, 'Second segment.')
    inc.submit_round_segment(task, 2, 'Third segment.')

    # Now the "slow round": NO queue item for well past the idle timeout, while
    # the task stays alive. The worker MUST keep the accumulator parked.
    time.sleep(0.6)  # 4× the shrunk idle window
    with inc._acc_lock:
        acc = inc._accumulators.get(tid)
    assert acc is not None, \
        'accumulator was reclaimed while the task was still alive — segments lost'
    with acc.lock:
        assert set(acc.segments.keys()) == {0, 1, 2}, \
            f'earlier segments were discarded during the slow round: {sorted(acc.segments.keys())}'

    # Round-3 finally closes and the turn finalizes on the DELIVERABLE.
    inc.submit_round_segment(task, 3, 'The final answer.')
    assert inc.finalize_incremental(task, 'conv-1', 5, 'The final answer.', msg_id='m-slow') is True

    deadline = time.time() + 5
    while time.time() < deadline and 'kw' not in committed:
        time.sleep(0.02)
    assert 'kw' in committed, 'finalize never committed'
    seg_trans = committed['kw'].get('segment_translations') or {}
    # ALL four rounds' Chinese survived into the committed message — the earlier
    # rounds were NOT lost to the idle timeout.
    assert seg_trans.get(0) == 'ZH:First segment.'
    assert seg_trans.get(1) == 'ZH:Second segment.'
    assert seg_trans.get(2) == 'ZH:Third segment.'
    assert seg_trans.get(3) == 'ZH:The final answer.'


def test_dead_task_worker_still_reclaimed_on_idle_timeout(monkeypatch):
    """★ The anti-leak invariant must survive the fix: a worker whose task is
    GONE (crashed / superseded — no finalize, no cancel ever arrives) must still
    be reclaimed from the registry on the idle timeout. Otherwise a genuinely
    orphaned accumulator would linger forever."""
    monkeypatch.setenv(inc._KILL_ENV, '1')
    monkeypatch.setattr(inc, '_WORKER_IDLE_TIMEOUT', 0.15)
    monkeypatch.setattr('lib.translate.engine._translate_freetext', _fake_translate)
    committed = {}
    monkeypatch.setattr('lib.translate.commit._commit_translation_to_db',
                        lambda *a, **k: committed.update(args=a, kw=k))
    monkeypatch.setattr(inc, 'push_event', lambda *a, **k: None, raising=False)

    tid = 't-dead'
    # The task is ABSENT from the registry (crashed / superseded).
    _install_fake_task_registry(monkeypatch, tid, alive=False)

    task = _make_task(task_id=tid, msg_id='m-dead')
    inc.submit_round_segment(task, 0, 'Orphaned segment.')
    with inc._acc_lock:
        assert tid in inc._accumulators  # accumulator spun up

    # No finalize, no cancel. The worker idles, probes liveness → dead → reclaim.
    deadline = time.time() + 5
    while time.time() < deadline:
        with inc._acc_lock:
            if tid not in inc._accumulators:
                break
        time.sleep(0.02)
    with inc._acc_lock:
        assert tid not in inc._accumulators, \
            'a dead task must still have its worker reclaimed on idle timeout'
    assert 'args' not in committed, 'an orphaned/dead task must never commit'


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
