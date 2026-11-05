"""A stuck procfs sensor cannot stop command control or publish late input."""
import builtins
import contextlib
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(params=['child-lists', 'parent-scan'])
def discovery(request, monkeypatch):
    """Run once through the kernel's child lists and once through the full scan."""
    from lib.compat import HAS_PROCFS
    from lib.project_mod import process_group
    if not HAS_PROCFS:
        pytest.skip('Linux procfs boundary')
    if request.param == 'child-lists':
        if not os.path.exists(f'/proc/self/task/{threading.get_native_id()}/children'):
            pytest.skip('kernel publishes no per-thread child lists')
    else:
        monkeypatch.setattr(process_group, '_kernel_lists_children', lambda: False)
    return request.param


_READER = ("import os, sys\n"
           "print(os.getpid(), flush=True)\n"
           "sys.stdin.readline()\n")
# The branch forks the reader from a worker thread, so only that thread's child
# list names it; the sibling only sleeps. The root reaps whichever child exits.
_THREADED_BRANCH = ('import subprocess, sys, threading; '
                    't = threading.Thread(target=subprocess.run, '
                    'args=([sys.executable, "-c", sys.argv[1]],)); t.start(); t.join()')
_ROOT = ("import os, subprocess, sys\n"
         "sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         "branch = subprocess.Popen([sys.executable, '-c', *sys.argv[1:]])\n"
         "print(sibling.pid, branch.pid, flush=True)\n"
         "while True:\n"
         "    try:\n"
         "        os.wait()\n"
         "    except ChildProcessError:\n"
         "        break\n")


@contextlib.contextmanager
def _stdin_reader_tree():
    """root -> (sibling, branch -[thread]-> reader blocked reading root's stdin)."""
    root = subprocess.Popen([sys.executable, '-c', _ROOT, _THREADED_BRANCH, _READER],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            start_new_session=True)
    try:
        lines = [root.stdout.readline().split() for _ in range(2)]
        sibling, branch = map(int, next(line for line in lines if len(line) == 2))
        reader = int(next(line for line in lines if len(line) == 1)[0])
        deadline = time.monotonic() + 10
        while True:
            with open(f'/proc/{reader}/syscall') as handle:
                if handle.read().split()[:2] == ['0', '0x0']:
                    break
            assert time.monotonic() < deadline, 'reader never blocked in read(0)'
            time.sleep(0.01)
        yield root, sibling, branch, reader, os.fstat(root.stdin.fileno()).st_ino
    finally:
        os.killpg(root.pid, signal.SIGKILL)
        root.wait()
        root.stdin.close()
        root.stdout.close()


class _ProcTouches:
    """Record every path this thread opens, lists or stats while active.

    Audit hooks see open/scandir/listdir however they are called; stat has no
    audit event, so it is wrapped. ``on_touch`` may act before the access.
    """
    _active = {}
    _hooked = False

    def __init__(self, monkeypatch, on_touch=None):
        self.paths = []
        self._on_touch = on_touch
        self._busy = False
        if not _ProcTouches._hooked:
            sys.addaudithook(_ProcTouches._audit)
            _ProcTouches._hooked = True
        real_stat = os.stat
        def stat(path, *args, **kwargs):
            self._touch(path)
            return real_stat(path, *args, **kwargs)
        monkeypatch.setattr(os, 'stat', stat)

    @staticmethod
    def _audit(event, args):
        recorder = _ProcTouches._active.get(threading.get_ident())
        if recorder is not None and event in ('open', 'os.scandir', 'os.listdir') and args:
            recorder._touch(args[0])

    def _touch(self, path):
        if self._busy or _ProcTouches._active.get(threading.get_ident()) is not self:
            return
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        path = os.fsdecode(path)
        self.paths.append(path)
        if self._on_touch is not None:
            self._busy = True
            try:
                self._on_touch(path)
            finally:
                self._busy = False

    def __enter__(self):
        _ProcTouches._active[threading.get_ident()] = self
        return self

    def __exit__(self, *args):
        _ProcTouches._active.pop(threading.get_ident(), None)

    def proc_pids(self):
        """PIDs named by /proc/<pid> paths; None marks a listing of /proc itself."""
        pids = set()
        for path in self.paths:
            if path.rstrip('/') == '/proc':
                pids.add(None)
            elif match := re.match(r'/proc/(\d+)(/|$)', path):
                pids.add(int(match.group(1)))
        return pids


@pytest.mark.parametrize('reason', ['stop', 'timeout', 'interrupt', 'finish'])
def test_blocked_syscall_read_does_not_block_command_control(tmp_path, monkeypatch, reason):
    from lib.project_mod import run_command as rc
    from lib.compat import HAS_PROCFS
    if not HAS_PROCFS:
        pytest.skip('Linux procfs boundary')
    from lib.project_mod import stdin_probe
    pool = stdin_probe.ProbePool()
    monkeypatch.setattr(stdin_probe, '_default_pool', lambda: pool)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    task = {'aborted': False}
    result = {}
    class BlockedSyscall:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            entered.set()
            assert release.wait(15), 'test must release its own sensor'
            return '0 0x0 0x0 0x0 0x0 0x0 0x0'
    def proc_open(path, *args, **kwargs):
        if str(path).startswith('/proc/') and str(path).endswith('/syscall'):
            return BlockedSyscall()
        return builtins.open(path, *args, **kwargs)
    monkeypatch.setattr(rc, 'open', proc_open, raising=False)
    callbacks = []
    chunks = []
    def run():
        try:
            result['text'] = rc.tool_run_command(
                str(tmp_path),
                'printf LIVE1; sleep 0.7; printf LIVE2' if reason == 'finish' else 'sleep 30',
                timeout=1 if reason == 'timeout' else None,
                task=task, stdin_callback=lambda hint: callbacks.append(hint),
                on_chunk=lambda stream, text: chunks.append((stream, text)))
        finally:
            finished.set()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        pid = task['_subprocess_pid']
        if reason == 'stop':
            task['aborted'] = True
        elif reason == 'interrupt':
            task['_cmd_interrupt'] = {'pid': pid, 'source': 'user', 'ts': time.time(), 'note': ''}
        assert finished.wait(3), 'procfs read prevented command control from returning'
        assert not release.is_set(), 'control must finish while the sensor is still blocked'
        assert not callbacks
        assert '_subprocess_pid' not in task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        expected = {'stop': 'Command aborted', 'timeout': 'timed out',
                    'interrupt': 'Command interrupted', 'finish': '[exit code: 0]'}[reason]
        assert expected.lower() in result['text'].lower()
        if reason == 'finish':
            live_output = ''.join(text for stream, text in chunks if stream == 'stdout')
            assert live_output == 'LIVE1LIVE2'
    finally:
        release.set()
        worker.join(7)
        assert not worker.is_alive()
        # The sensor owns no task callback; after release its late answer
        # must remain fenced even though it says that stdin was being read.
        deadline = time.monotonic() + 2
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 0, 'test must retire its physical sensor'
        assert not callbacks


def test_real_interactive_input_still_reaches_owned_child(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    from lib.compat import HAS_PROCFS
    if not HAS_PROCFS:
        pytest.skip('Linux procfs boundary')
    replies = []
    def answer(hint):
        replies.append(hint)
        return 'VALUE7'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote("print('READ=' + input(), flush=True)")
    text = tool_run_command(str(tmp_path), command, timeout=5, stdin_callback=answer)
    assert 'READ=VALUE7' in text
    assert len(replies) == 1


def test_blocked_sensor_never_refuses_later_sensors_and_fences_late_result():
    from lib.project_mod.stdin_probe import ProbePool, StdinProbe
    pool = ProbePool()
    entered, release = threading.Event(), threading.Event()
    observed = []
    def blocked(pid, inode, *, stop_event):
        entered.set()
        assert release.wait(5)
        return (pid, 'late-input')
    def idle(pid, inode, *, stop_event):
        observed.append(pid)
        return None
    first = StdinProbe(blocked, 1, 2, pool=pool)
    later = [StdinProbe(idle, 3 + index, 4, pool=pool) for index in range(8)]
    try:
        assert first.poll() is None and entered.wait(2)
        first.close()
        for probe in later:
            assert probe.poll() is None
        deadline = time.monotonic() + 2
        while len(set(observed)) < 8 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert set(observed) == set(range(3, 11))
        for probe in later:
            probe.close()
        deadline = time.monotonic() + 2
        while pool.active > 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 1
        release.set()
        deadline = time.monotonic() + 2
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 0
        assert first.poll() is None
        assert all(probe.poll() is None for probe in later)
    finally:
        release.set()
        first.close()
        for probe in later: probe.close()
        deadline = time.monotonic() + 2
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 0


def test_noninteractive_stdin_reader_still_receives_eof(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    calls = []
    out = tool_run_command(str(tmp_path), 'sort', timeout=5,
                           stdin_callback=lambda hint: calls.append(hint))
    assert '[exit code: 0]' in out
    assert not calls


@pytest.mark.parametrize('boundary', ['construction', 'launch'])
def test_optional_sensor_failure_preserves_command_output_and_cleanup(tmp_path, monkeypatch, boundary):
    from lib.project_mod import stdin_probe
    from lib.project_mod.run_command import tool_run_command
    def unavailable(*args, **kwargs):
        raise RuntimeError('owned sensor admission unavailable')
    if boundary == 'construction':
        monkeypatch.setattr(stdin_probe.StdinProbe, '__init__', unavailable)
    else:
        monkeypatch.setattr(stdin_probe.ProbePool, 'launch', unavailable)
    task = {'aborted': False}
    out = tool_run_command(str(tmp_path), 'echo retained-output && sleep 0.3', timeout=3,
                           task=task, stdin_callback=lambda hint: None)
    assert 'retained-output' in out and '[exit code: 0]' in out
    assert '_subprocess_pid' not in task


def test_descendant_scan_finds_grandchildren_whose_names_contain_parentheses(discovery):
    from lib.project_mod.run_command import _collect_descendants
    grandchild = ("import os, pathlib, time\n"
                  "pathlib.Path('/proc/self/comm').write_text('odd) name (x')\n"
                  "print(os.getpid(), flush=True)\n"
                  "time.sleep(60)\n")
    # Each generation runs the next code argument: parent -> child -> grandchild.
    level = 'import subprocess, sys; subprocess.run([sys.executable, "-c", *sys.argv[1:]])'
    parent = subprocess.Popen([sys.executable, '-c', level, level, grandchild],
                              stdout=subprocess.PIPE, start_new_session=True)
    try:
        grandchild_pid = int(parent.stdout.readline())
        with open(f'/proc/{grandchild_pid}/comm') as comm:
            assert comm.read().strip() == 'odd) name (x'
        tree = _collect_descendants(parent.pid)
        assert tree[0] == parent.pid
        assert grandchild_pid in tree
        assert len(tree) == len(set(tree)) == 3
        assert os.getpid() not in tree
    finally:
        os.killpg(parent.pid, signal.SIGKILL)
        parent.wait()
        parent.stdout.close()


def test_probe_tick_reads_only_the_command_subtree(discovery, monkeypatch):
    from lib.project_mod.run_command import _is_any_child_reading_stdin
    with _stdin_reader_tree() as (root, sibling, branch, reader, inode):
        subtree = {root.pid, sibling, branch, reader}
        assert _is_any_child_reading_stdin(root.pid, inode)[0] == reader
        with _ProcTouches(monkeypatch) as touches:
            found = _is_any_child_reading_stdin(root.pid, inode)
        assert found[0] == reader
        if discovery == 'child-lists':
            assert touches.proc_pids() == subtree
            assert all(path.startswith('/proc/') for path in touches.paths)
        else:
            # Control: the recorder sees the full scan that child lists avoid.
            assert None in touches.proc_pids()
            assert touches.proc_pids() - {None} > subtree


@pytest.mark.parametrize('commands', [1, 3])
def test_probe_tick_reads_per_command_with_n_commands(discovery, monkeypatch, commands):
    """/proc reads of one tick per command while N commands run concurrently.

    With child lists a tick reads only its own command's subtree: one listing
    per member's thread directory, one read of every member thread's child
    list, each path once, so N commands cost the sum of their own subtrees and
    nothing grows with the host or the other commands. The parent-scan control
    lists /proc for every command on every tick.
    """
    from lib.project_mod.run_command import _is_any_child_reading_stdin
    with contextlib.ExitStack() as stack:
        trees = [stack.enter_context(_stdin_reader_tree()) for _ in range(commands)]
        ticks = []
        for root, sibling, branch, reader, inode in trees:
            subtree = {root.pid, sibling, branch, reader}
            # The first tick also runs the once-per-process child-list probe.
            assert _is_any_child_reading_stdin(root.pid, inode)[0] == reader
            with _ProcTouches(monkeypatch) as touches:
                found = _is_any_child_reading_stdin(root.pid, inode)
            assert found[0] == reader
            threads = {f'/proc/{pid}/task/{tid}/children'
                       for pid in subtree for tid in os.listdir(f'/proc/{pid}/task')}
            ticks.append((touches, subtree, threads))
        others = set().union(*(subtree for _, subtree, _ in ticks))
        for touches, subtree, threads in ticks:
            if discovery == 'child-lists':
                assert touches.proc_pids() == subtree
                assert not touches.proc_pids() & (others - subtree)
                assert len(touches.paths) == len(set(touches.paths))
                assert {path for path in touches.paths if path.endswith('/children')} == threads
                assert {path for path in touches.paths if path.endswith('/task')} == {
                    f'/proc/{pid}/task' for pid in subtree}
            else:
                assert sum(path.rstrip('/') == '/proc' for path in touches.paths) == 1


def test_descendant_discovery_matches_the_full_scan(monkeypatch):
    from lib.compat import HAS_PROCFS
    from lib.project_mod import process_group
    from lib.project_mod.run_command import _collect_descendants
    if not HAS_PROCFS:
        pytest.skip('Linux procfs boundary')
    with _stdin_reader_tree() as (root, sibling, branch, reader, _inode):
        walked = _collect_descendants(root.pid)
        monkeypatch.setattr(process_group, '_kernel_lists_children', lambda: False)
        scanned = _collect_descendants(root.pid)
    assert walked[0] == scanned[0] == root.pid
    assert len(walked) == len(set(walked))
    assert set(walked) == set(scanned) == {root.pid, sibling, branch, reader}


def test_probe_detects_grandchild_reader_when_a_child_exits_mid_scan(discovery, monkeypatch):
    from lib.project_mod.run_command import _is_any_child_reading_stdin
    with _stdin_reader_tree() as (root, sibling, branch, reader, inode):
        exited = []
        def exit_sibling_on_first_visit(path):
            if exited or not path.startswith(f'/proc/{sibling}/'):
                return
            os.kill(sibling, signal.SIGKILL)
            deadline = time.monotonic() + 10
            while os.path.exists(f'/proc/{sibling}'):  # the root reaps it
                assert time.monotonic() < deadline, 'root never reaped the sibling'
                time.sleep(0.005)
            exited.append(path)
        with _ProcTouches(monkeypatch, exit_sibling_on_first_visit) as touches:
            found = _is_any_child_reading_stdin(root.pid, inode)
        assert exited, 'the probe never visited the exiting child'
        assert found[0] == reader
        assert reader in touches.proc_pids()
