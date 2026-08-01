"""tests/test_finalize_persist_before_autopilot.py — terminal persist must
not be hostage to the VU sub-task's liveness.

Measured incident (2026-07-31, task 752273db, epic pt_5f0262fc): the task
finished at 20:38:27 — message committed fr=stop (pre-emit sync),
status='done' in memory — but its ``task_results`` row stayed 'running'
for 2h57m. ``_finalize_and_emit_done`` called ``persist_task_result``
AFTER ``maybe_run_autopilot``, and the VU sub-task runs INLINE inside
that hook (``_run_single_turn`` on the finalize thread): it sat in a
``run_command`` crawling a FUSE parent dir, so the persist (and the queue
drain riding it) never ran until the zombie was aborted by hand. The
baton's own comment already assumed the correct order
("persist_task_result runs _dispatch_queued_message before our hook
fires") — the code disagreed.

The fix:
  1. ``persist_task_result(task, _defer_heavy_release=True)`` runs BEFORE
     the hook — terminal row, conv sync, queue drain, proactive status
     all land before the VU can hang.
  2. The heavy-state release is deferred past the hook (the VU inherits
     ``task['messages']``) and runs right after ``append_event(done_evt)``.

Pinned here: the source ORDER (with an in-test NEUTER proving the ratchet
is keyed on it), the defer parameter's existence, and the release-split
behaviour against the REAL ``persist_task_result``.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_finalize_persist_before_autopilot.py -v
"""

from __future__ import annotations

import os
import threading

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
FINALIZE_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')
PERSIST_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'manager', '_persist.py')

_PERSIST_CALL = 'persist_task_result(task, _defer_heavy_release=True)'
_HOOK_CALL = 'maybe_run_autopilot(task)'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _persist_precedes_hook(src: str) -> bool:
    return src.index(_PERSIST_CALL) < src.index(_HOOK_CALL)


# ═════════════════════════════════════════════════════════════════════
#  1. Source-order contract
# ═════════════════════════════════════════════════════════════════════

def test_persist_runs_before_autopilot_hook():
    src = _read(FINALIZE_PATH)
    assert _persist_precedes_hook(src), (
        f'{FINALIZE_PATH}: persist_task_result must run BEFORE '
        f'maybe_run_autopilot — the VU sub-task runs inline inside the hook '
        f'and can hang indefinitely, so the parent\'s terminal row (and the '
        f'queue drain riding persist) must land first (task 752273db: row '
        f'stuck at running 2h57m).'
    )


def test_heavy_release_deferred_past_hook():
    """The VU inherits task['messages'] — the heavy-state release must happen
    AFTER the hook (and after the done event), not inside the early persist."""
    src = _read(FINALIZE_PATH)
    hook_pos = src.index(_HOOK_CALL)
    release_pos = src.index('_release_heavy_task_state(task)')
    done_pos = src.index('append_event(task, done_evt)')
    assert release_pos > hook_pos, (
        'heavy-state release must not run before the autopilot hook — the VU '
        'reads task[\'messages\'] (run_virtual_user: parent_messages)')
    assert release_pos > done_pos, (
        'the release should sit at the old trailing-persist site, right after '
        'append_event(done_evt), preserving the pre-fix ordering for every '
        'post-done consumer')


def test_defer_param_declared():
    src = _read(PERSIST_PATH)
    assert '_defer_heavy_release' in src, (
        'persist_task_result must accept _defer_heavy_release — the early '
        'call in _finalize_and_emit_done depends on it')


def test_neuter_order_swap_breaks_ratchet():
    """NEUTER: restore the OLD order (hook before persist) on a string copy —
    the ratchet above must flip red on it, proving it is keyed on the real
    ordering and would catch a regression."""
    src = _read(FINALIZE_PATH)
    assert _persist_precedes_hook(src), 'precondition: fixed order missing'
    persist_block = src[src.index(_PERSIST_CALL):]
    persist_line = persist_block.split('\n', 1)[0]
    # Build the neutered variant: delete the early persist call and append a
    # trailing one after the hook (the pre-fix shape).
    neutered = src.replace(_PERSIST_CALL + '\n', '', 1)
    hook_pos = neutered.index(_HOOK_CALL)
    hook_line_end = neutered.index('\n', hook_pos) + 1
    neutered = (neutered[:hook_line_end]
                + '    persist_task_result(task)  # NEUTER: old trailing persist\n'
                + neutered[hook_line_end:])
    assert not _persist_precedes_hook(neutered.replace(
        'persist_task_result(task)  # NEUTER: old trailing persist',
        _PERSIST_CALL)), (
        'NEUTER applied but the ratchet still passes on the old order — '
        'the source-order test is not actually keyed on the ordering')


# ═════════════════════════════════════════════════════════════════════
#  2. Behavioural: the release split against the REAL persist_task_result
# ═════════════════════════════════════════════════════════════════════

def _mk_task(task_id):
    return {
        'id': task_id,
        'convId': '',
        'status': 'done',
        'aborted': False,
        'content': 'answer',
        'thinking': '',
        'error': None,
        'finishReason': 'stop',
        'model': 'm',
        'provider_id': 'p',
        'usage': {},
        'apiRounds': [],
        'toolRounds': [],
        'config': {},
        'messages': [{'role': 'user', 'content': 'q'}],
        'events': [],
        'events_lock': threading.Lock(),
        'created_at': 0.0,
    }


@pytest.fixture()
def persist_side_effects_off(monkeypatch):
    """Neutralise persist_task_result's fan-out (sync/queue/proactive/summary)
    so the behavioural assertions isolate the release split. The row upsert
    still runs (real DB, cleaned up per test)."""
    import lib.tasks_pkg.manager._sync as _sync
    for name in ('_sync_result_to_conversation', '_update_proactive_execution_status',
                 '_dispatch_queued_message', '_maybe_refresh_project_summary'):
        monkeypatch.setattr(_sync, name, lambda *a, **k: None, raising=False)


def _cleanup_row(task_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
        db.commit()
    except Exception:
        pass


def test_defer_release_keeps_messages(persist_side_effects_off):
    from lib.tasks_pkg.manager import persist_task_result
    tid = 'pt5f-defer-%d' % os.getpid()
    task = _mk_task(tid)
    try:
        persist_task_result(task, _defer_heavy_release=True)
        assert task['messages'] is not None, (
            '_defer_heavy_release must NOT null task[\'messages\'] — the VU '
            'sub-task inherits it after the early persist returns')
    finally:
        _cleanup_row(tid)


def test_default_call_still_releases(persist_side_effects_off):
    """COMPLEMENT: without the defer flag the release still fires — the RSS
    contract of every other persist caller is unchanged."""
    from lib.tasks_pkg.manager import persist_task_result
    tid = 'pt5f-nodefer-%d' % os.getpid()
    task = _mk_task(tid)
    try:
        persist_task_result(task)
        assert task['messages'] is None, (
            'the default path must still release heavy terminal state '
            '(RSS bounding contract)')
    finally:
        _cleanup_row(tid)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
