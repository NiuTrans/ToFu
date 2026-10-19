"""A transient cleanup failure remains owned/visible until group quiescence."""
import threading
import time

import pytest

from lib.project_mod.process_group import ProcessGroupRetirementError
from lib.tasks_pkg import background_commands as background

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['cleanup-error', 'no-leader-authority'])
def test_failed_retirement_retains_watcher_and_pending_receipt(tmp_path, monkeypatch, failure):
    ready, release = threading.Event(), threading.Event()
    captured = []
    original_thread = threading.Thread
    def thread(*args, **kwargs):
        result = original_thread(*args, **kwargs)
        if str(kwargs.get('name') or '').startswith('bg-cmd-reattach-'):
            captured.append(result)
        return result
    def retire(_record):
        ready.set()
        if failure == 'no-leader-authority':
            return False
        raise ProcessGroupRetirementError('Inherited group descriptor is not yet quiescent')
    monkeypatch.setattr(background.threading, 'Thread', thread)
    monkeypatch.setattr(background, '_kill_record_tree', retire)
    monkeypatch.setattr(background, '_record_pid_alive', lambda _record: True)
    monkeypatch.setattr(background, '_read_exit_code', lambda _paths: None)
    monkeypatch.setattr(background, '_result_from_record', lambda *a, **k: 'settled after observed quiescence')
    monkeypatch.setattr(background, '_deliver_record', lambda *a: True)
    monkeypatch.setattr(background, '_remove_record_dir', lambda *a: None)
    monkeypatch.setattr(background, 'REATTACH_POLL_S', 0.01)
    monkeypatch.setattr('lib.project_mod.process_group.group_has_live_members', lambda _pgid: not release.is_set())
    command_id = 'bg_retirement-pending-owned'
    record = {'command_id': command_id, 'command': 'owned command', 'pid': 12345, 'pgid': 12345,
              'pid_starttime': 'owned-start', 'task_id': 'own-task', 'user_id': None, 'conv_id': ''}
    paths = {'dir': str(tmp_path)}
    try:
        background._reattach(record, paths)
        with background._lock:
            entry = background._registry[command_id]
            entry['task']['aborted'] = True
        assert ready.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with background._lock:
                error = entry['state']['error']
            if error:
                break
            time.sleep(0.01)
        assert error and 'not yet confirmed' in error
        assert not entry['done'].is_set()
        assert not entry['state']['finished']
        assert captured and captured[0].is_alive()
        release.set()
        assert entry['done'].wait(2)
        captured[0].join(2)
        assert not captured[0].is_alive()
        assert entry['state']['result'] == 'settled after observed quiescence'
    finally:
        release.set()
        for worker in captured:
            worker.join(3)
        with background._lock:
            background._registry.pop(command_id, None)


@pytest.mark.parametrize('group', [None, 0, 1, True, 'invalid'])
def test_unverified_group_never_allocates_recovery_watcher(group, monkeypatch):
    from unittest.mock import Mock
    factory = Mock(side_effect=AssertionError('Unsafe group allocated a watcher'))
    monkeypatch.setattr(background.threading, 'Thread', factory)
    with pytest.raises(ValueError, match='unverified process-group record'):
        background._reattach({'command_id': 'bg_invalid-owned', 'pgid': group}, {})
    factory.assert_not_called()


@pytest.mark.parametrize('name', ['python3', 'worker name', 'worker name)'])
def test_shared_birth_token_parser_preserves_comm_boundaries(name, monkeypatch):
    from types import SimpleNamespace
    from lib.project_mod import process_group
    fields = [b'S', b'1', b'123', b'123'] + [b'0'] * 15 + [b'456789', b'4096']
    raw = f'123 ({name}) '.encode() + b' '.join(fields)
    monkeypatch.setattr(process_group, 'Path', lambda _path: SimpleNamespace(read_bytes=lambda: raw))
    assert process_group.read_process_fields(123)[2] == b'123'
    assert background._pid_starttime(123) == '456789'
