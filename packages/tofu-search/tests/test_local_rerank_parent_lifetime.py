"""The actual scoring entry point cannot outlive its launching process.

A separate subreaper owns deliberately orphaned children; pytest never changes
its process-wide adoption policy. A retained pipe writer prevents accidental EOF
from masking a missing parent-death guard. No model or network is needed.
"""

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tofu_search.local_rerank import LocalReranker

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux CPU worker"),
]


def _live(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in (
            "Z", "X",
        )
    except FileNotFoundError:
        return False


def _wait(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _waiting_for_input(pid):
    try:
        return "pipe_read" in Path(f"/proc/{pid}/wchan").read_text()
    except FileNotFoundError:
        return False
def _waiting_for_input(pid):
    """Whether the worker is blocked on its stdin pipe.

    ``wchan`` names the kernel wait channel only when the running kernel
    exports symbols. Hardened and older builds (and some container kernels)
    report ``0`` instead, which is not evidence of anything: reading it as
    "not waiting" made this probe fail on hosts where the worker was in fact
    blocked. Only trust ``wchan`` when it carries a symbol; otherwise fall back
    to the scheduler state, since an unrequested worker has nothing to run and
    therefore sleeps.
    """
    try:
        channel = Path(f"/proc/{pid}/wchan").read_text().strip()
    except FileNotFoundError:
        return False
    if channel and channel != "0":
        return "pipe_read" in channel
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state == "S"


def _probe_parent_death(directory):
    library = ctypes.CDLL(None, use_errno=True)
    assert library.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    trace = directory / "child.pid"
    reader, writer = os.pipe()
    launcher_code = """
import os, subprocess, sys, time
from pathlib import Path
from tofu_search.local_rerank import LocalReranker
ranker = LocalReranker(sys.argv[2], threads=1, max_parallel=1, max_rss_mb=4096)
child = subprocess.Popen(ranker._command(), stdin=int(sys.argv[1]),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
os.close(int(sys.argv[1]))
Path(sys.argv[2], 'child.pid').write_text(str(child.pid))
time.sleep(30)
"""
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_code, str(reader), str(directory)],
        pass_fds=(reader,), start_new_session=True,
    )
    os.close(reader)
    child_pid = None
    try:
        assert _wait(trace.exists), "launcher did not publish its child"
        child_pid = int(trace.read_text())
        assert _wait(lambda: _waiting_for_input(child_pid)), "worker did not await input"
        launcher.kill()
        launcher.wait(timeout=5)
        retired = _wait(lambda: not _live(child_pid), seconds=1)
    finally:
        if launcher.poll() is None:
            launcher.kill()
        launcher.wait(timeout=5)
        if child_pid is None and trace.exists():
            child_pid = int(trace.read_text())
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            assert _wait(lambda: not _live(child_pid)), "probe cleanup failed"
            os.waitpid(child_pid, 0)
        os.close(writer)
    return {"retired_with_parent": retired, "probe_cleanup_joined": True}


def test_actual_worker_dies_with_parent_before_reading_request(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, __file__, "--parent-death-probe", str(tmp_path)],
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["probe_cleanup_joined"]
    assert receipt["retired_with_parent"], "worker remained live after its parent died"


@pytest.mark.parametrize("parent", [None, "invalid", "mismatched"])
def test_unbound_worker_exits_without_waiting_for_request(tmp_path, parent):
    with LocalReranker(tmp_path) as ranker:
        command = ranker._command()[:3]
    if parent is not None:
        command.append(str(os.getppid()) if parent == "mismatched" else parent)
    worker = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert _wait(lambda: worker.poll() is not None, seconds=1), "unbound worker read input"
        assert worker.returncode != 0
        assert json.loads(worker.stdout.read())["kind"] == "parent_lifetime_unavailable"
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)


def test_bound_worker_keeps_reading_while_parent_is_alive(tmp_path):
    with LocalReranker(tmp_path) as ranker:
        worker = subprocess.Popen(
            ranker._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
    try:
        assert _wait(lambda: _waiting_for_input(worker.pid))
        assert worker.poll() is None
        output, _ = worker.communicate(b"{}", timeout=5)
        assert worker.returncode == 1
        assert json.loads(output)["kind"] == "inference_failed"
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)


if __name__ == "__main__":
    assert sys.argv[1] == "--parent-death-probe"
    print(json.dumps(_probe_parent_death(Path(sys.argv[2]))))
