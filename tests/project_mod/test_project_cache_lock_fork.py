"""Forked journal work must not inherit another thread's cache-lock ownership."""
import os
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not hasattr(os, 'fork'), reason='POSIX fork boundary'),
]


def test_child_journal_work_resets_cache_lock_without_waiting_for_parent(tmp_path):
    program = r'''
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback

from lib import project_mod
from lib.project_mod import config, modifications as mods
from lib.project_mod.config import _lock as imported_lock

root = Path(sys.argv[1])
project = str(root / 'project')
Path(project).mkdir()
config.SESSIONS_DIR = mods.SESSIONS_DIR = str(root / 'sessions')
config._state['path'] = project
config._roots['fork-project'] = {'path': project}
config._state['sessionId'] = mods._get_session_dir(project)
assert mods._record_modification(
    project, 'write_file', 'parent.txt', post_image={'kind': 'absent'})
assert [row['path'] for row in mods.get_modifications(project)] == ['parent.txt']
inherited_state = dict(config._state)
inherited_roots = dict(config._roots)
assert imported_lock is config._lock is mods._lock is project_mod._lock

acquired, release = threading.Event(), threading.Event()
owner_errors = []
def owner():
    try:
        # Preserve recursion depth as well as ownership in the parent.
        with imported_lock:
            with imported_lock:
                acquired.set()
                assert release.wait(12), 'fork waited for the parent cache lock'
    except BaseException:
        owner_errors.append(traceback.format_exc())

thread = threading.Thread(target=owner)
thread.start()
pid = None
try:
    assert acquired.wait(3)
    assert not imported_lock.acquire(blocking=False)
    pid = os.fork()
    if pid == 0:
        try:
            # Existing imports must still reference the same, now usable lock.
            assert imported_lock is config._lock is mods._lock is project_mod._lock
            assert config._state == inherited_state
            assert config._roots == inherited_roots
            assert mods._record_modification(
                project, 'write_file', 'child.txt', post_image={'kind': 'absent'})
            assert [row['path'] for row in mods.get_modifications(project)] == [
                'parent.txt', 'child.txt']
            with imported_lock:
                with mods._lock:
                    assert config._state['path'] == project
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)

    deadline = time.monotonic() + 5
    while True:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            pid = None
            break
        assert time.monotonic() < deadline, 'child blocked on inherited cache lock'
        time.sleep(0.01)
    assert os.waitstatus_to_exitcode(status) == 0, status
    # Child completion cannot require releasing or replacing the parent's lock.
    assert not release.is_set()
    assert thread.is_alive()
    assert not imported_lock.acquire(blocking=False)
finally:
    if pid is not None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    release.set()
    thread.join(3)

assert not thread.is_alive()
assert not owner_errors, owner_errors
assert imported_lock.acquire(blocking=False)
imported_lock.release()
# The fork reset does not turn a process cache into authority: the parent must
# invalidate its pre-fork snapshot and reload the child's committed journal.
assert [row['path'] for row in mods.get_modifications(project)] == [
    'parent.txt', 'child.txt']
print('child journal committed; parent cache lock ownership preserved')
'''
    result = subprocess.run(
        [sys.executable, '-c', program, str(tmp_path)],
        env={**os.environ, 'TOFU_DATA_DIR': str(tmp_path / 'runtime')},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'child journal committed; parent cache lock ownership preserved' in result.stdout
