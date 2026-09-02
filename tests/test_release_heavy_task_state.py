#!/usr/bin/env python3
"""Terminal chat tasks must RELEASE their heavy input state to bound RSS.

Root cause (measured 2026-07-11): essentially all of the server's ~3.3 GB
private-dirty heap is per-task state, not import baseline. Each finished chat
task pins its full API-message context (``task['messages']``) and per-turn
endpoint snapshots (``task['_flow_turns']``) for the ttl=3600s retention
window — and forever for never-finalized carriers. Those fields have NO reader
after the turn reaches a terminal state: every post-terminal consumer
(chat_poll DB path, killed-recovery, reconcile) rebuilds from the DB.

``lib.tasks_pkg.manager._release_heavy_task_state`` nulls those fields on a
terminal task. This suite asserts, against the REAL function:

  * terminal task → messages + _flow_turns are released (set None);
  * ``events`` / ``content`` / ``thinking`` are KEPT (a reconnecting SSE client
    replays the retained absolute-cursor tail; content/thinking are thin and
    read by pollers);
  * a NON-terminal (running) task is UNTOUCHED (defensive — never strip a task
    that could still stream);
  * NC: with the release neutered, the heavy fields survive a terminal persist
    → proves the release is load-bearing (the leak returns).

Pure (no DB) — the release logic is a dict mutation gated on status.

Standalone runner + importable pytest functions.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _big_task(status='done'):
    """A task carrying a heavy conversation context + endpoint snapshot."""
    messages = [{'role': 'user' if i % 2 == 0 else 'assistant',
                 'content': 'x' * 4000, '_msgId': f'm{i}'} for i in range(250)]
    return {
        'id': 'tk-heavy-0001',
        'convId': 'cv-heavy',
        'status': status,
        'messages': messages,
        '_flow_turns': [{'content': 'y' * 20000} for _ in range(8)],
        'events': [{'type': 'delta', 'seq': i, 'content': 'd' * 500}
                   for i in range(30)],
        'content': 'final answer',
        'thinking': 'some reasoning',
    }


def test_terminal_task_releases_heavy_fields():
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    task = _big_task('done')
    n = _release_heavy_task_state(task)
    assert n == 2, f'expected 2 fields released, got {n}'
    assert task['messages'] is None, 'task["messages"] not released'
    assert task['_flow_turns'] is None, 'task["_flow_turns"] not released'
    _ok('terminal task → messages + _flow_turns released')


def test_lightweight_fields_are_kept():
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    task = _big_task('done')
    _release_heavy_task_state(task)
    # events MUST survive — reconnect replays the retained absolute-cursor tail.
    assert isinstance(task['events'], list) and len(task['events']) == 30, \
        'events wrongly dropped — breaks SSE reconnect within TTL'
    assert task['content'] == 'final answer', 'content wrongly dropped'
    assert task['thinking'] == 'some reasoning', 'thinking wrongly dropped'
    _ok('events / content / thinking KEPT (SSE reconnect + poll intact)')


def test_running_task_untouched():
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    task = _big_task('running')
    n = _release_heavy_task_state(task)
    assert n == 0, f'running task released {n} fields — must be 0'
    assert task['messages'] is not None and len(task['messages']) == 250, \
        'running task lost its messages — could still be streaming!'
    _ok('running (non-terminal) task is UNTOUCHED (defensive)')


def test_error_and_aborted_also_release():
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    for st in ('error', 'aborted'):
        task = _big_task(st)
        _release_heavy_task_state(task)
        assert task['messages'] is None, f'{st} task did not release messages'
    _ok('error + aborted terminal states also release heavy fields')


def test_terminal_releases_coalesce_one_maintenance_heap_trim(monkeypatch):
    """Freed terminal inputs must become lower RSS without trimming per task."""
    from lib.tasks_pkg.manager import _maintenance as maintenance
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state

    maintenance._released_task_heap_trim_requested.clear()
    trim_calls = []
    monkeypatch.setattr(
        maintenance, '_malloc_trim', lambda: trim_calls.append(True) or True)

    _release_heavy_task_state(_big_task('done'))
    _release_heavy_task_state(_big_task('done'))

    assert maintenance.trim_released_task_heap() is True
    assert maintenance.trim_released_task_heap() is False
    assert trim_calls == [True]


_POSITIVE = [
    test_terminal_task_releases_heavy_fields,
    test_lightweight_fields_are_kept,
    test_running_task_untouched,
    test_error_and_aborted_also_release,
]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def _neuter_and_check():
    """NC: replace _release_heavy_task_state with a no-op and confirm a
    terminal task then RETAINS its heavy fields → the leak returns, proving
    the real release is load-bearing."""
    import lib.tasks_pkg.manager._persist as mgr
    task = _big_task('done')
    orig = mgr._release_heavy_task_state
    try:
        mgr._release_heavy_task_state = lambda _t: 0   # neutered
        mgr._release_heavy_task_state(task)
        leaked = task['messages'] is not None and task['_flow_turns'] is not None
        return leaked, ('messages retained=%s turns retained=%s' % (
            task['messages'] is not None, task['_flow_turns'] is not None))
    finally:
        mgr._release_heavy_task_state = orig


def main():
    print()
    print(_color('═══ release heavy terminal task state (RSS-at-source) + neuter ═══', '36'))
    print()

    print(_color('Baseline (shipped release):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix _release_heavy_task_state / persist wiring first')

    print()
    print(_color('NC — neuter the release, repeat a terminal persist:', '36'))
    leaked, out = _neuter_and_check()
    if not leaked:
        _fail('NC did not confirm the release is load-bearing:\n' + out)
    _ok('NC: with the release dead, a terminal task retains its heavy fields (leak returns)')

    print()
    print(_color('═══ ALL RELEASE TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
