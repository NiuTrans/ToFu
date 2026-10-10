"""Wide directory trees remain complete without a thread per directory."""
import threading
import time

import pytest

from lib.project_mod import tree_index
from lib.observability import InstrumentedThreadPoolExecutor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('capacity', [2, 8])
def test_wide_tree_batches_directory_work_and_preserves_every_file(monkeypatch, capacity):
    width = 4096
    active = peak_active = peak_threads = 0
    lock = threading.Lock()
    monkeypatch.setattr(tree_index, '_compile_gitignore', lambda root: ([], []))
    monkeypatch.setattr(tree_index, '_walk_jobs', lambda: capacity)
    monkeypatch.setattr(tree_index, '_max_entries', lambda: 10_000)

    def scan(abs_dir, rel_dir, depth, out, contexts):
        nonlocal active, peak_active, peak_threads
        if not rel_dir:
            return [(f'/root/{n}', str(n), 1, contexts) for n in range(width)]
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            peak_threads = max(peak_threads, len(pool._threads))
        try:
            time.sleep(0.001)
            out.append((f'{rel_dir}/file.txt', 1, 0))
            return []
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(tree_index, '_scan_one_dir', scan)
    pool = InstrumentedThreadPoolExecutor(max_workers=capacity,
        metric_pool='tree-index-scan', thread_name_prefix='pressure-test-scan')
    try:
        rows, complete = tree_index._walk_tree('/root', time.time() + 30,
                                               scan_executor=pool)
        assert complete
        assert {row[0] for row in rows} == {f'{n}/file.txt' for n in range(width)}
        assert peak_active <= capacity
        assert peak_active > 1
        assert peak_threads <= capacity * 2  # Completed-Future callbacks may still occupy a worker.
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_expired_build_never_submits_the_remaining_frontier(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(tree_index, '_compile_gitignore', lambda root: ([], []))
    pool = Mock()
    rows, complete = tree_index._walk_tree('/root', time.time() - 1, scan_executor=pool)
    assert not complete and rows == []
    pool.submit.assert_not_called()


def test_frontier_overflow_refuses_a_partial_index(monkeypatch):
    monkeypatch.setattr(tree_index, '_compile_gitignore', lambda root: ([], []))
    monkeypatch.setattr(tree_index, '_max_entries', lambda: 10)
    monkeypatch.setattr(tree_index, '_scan_one_dir', lambda *args: [
        (f'/root/{n}', str(n), 1, ()) for n in range(11)])
    rows, complete = tree_index._walk_tree('/root', time.time() + 5)
    assert not complete and rows == []
