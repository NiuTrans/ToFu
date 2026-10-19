"""Search cleanup traverses only owned children and reaps on every exit path."""
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from lib.project_mod import grep_engine, process_group, read_tools

pytestmark = pytest.mark.unit


def test_descendant_walk_reads_all_owned_threads_without_global_scan(monkeypatch):
    visited = []
    tree = {'10': {'10': '20', '11': '30'}, '20': {'20': '40'},
            '30': {'30': ''}, '40': {'40': '10'}}

    class Entries:
        def __init__(self, names):
            self.names = names

        def __enter__(self):
            from types import SimpleNamespace
            return iter(SimpleNamespace(name=name) for name in self.names)

        def __exit__(self, *args):
            pass

    def scan(path):
        visited.append(path)
        parts = path.split('/')
        assert parts[-1] == 'task' and len(parts) == 4
        return Entries(tree[parts[2]])

    def read(path):
        parts = str(path).split('/')
        return tree[parts[2]][parts[4]]

    monkeypatch.setattr(process_group.os, 'scandir', scan)
    monkeypatch.setattr(Path, 'read_text', read)
    assert set(process_group.descendant_pids(10)) == {20, 30, 40}
    assert len(visited) == 4


@pytest.mark.parametrize('exit_kind', ['cancel', 'sink-error'])
def test_search_exit_reaps_child_and_closes_pipes(tmp_path, monkeypatch, exit_kind):
    launched = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        launched.append(child)
        return child

    monkeypatch.setattr(grep_engine.subprocess, 'Popen', launch)
    cancel = threading.Event()

    def sink(data):
        assert data == b'complete\n'
        if exit_kind == 'sink-error':
            raise RuntimeError('output sink failed')
        cancel.set()

    request = grep_engine.SearchRequest(
        cwd=str(tmp_path), preferred_backend='gnu', cancel_event=cancel,
        gnu_argv=(sys.executable, '-c',
                  'import time; print("complete",flush=True); time.sleep(30)'), timeout=5)
    if exit_kind == 'sink-error':
        with pytest.raises(RuntimeError, match='output sink failed'):
            grep_engine.run_search(request, stdout_sink=sink)
    else:
        result = grep_engine.run_search(request, stdout_sink=sink)
        assert result.cancelled and result.returncode == 130
        assert not result.timed_out
    assert len(launched) == 1
    child = launched[0]
    assert child.returncode is not None
    assert child.stdout.closed and child.stderr.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)


def test_early_page_cancels_and_joins_prefetched_chunks():
    import re
    from types import SimpleNamespace

    cancel = threading.Event()
    ready = threading.Event()
    retired = threading.Event()
    called = []

    def search(index, chunk):
        called.append(index)
        if index == 0:
            assert ready.wait(5)
            return index, b'a:1:hit\n', False, False, 0, SimpleNamespace(limit_reached=False)
        ready.set()
        assert cancel.wait(5)
        retired.set()
        return index, b'', False, False, 0, SimpleNamespace(limit_reached=False)

    results = [None] * 100
    actual = read_tools._run_pool(search, list(range(100)), 2, results,
        0, False, 1, re.compile('.'), cancel_event=cancel)
    assert actual[3] and actual[-1] == 0
    assert sorted(called) == [0, 1]
    assert retired.is_set()
    assert results[0] == b'a:1:hit\n' and results[1:] == [None] * 99
