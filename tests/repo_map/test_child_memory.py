"""Memory evidence cannot miss a short-lived allocation or invent a joined child."""
import os
import subprocess
import sys

import pytest
from benchmarks.repository_index.child_memory import observe_children, child_receipts

pytestmark = [pytest.mark.unit, pytest.mark.skipif(sys.platform != 'linux', reason='Linux wait4 evidence')]


@pytest.mark.parametrize('wait', ['wait', 'poll', 'communicate'])
def test_short_lived_child_peak_and_exit_are_observed(wait):
    original = subprocess.Popen
    with observe_children() as children:
        # Bash allocates and frees the buffer before its parent can sample it.
        with subprocess.Popen(['/bin/bash', '-c', "printf -v block '%33554432s' ''; unset block; exit 7"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            if wait == 'wait': child.wait(timeout=10)
            elif wait == 'communicate': child.communicate(timeout=10)
            else:
                import time
                deadline = time.monotonic() + 10
                while child.poll() is None:
                    assert time.monotonic() < deadline
                    time.sleep(.01)
    rows = child_receipts(children)
    assert subprocess.Popen is original
    assert len(rows) == 1 and rows[0]['pid'] == child.pid and rows[0]['returncode'] == 7
    assert rows[0]['peak_rss_bytes'] >= 32 * 1024 * 1024
    with pytest.raises(ChildProcessError): os.waitpid(child.pid, os.WNOHANG)


def test_missing_child_usage_is_refused_and_hook_restored_on_error():
    original = subprocess.Popen
    with pytest.raises(LookupError):
        with observe_children(): raise LookupError('caller failed')
    assert subprocess.Popen is original
    with pytest.raises(AssertionError, match='joined exit'):
        child_receipts([{'pid':123}])


def test_evidence_write_failure_preserves_child_exit_and_refuses_capture(monkeypatch):
    from benchmarks.repository_index import child_memory
    primary = OSError('injected journal write failure')
    def failed(*_args):
        raise primary
    monkeypatch.setattr(child_memory.RecordJournal, 'append', failed)
    original = subprocess.Popen
    with pytest.raises(OSError) as caught:
        with observe_children():
            with subprocess.Popen(['/bin/sh', '-c', 'exit 7']) as child:
                assert child.wait(timeout=10) == 7
    assert caught.value is primary and child.returncode == 7
    assert subprocess.Popen is original
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)


def test_in_progress_capture_is_not_materialized_and_missing_exit_is_refused():
    with observe_children() as capture:
        with subprocess.Popen(['/bin/sh', '-c', 'exit 0']) as child:
            child.wait(timeout=10)
        with pytest.raises(RuntimeError, match='not complete'):
            child_receipts(capture)
    assert len(child_receipts(capture)) == 1
    child = None
    try:
        with pytest.raises(AssertionError, match='joined exit'):
            with observe_children():
                child = subprocess.Popen(['/bin/sleep', '10'])
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=10)
