#!/usr/bin/env python3
"""Root-simplify frontend sync (epic pt_90a4a14875094c3f): fold the persisted
``task_events`` delta log to bootstrap a COLD reconnect's ``state`` snapshot,
instead of the 5s-stale ``task_results`` checkpoint. This closes the cold-replay
window the keep-longer belt masks — moving the "never shrink an in-flight field"
invariant to the SERVER, where the authoritative record lives.

Tests (pure-fn, no DB except the log round-trip):
  1. ``test_fold_reconstructs_exact_buffer`` — a stream of N deltas persisted to
     the log folds back to the EXACT concatenated text the client saw, while the
     5s checkpoint (frozen at delta #k) is SHORTER. ★ THE WINDOW-CLOSE.
  2. ``test_fold_honors_delta_reset`` — a ``delta_reset`` mid-stream clears the
     accumulated content, mirroring sse_pipeline.js, so pre-tool narration is
     dropped from the reconstruction.
  3. ``test_fold_honors_retry_reset`` — a ``retry_reset`` clears content+thinking
     (from-scratch re-run).
  4. ``test_fold_falls_back_to_checkpoint`` — with an EMPTY/failed log, the fold
     returns the checkpoint pair (the residual best-effort-persist-failure case
     the belt's server-side equivalent still covers).
  5. ``test_fold_never_shrinks`` — folded text shorter than the checkpoint (a
     partial log) → the longer checkpoint wins (keep-longer invariant, server-side).

NEUTER: make ``fold_text_from_events`` ignore ``content`` deltas (return '') →
test #1 FAILS (reconstruction empty, checkpoint's short prefix used), proving the
fold is load-bearing; the checkpoint-fallback tests still pass.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'lib', 'tasks_pkg', 'event_fold.py')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('\u2713', '32'), msg)
def _fail(msg): print(' ', _color('\u2717', '31'), msg); sys.exit(1)


def _persist_deltas(task_id, chunks, *, resets=()):
    """Persist a delta stream to task_events; `resets` = {index: 'delta_reset'|'retry_reset'}."""
    from lib.tasks_pkg.event_log import append_persistent_event
    eid = 0
    for i, chunk in enumerate(chunks):
        if i in dict(resets):
            append_persistent_event(task_id, eid, {'type': dict(resets)[i]}); eid += 1
        append_persistent_event(task_id, eid, {'type': 'delta', 'content': chunk}); eid += 1
    return eid


def test_fold_reconstructs_exact_buffer():
    from lib.tasks_pkg.event_fold import fold_cold_state_text
    tid = f'fold-{uuid.uuid4().hex[:8]}'
    chunks = [f'tok{i} ' for i in range(50)]
    _persist_deltas(tid, chunks)
    client_saw = ''.join(chunks)
    # 5s checkpoint froze at delta #20 (the stale window)
    checkpoint = ''.join(chunks[:20])
    content, _thinking = fold_cold_state_text(tid, checkpoint_content=checkpoint)
    assert content == client_saw, (
        f'fold did NOT reconstruct the exact client buffer: got {len(content)} '
        f'chars, expected {len(client_saw)}')
    assert len(content) > len(checkpoint), 'fold not longer than the 5s checkpoint'
    _ok('★ fold reconstructs the EXACT client buffer; longer than the 5s checkpoint')


def test_fold_honors_delta_reset():
    from lib.tasks_pkg.event_fold import fold_cold_state_text
    tid = f'fold-{uuid.uuid4().hex[:8]}'
    # "narration " then a delta_reset, then the real answer
    _persist_deltas(tid, ['narration ', 'real ', 'answer'], resets={1: 'delta_reset'})
    content, _ = fold_cold_state_text(tid)
    assert content == 'real answer', f'delta_reset not honored: got {content!r}'
    _ok('delta_reset clears pre-tool narration (mirrors sse_pipeline.js)')


def test_fold_honors_retry_reset():
    from lib.tasks_pkg.event_fold import fold_text_from_events
    events = [
        {'payload': {'type': 'delta', 'content': 'attempt1 ', 'thinking': 'th1 '}},
        {'payload': {'type': 'retry_reset'}},
        {'payload': {'type': 'delta', 'content': 'attempt2', 'thinking': 'th2'}},
    ]
    content, thinking = fold_text_from_events(events)
    assert content == 'attempt2' and thinking == 'th2', (
        f'retry_reset not honored: content={content!r} thinking={thinking!r}')
    _ok('retry_reset clears content+thinking (from-scratch re-run)')


def test_fold_falls_back_to_checkpoint():
    from lib.tasks_pkg.event_fold import fold_cold_state_text
    tid = f'fold-empty-{uuid.uuid4().hex[:8]}'  # no events persisted
    content, thinking = fold_cold_state_text(
        tid, checkpoint_content='ckpt text', checkpoint_thinking='ckpt think')
    assert content == 'ckpt text' and thinking == 'ckpt think', (
        f'empty-log fallback wrong: {content!r} / {thinking!r}')
    _ok('empty/failed log → falls back to the checkpoint pair (residual case)')


def test_fold_never_shrinks():
    from lib.tasks_pkg.event_fold import fold_cold_state_text
    tid = f'fold-short-{uuid.uuid4().hex[:8]}'
    _persist_deltas(tid, ['ab'])  # folded = 'ab' (2 chars)
    content, _ = fold_cold_state_text(tid, checkpoint_content='a much longer checkpoint')
    assert content == 'a much longer checkpoint', (
        f'keep-longer invariant violated: shorter fold shrank the field: {content!r}')
    _ok('shorter fold never shrinks a longer checkpoint (server-side keep-longer)')


_POSITIVE = [test_fold_reconstructs_exact_buffer, test_fold_honors_delta_reset,
             test_fold_honors_retry_reset, test_fold_falls_back_to_checkpoint,
             test_fold_never_shrinks]


def _run(fn):
    try:
        fn(); return True
    except AssertionError as e:
        print(' ', _color('\u2717', '31'), f'{fn.__name__}: {e}'); return False
    except Exception as e:
        import traceback; traceback.print_exc()
        print(' ', _color('\u2717', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def main():
    print()
    print(_color('\u2550\u2550\u2550 event-log fold cold-replay \u2014 neuter \u2550\u2550\u2550', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_event_fold_cold_replay')

    print(_color('Baseline (shipped code):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed')

    print()
    print(_color('NC \u2014 make fold ignore content deltas:', '36'))
    from tests._nc_harness import neutered_source
    _fixed = "        if etype == 'delta':\n            c = payload.get('content')"
    _broken = "        if etype == 'delta':\n            c = None  # NC"
    with neutered_source(_TARGET, _fixed, _broken):
        recon_ok = _run(test_fold_reconstructs_exact_buffer)
        fb_ok = _run(test_fold_falls_back_to_checkpoint)
        shrink_ok = _run(test_fold_never_shrinks)
    if recon_ok:
        _fail('NC: reconstruction test PASSED with content deltas ignored — fold not load-bearing / test does not pin it')
    if not (fb_ok and shrink_ok):
        _fail('NC: a checkpoint-fallback control failed — unintended blast radius')
    _ok('NC: reconstruction FAILS with content deltas ignored; fallback controls still pass')

    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed')

    print()
    print(_color('\u2550\u2550\u2550 ALL EVENT-FOLD TESTS + NEUTER PASSED \u2550\u2550\u2550', '32'))
    print()


if __name__ == '__main__':
    main()
