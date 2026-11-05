"""Identical concurrent memory listings share scans without serving stale state.

Callers with the same complete listing request share at most two corpus scans:
the one already running and one queued behind it. A caller never receives a
scan that started before it arrived, so every write completed before its call
is visible, and each caller of a shared scan owns independent rows.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_WAIT_SECONDS = 10.0


@pytest.fixture()
def coalescing_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    projects = []
    for index in range(2):
        project = tmp_path / f'project-{index}'
        (project / '.tofu' / 'memories').mkdir(parents=True)
        projects.append(project)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._migrated_roots.update(str(project) for project in projects)
    storage_dirs._server_store_migrated = True
    memory_files._metadata_cache.clear()
    yield projects
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _write_memory(directory: Path, memory_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{memory_id}.md'
    path.write_text(
        '---\n'
        f'name: {memory_id}\n'
        f'description: coalescing fixture for {memory_id}\n'
        'tags: [alpha, beta]\n'
        '---\n\n'
        f'body for {memory_id}\n',
        encoding='utf-8',
    )
    return path


class _SlowProjectScans:
    """Count project-store scans; the first one snapshots, then blocks."""

    def __init__(self, monkeypatch, project: Path) -> None:
        import lib.memory.storage._files as memory_files

        self.directory = str(project / '.tofu' / 'memories')
        self.scans = 0
        self.first_snapshot_taken = threading.Event()
        self.release_first = threading.Event()
        self._original = memory_files._sorted_visible_dir_entries
        self._lock = threading.Lock()
        monkeypatch.setattr(
            memory_files, '_sorted_visible_dir_entries', self._entries)

    def _entries(self, dirpath):
        entries = self._original(dirpath)
        if os.path.normpath(str(dirpath)) != os.path.normpath(self.directory):
            return entries
        with self._lock:
            self.scans += 1
            first = self.scans == 1
        if first:
            self.first_snapshot_taken.set()
            assert self.release_first.wait(_WAIT_SECONDS)
        return entries


def _start(function, *args, **kwargs) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def run():
        try:
            outcome['value'] = function(*args, **kwargs)
        except BaseException as error:  # noqa: BLE001 - asserted by tests
            outcome['error'] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _wait_for_queued_callers(count: int) -> None:
    import lib.memory.storage._crud as crud

    deadline = time.monotonic() + _WAIT_SECONDS
    while crud._memory_listing_reads.snapshot()['queuedCallers'] != count:
        if time.monotonic() > deadline:
            raise AssertionError(f'{count} callers never queued')
        time.sleep(0.001)


def _eligible(project: Path) -> list[dict]:
    from lib.memory.storage import get_eligible_memories

    return get_eligible_memories(
        str(project), include_body=False, record_view='retrieval')


def _join(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(_WAIT_SECONDS)
        assert not thread.is_alive()


def test_identical_concurrent_listings_share_at_most_two_scans(
        coalescing_store, monkeypatch):
    project = coalescing_store[0]
    for index in range(5):
        _write_memory(project / '.tofu' / 'memories', f'note-{index}')
    scans = _SlowProjectScans(monkeypatch, project)

    leader, leader_outcome = _start(_eligible, project)
    assert scans.first_snapshot_taken.wait(_WAIT_SECONDS)
    followers = [_start(_eligible, project) for _ in range(7)]
    _wait_for_queued_callers(7)
    scans.release_first.set()
    _join(leader, *(thread for thread, _outcome in followers))

    expected = [f'note-{index}' for index in range(5)]
    assert [row['id'] for row in leader_outcome['value']] == expected
    for _thread, outcome in followers:
        assert [row['id'] for row in outcome['value']] == expected
    assert scans.scans == 2


def test_caller_arriving_after_a_write_sees_it(coalescing_store, monkeypatch):
    from lib.memory.storage import create_memory

    project = coalescing_store[0]
    _write_memory(project / '.tofu' / 'memories', 'existing-note')
    scans = _SlowProjectScans(monkeypatch, project)

    early, early_outcome = _start(_eligible, project)
    assert scans.first_snapshot_taken.wait(_WAIT_SECONDS)
    written = create_memory(
        name='Written While Scanning',
        description='a write completed before the late caller arrived',
        body='durable body',
        scope='project',
        project_path=str(project),
    )
    late, late_outcome = _start(_eligible, project)
    _wait_for_queued_callers(1)
    scans.release_first.set()
    _join(early, late)

    # The running scan predates the write; the late caller must not share it.
    assert [row['id'] for row in early_outcome['value']] == ['existing-note']
    assert sorted(row['id'] for row in late_outcome['value']) == sorted(
        ['existing-note', written['id']])


def test_callers_sharing_a_scan_receive_independent_rows(
        coalescing_store, monkeypatch):
    from lib.memory.storage import list_all_memories

    project = coalescing_store[0]
    _write_memory(project / '.tofu' / 'memories', 'shared-note')
    scans = _SlowProjectScans(monkeypatch, project)

    leader, _leader_outcome = _start(
        list_all_memories, str(project), include_body=False)
    assert scans.first_snapshot_taken.wait(_WAIT_SECONDS)
    sharers = [
        _start(list_all_memories, str(project), include_body=False)
        for _ in range(3)
    ]
    _wait_for_queued_callers(3)
    scans.release_first.set()
    _join(leader, *(thread for thread, _outcome in sharers))

    first, second, third = (outcome['value'] for _thread, outcome in sharers)
    assert scans.scans == 2
    first[0]['tags'].append('mutated')
    first[0].pop('filepath')
    second.clear()
    assert third[0]['tags'] == ['alpha', 'beta']
    assert third[0]['filepath'].endswith('shared-note.md')
    assert [row['id'] for row in third] == ['shared-note']


def test_listings_with_different_inputs_never_share_or_wait(
        coalescing_store, monkeypatch):
    from lib.memory.storage import list_all_memories

    project, extra = coalescing_store
    _write_memory(project / '.tofu' / 'memories', 'primary-note')
    _write_memory(extra / '.tofu' / 'memories', 'extra-note')
    scans = _SlowProjectScans(monkeypatch, project)

    blocked, blocked_outcome = _start(_eligible, project)
    assert scans.first_snapshot_taken.wait(_WAIT_SECONDS)

    # A different view and a different root set are different requests: they
    # run their own scans while the retrieval listing is still in flight.
    complete = list_all_memories(str(project), include_body=True)
    union = list_all_memories(
        str(project), extra_paths=[str(extra)], include_body=False)
    assert blocked.is_alive()
    scans.release_first.set()
    _join(blocked)

    assert [row['id'] for row in blocked_outcome['value']] == ['primary-note']
    assert set(blocked_outcome['value'][0]) == {
        'id', 'name', 'description', 'enabled', 'tags', 'scope', 'filepath',
        'is_package', 'package_dir', 'eligible'}
    assert complete[0]['body'] == 'body for primary-note'
    assert [row['id'] for row in union] == ['primary-note', 'extra-note']
    assert scans.scans == 3
