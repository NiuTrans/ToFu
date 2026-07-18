#!/usr/bin/env python3
"""Item #2 (patch→fundamental-fix epic pt_e02044f4ab084dff): the orphaned-user-
turn verdict is BACKEND-authoritative (lib/conversations/reconcile.py
classify_orphan_resumable), replacing the frontend Case-E age<5min heuristic
that could AUTO-FIRE a billed LLM turn — and, on a stale _needsLoad shell,
DOUBLE-ANSWER.

WHY BACKEND (the double-answer bug the frontend can't guard)
-----------------------------------------------------------
The old frontend detected an orphan from either the loaded messages OR a
_needsLoad shell's settings.lastMsgRole metadata — which can be STALE. If a
task actually completed (real DB tail = assistant answer) but the shell
metadata still said trailing-user, Case-E auto-fired a SECOND billed turn on top
of the existing answer. A metadata-only frontend cannot check this; the backend,
with the authoritative `messages`, can: it classifies against the REAL tail.

Tests (pure classifier — no DB/network):
  1. ``test_trailing_user_no_live_task_is_resumable`` — real tail is user, no
     live task, fresh → marker returned (idx + timestamp).
  2. ★ ``test_double_answer_guard_tail_is_assistant`` — the LATENT BUG: the real
     tail is already an ASSISTANT answer (task completed) → NOT resumable, even
     though a stale shell would have said trailing-user. This is the case only
     backend DB-verification closes.
  3. ``test_live_task_never_resumable`` — trailing user BUT a live task exists →
     not resumable (a response is coming).
  4. ``test_stale_orphan_beyond_freshness_bound`` — trailing user but older than
     max_age → not resumable (server policy, not a client guess).
  5. ``test_image_gen_orphan_flagged`` — trailing user is image-gen → marker
     carries isImageGen=True so the frontend skips the affordance.

Double-neuter (on-disk, restored byte-identical):
  NC-1 (detection load-bearing): make classify_orphan_resumable always return
        None → test #1 FAILS (no marker), while the negative tests #2/#3/#4 still
        pass (they already expect None). Proves detection is load-bearing.
  NC-2 (no-double-answer invariant load-bearing): remove the tail-role guard
        (accept ANY tail, not just role=='user') → test #2 FAILS (an assistant
        tail is wrongly marked resumable = the double-answer bug returns), while
        #1 still passes. Proves the role guard is load-bearing.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

_THIS = os.path.abspath(__file__)
_ROOT = os.path.dirname(os.path.dirname(_THIS))
_TARGET = os.path.join(_ROOT, 'lib', 'conversations', 'reconcile.py')

_NOW = 1_000_000_000_000  # fixed epoch-ms for deterministic freshness


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _cls(**kw):
    from lib.conversations.reconcile import classify_orphan_resumable
    return classify_orphan_resumable(**kw)


# ─────────────────────────── positive + negative tests ───────────────────────────

def test_trailing_user_no_live_task_is_resumable():
    msgs = [
        {'role': 'assistant', 'content': 'prev answer', 'finishReason': 'stop', 'timestamp': _NOW - 5000},
        {'role': 'user', 'content': 'a new question', 'timestamp': _NOW - 1000},
    ]
    m = _cls(messages=msgs, has_live_task=False, now_ms=_NOW)
    assert m is not None, 'fresh unanswered trailing-user turn should be resumable'
    assert m['msgIndex'] == 1, f'wrong msgIndex {m}'
    assert m['timestamp'] == _NOW - 1000
    assert m['isImageGen'] is False
    _ok('trailing user + no live task + fresh → resumable marker')


def test_double_answer_guard_tail_is_assistant():
    """★ The latent double-answer bug: real tail already answered → NOT resumable."""
    msgs = [
        {'role': 'user', 'content': 'a question', 'timestamp': _NOW - 3000},
        {'role': 'assistant', 'content': 'here is the answer', 'finishReason': 'stop', 'timestamp': _NOW - 1000},
    ]
    # Even if a stale shell said trailing-user, the AUTHORITATIVE tail is assistant.
    m = _cls(messages=msgs, has_live_task=False, now_ms=_NOW)
    assert m is None, (
        'DOUBLE-ANSWER BUG: an already-answered conv was marked resumable — '
        'a second billed turn would fire on top of the existing answer')
    _ok('★ real tail is assistant answer → NOT resumable (double-answer guard)')


def test_live_task_never_resumable():
    msgs = [{'role': 'user', 'content': 'q', 'timestamp': _NOW - 500}]
    m = _cls(messages=msgs, has_live_task=True, now_ms=_NOW)
    assert m is None, 'a conv with a live task must never be resumable (response coming)'
    _ok('trailing user BUT live task → not resumable')


def test_stale_orphan_beyond_freshness_bound():
    from lib.conversations.reconcile import ORPHAN_RESUMABLE_MAX_AGE_MS
    msgs = [{'role': 'user', 'content': 'ancient', 'timestamp': _NOW - ORPHAN_RESUMABLE_MAX_AGE_MS - 10_000}]
    m = _cls(messages=msgs, has_live_task=False, now_ms=_NOW)
    assert m is None, 'a user turn older than the freshness bound must not be resumable'
    _ok('trailing user beyond freshness bound → not resumable')


def test_image_gen_orphan_flagged():
    msgs = [{'role': 'user', 'content': '🎨 a cat', '_isImageGen': True, 'timestamp': _NOW - 1000}]
    m = _cls(messages=msgs, has_live_task=False, now_ms=_NOW)
    assert m is not None and m['isImageGen'] is True, (
        'image-gen orphan must be marked isImageGen=True so the frontend skips it')
    _ok('image-gen trailing user → marker carries isImageGen=True')


_POSITIVE = [
    test_trailing_user_no_live_task_is_resumable,
    test_double_answer_guard_tail_is_assistant,
    test_live_task_never_resumable,
    test_stale_orphan_beyond_freshness_bound,
    test_image_gen_orphan_flagged,
]


def _run(fn):
    try:
        fn(); return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}'); return False
    except Exception as e:
        import traceback; traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}'); return False


def _neuter(find, repl, label):
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    if src.count(find) != 1:
        raise AssertionError(f'NC anchor not unique for {label}: count={src.count(find)}')
    with open(_TARGET, 'w', encoding='utf-8') as f:
        f.write(src.replace(find, repl, 1))
    return src


def _restore(src):
    with open(_TARGET, 'w', encoding='utf-8') as f:
        f.write(src)


def _subrun(test_name):
    code = (f'import tests.test_orphan_resumable_classifier as t; '
            f'import sys; sys.exit(0 if t._run(t.{test_name}) else 1)')
    r = subprocess.run([sys.executable, '-c', code], cwd=_ROOT, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


def main():
    print()
    print(_color('═══ orphan-resumable classifier — double-neuter ═══', '36'))
    print()
    print(_color('Baseline (shipped code):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed')

    # ── NC-1: detection off → positive test fails, negatives still pass ──
    print()
    print(_color('NC-1 — neuter detection (classify_orphan_resumable → None):', '36'))
    backup = _neuter(
        '    if has_live_task:\n        return None\n    tail = _last_real_turn(messages)',
        '    return None  # NC-1\n    if has_live_task:\n        return None\n    tail = _last_real_turn(messages)',
        'detection')
    try:
        pos_ok, _ = _subrun('test_trailing_user_no_live_task_is_resumable')
        neg_ok, _ = _subrun('test_double_answer_guard_tail_is_assistant')
        if pos_ok:
            _fail('NC-1: positive test PASSED with detection off — not load-bearing!')
        if not neg_ok:
            _fail('NC-1: negative control failed — unexpected blast radius')
        _ok('NC-1: resumable test FAILS with detection off; double-answer guard still passes')
    finally:
        _restore(backup)

    # ── NC-2: remove the tail-role guard → double-answer bug returns ──
    print()
    print(_color("NC-2 — neuter role guard (accept any tail, not just role=='user'):", '36'))
    backup = _neuter(
        "    if tail is None or tail.get('role') != 'user':\n        return None",
        "    if tail is None:\n        return None",
        'tail-role guard')
    try:
        dbl_ok, _ = _subrun('test_double_answer_guard_tail_is_assistant')
        pos_ok, _ = _subrun('test_trailing_user_no_live_task_is_resumable')
        if dbl_ok:
            _fail('NC-2: double-answer test PASSED with role guard removed — bug not guarded!')
        if not pos_ok:
            _fail('NC-2: positive control failed — unexpected blast radius')
        _ok('NC-2: double-answer guard FAILS with role check removed; positive still passes')
    finally:
        _restore(backup)

    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed — file not restored correctly')

    print()
    print(_color('═══ ALL ORPHAN-CLASSIFIER TESTS + DOUBLE-NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
