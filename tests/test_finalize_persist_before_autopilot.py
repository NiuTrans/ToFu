"""Terminal executor state settles before the Autopilot successor can block.

Autopilot inherits the parent task's input context. Finalization therefore
persists executor recovery state first, defers heavy-state release, emits the
terminal event, runs the successor decision, and releases the context last.
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


def test_heavy_release_deferred_past_hook_and_commit_capture():
    """The VU inherits task['messages'] — the heavy-state release must happen
    AFTER the hook/done and after commit captures its tool-round input."""
    src = _read(FINALIZE_PATH)
    hook_pos = src.index(_HOOK_CALL)
    release_pos = src.index('_release_heavy_task_state(task)')
    done_pos = src.index('append_event(task, done_evt)')
    commit_pos = src.rfind(
        '_spawn_async_commit_round(task', hook_pos, release_pos)
    assert release_pos > hook_pos, (
        'heavy-state release must not run before the autopilot hook — the VU '
        'reads task[\'messages\'] (run_virtual_user: parent_messages)')
    assert release_pos > done_pos, (
        'the release should sit at the old trailing-persist site, right after '
        'append_event(done_evt), preserving the pre-fix ordering for every '
        'post-done consumer')
    assert commit_pos > hook_pos, (
        'commit-round admission must snapshot opaque-writer evidence before '
        'terminal toolRounds are released')


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
        '_userId': 1,
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
    """Replace durable fan-out so the test isolates the release contract."""
    import lib.tasks_pkg.manager._persist as persist_module
    import lib.tasks_pkg.manager._sync as _sync
    monkeypatch.setattr(
        persist_module, '_upsert_task_row', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        persist_module, '_stamp_conv_provider_id', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _sync, '_update_proactive_execution_status', lambda *args, **kwargs: None)


def test_defer_release_keeps_messages(persist_side_effects_off):
    from lib.tasks_pkg.manager import persist_task_result
    tid = 'pt5f-defer-%d' % os.getpid()
    task = _mk_task(tid)
    persist_task_result(task, _defer_heavy_release=True)
    assert task['messages'] is not None, (
        '_defer_heavy_release must NOT null task[\'messages\'] — the VU '
        'sub-task inherits it after the early persist returns')


def test_default_call_still_releases(persist_side_effects_off):
    """COMPLEMENT: without the defer flag the release still fires — the RSS
    contract of every other persist caller is unchanged."""
    from lib.tasks_pkg.manager import persist_task_result
    tid = 'pt5f-nodefer-%d' % os.getpid()
    task = _mk_task(tid)
    persist_task_result(task)
    assert task['messages'] is None, (
        'the default path must still release heavy terminal state '
        '(RSS bounding contract)')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
