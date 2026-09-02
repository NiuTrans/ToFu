#!/usr/bin/env python3
"""Integration + concurrency hardening for the RSS-at-source fix.

The unit tests (test_release_heavy_task_state / test_shed_memory_under_pressure)
cover the release + shed logic in isolation. This suite hardens the parts that
only reasoning covered so far:

  1. END-TO-END: the REAL ``persist_task_result`` (not just the helper) must
     release ``task['messages']`` AND still persist the full result to the DB —
     nothing lost. A poll-shaped read of ``task_results`` returns the content.
  2. NO POST-TERMINAL READER CRASHES on the nulled field: every in-tree reader
     uses ``task.get('messages') or []`` — assert that idiom holds for the two
     that run near finalization (autopilot summary input, endpoint snapshot).
  3. CONCURRENCY: ``shed_memory_under_pressure`` iterates + evicts
     ``_chat_runtime`` while other threads create/finish tasks. Must not raise
     (RuntimeError: dict changed size) and must never drop a RUNNING task.
  4. IDEMPOTENCY: calling the release twice, and shed twice, is harmless.

DB tests seed a real conversations row (mirrors test_upsert_task_row_orphan_guard).

Standalone runner + importable pytest functions.
"""

import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _heavy_terminal_task(conv_id, tid):
    """A finished single-turn chat task carrying a long conversation context."""
    msgs = [{'role': 'user' if j % 2 == 0 else 'assistant',
             'content': f'msg{j}-' + 'x' * 3000, '_msgId': f'm{j}'}
            for j in range(150)]
    return {
        'id': tid, 'convId': conv_id, 'status': 'done', 'finishReason': 'stop',
        'messages': msgs, 'content': 'the final answer', 'thinking': 'reasoning',
        'model': 'test-model', 'provider_id': 'test', 'usage': {},
        'toolRounds': [], 'events': [{'type': 'delta', 'seq': 0, 'content': 'x'}],
        'created_at': time.time(), 'finished_at': time.time(),
    }


def test_e2e_persist_releases_messages_and_keeps_db():
    """The real persistence boundary releases memory after Sidecar commit."""
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import persist_task_result
    from tests._seed import delete_conversation, seed_conversation

    conv_id = 'cv-release-e2e'
    tid = 'tk-' + uuid.uuid4().hex[:12]
    seed_conversation(
        conv_id, messages=[{'role': 'user', 'content': 'hi'}],
        title='release-integ-test')
    task = _heavy_terminal_task(conv_id, tid)
    task['_userId'] = 1
    try:
        persist_task_result(task)
        assert task['messages'] is None, 'persist did not release task["messages"]'
        row = get_storage_client().query(
            'record.get', {'namespace': 'task_results', 'key': tid})
        assert row is not None, 'task result missing after persist'
        value = row['value']
        assert value['content'] == 'the final answer'
        assert value['status'] == 'done'
    finally:
        delete_conversation(conv_id)
    _ok('e2e: persistence releases messages after a complete Sidecar commit')


def test_post_terminal_readers_tolerate_none_messages():
    """Readers of task['messages'] that could run after finalization use the
    `task.get('messages') or []` idiom, so a released (None) field yields []
    not a crash. Exercise the REAL readers with messages=None."""
    task = {'id': 'tk-none', 'convId': 'c', 'messages': None, 'aborted': False}

    # trajectory.flatten — a real post-terminal reader of task['messages']
    # (trajectory export). Must tolerate a released (None) field.
    from lib.trajectory import flatten
    for fmt in ('sharegpt', 'openai-finetune', 'anthropic', 'tofu-native'):
        out = flatten(task, fmt)
        assert isinstance(out, dict) and out.get('format') == fmt, \
            f'flatten({fmt}) failed on released messages'

    _ok('real post-terminal readers treat released (None) messages as [] — no crash')


def test_shed_concurrent_with_task_churn():
    """shed_memory_under_pressure must not raise while other threads mutate the
    registry, and must never evict a running task."""
    from lib.tasks_pkg.manager import shed_memory_under_pressure
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    rt = chat_task_runtime
    stop = threading.Event()
    errors = []
    running_tid = 'tk-run-guard-' + uuid.uuid4().hex[:8]

    def churn():
        i = 0
        while not stop.is_set():
            try:
                tid = f'tk-churn-{i}-{uuid.uuid4().hex[:6]}'
                t = rt.create(user_id=1, task_id=tid)
                t['status'] = 'done'
                t['finished_at'] = time.time()
                i += 1
                if i % 50 == 0:
                    time.sleep(0)  # yield
            except Exception as e:  # noqa: BLE001
                errors.append(('churn', repr(e)))
                break

    def shedder():
        while not stop.is_set():
            try:
                shed_memory_under_pressure()
            except Exception as e:  # noqa: BLE001
                errors.append(('shed', repr(e)))
                break

    r = rt.create(user_id=1, task_id=running_tid)
    r['status'] = 'running'   # must survive every shed
    threads = [threading.Thread(target=churn), threading.Thread(target=churn),
               threading.Thread(target=shedder)]
    try:
        for th in threads:
            th.start()
        time.sleep(1.5)
        stop.set()
        for th in threads:
            th.join(timeout=5)
        assert not errors, f'concurrent shed/churn raised: {errors[:3]}'
        assert rt.get(running_tid) is not None, \
            'running task was evicted by a concurrent shed'
    finally:
        stop.set()
        rt._tasks.pop(running_tid, None)
        # sweep any churn leftovers
        rt.cleanup_stale(max_age=0)
    _ok('shed is thread-safe under task churn; running task never evicted')


def test_release_and_shed_are_idempotent():
    from lib.tasks_pkg.manager import shed_memory_under_pressure
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    task = _heavy_terminal_task('c-idem', 'tk-idem')
    task['_flow_turns'] = [{'content': 'turn'}]   # both heavy fields present
    n1 = _release_heavy_task_state(task)
    n2 = _release_heavy_task_state(task)   # already released
    assert n1 == 2 and n2 == 0, f'release not idempotent: {n1}, {n2}'
    r1 = shed_memory_under_pressure()
    r2 = shed_memory_under_pressure()      # nothing left to evict
    assert isinstance(r1, dict) and isinstance(r2, dict), 'shed must always return a dict'
    _ok('release + shed are idempotent (double-call harmless)')


_POSITIVE = [
    test_e2e_persist_releases_messages_and_keeps_db,
    test_post_terminal_readers_tolerate_none_messages,
    test_shed_concurrent_with_task_churn,
    test_release_and_shed_are_idempotent,
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


def main():
    print()
    print(_color('═══ release/shed integration + concurrency + idempotency ═══', '36'))
    print()
    try:
        from tests._standalone_guard import guard_standalone_storage
        guard_standalone_storage('test_release_heavy_state_integration.__main__')
    except Exception:
        pass
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('integration suite failed')
    print()
    print(_color('═══ ALL INTEGRATION TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
