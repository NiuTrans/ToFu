"""Bounded lifecycle contracts for durable swarm output directories."""

from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

from lib.swarm.integration import _logs
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec


pytestmark = pytest.mark.unit


def test_master_constructor_does_not_create_empty_output_directory(tmp_path):
    output_dir = tmp_path / 'not-yet-valuable'

    MasterOrchestrator(
        task_id='lazy-output',
        conv_id='conversation',
        specs=[SubTaskSpec(id='worker', objective='work')],
        user_id=1,
        output_dir=str(output_dir),
    )

    assert not output_dir.exists()


def test_prune_removes_only_empty_immediate_directories(tmp_path):
    empty_a = tmp_path / 'empty-a'
    empty_b = tmp_path / 'empty-b'
    valuable = tmp_path / 'valuable'
    nested = tmp_path / 'nested'
    empty_a.mkdir()
    empty_b.mkdir()
    valuable.mkdir()
    (valuable / 'agent.log').write_text('durable result', encoding='utf-8')
    nested.mkdir()
    (nested / 'child').mkdir()
    symlink = tmp_path / 'linked'
    try:
        symlink.symlink_to(empty_a, target_is_directory=True)
    except OSError:
        symlink = None

    stats = _logs._prune_empty_output_dirs(
        str(tmp_path), entry_limit=32)

    assert stats['removed'] == 2
    assert not empty_a.exists()
    assert not empty_b.exists()
    assert (valuable / 'agent.log').read_text(encoding='utf-8') == 'durable result'
    assert (nested / 'child').is_dir()
    if symlink is not None:
        assert os.path.lexists(symlink)


def test_prune_bounds_work_by_entries_inspected(tmp_path):
    for index in range(6):
        (tmp_path / f'empty-{index}').mkdir()

    stats = _logs._prune_empty_output_dirs(
        str(tmp_path), entry_limit=2)

    assert stats == {
        'scanned': 2,
        'removed': 2,
        'errors': 0,
        'cancelled': False,
        'capped': True,
        'entryLimit': 2,
    }
    assert len(list(tmp_path.iterdir())) == 4


def test_background_cleanup_is_singleton_and_shutdown_cancellable(
    tmp_path,
    monkeypatch,
):
    entered = threading.Event()

    def blocked_prune(base_dir, *, entry_limit=None, cancel_event=None):
        entered.set()
        assert cancel_event is not None
        cancel_event.wait(timeout=2)
        return {
            'scanned': 0,
            'removed': 0,
            'errors': 0,
            'cancelled': cancel_event.is_set(),
            'capped': False,
            'entryLimit': entry_limit or 1,
        }

    monkeypatch.setattr(_logs, '_prune_empty_output_dirs', blocked_prune)
    assert _logs.start_swarm_output_cleanup(str(tmp_path))
    assert entered.wait(timeout=1)
    assert not _logs.start_swarm_output_cleanup(str(tmp_path))
    assert _logs.stop_swarm_output_cleanup(timeout=2)
    assert not _logs.swarm_output_cleanup_snapshot()['running']
