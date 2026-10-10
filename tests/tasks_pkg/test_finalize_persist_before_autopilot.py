"""Terminal release waits for authoritative settlement and post-done consumers.

Turn settlement precedes executor diagnostics; behavioral root ordering lives
in test_chat_task_terminal and test_swarm_turn_handoff. These tests cover the
explicit heavy-state handoff needed by commit capture after durable done.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit


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
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state

    task = _mk_task('terminal-defer')
    assert persist_task_result(task, _defer_heavy_release=True) is True
    assert task['messages'] == [{'role': 'user', 'content': 'q'}]
    _release_heavy_task_state(task)
    assert task['messages'] is None


def test_default_call_still_releases(persist_side_effects_off):
    from lib.tasks_pkg.manager import persist_task_result

    task = _mk_task('terminal-default-release')
    assert persist_task_result(task) is True
    assert task['messages'] is None
