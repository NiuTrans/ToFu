"""The event-append congestion benchmark measures the authority it claims to.

``benchmarks/event_append_congestion.py`` is valid evidence only while its
in-process authority charges queue wait against each request deadline (a
request still queued at its deadline is refused, never served late), counts a
row carried by two concurrently pending requests as a duplicate send, and its
worker commits every lane's exact rows through the production append path.
The current tree must commit that log without a single concurrent duplicate:
a row is never re-sent while its batch is in flight, whatever the scheduling.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / 'benchmarks' / 'event_append_congestion.py'


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location('event_append_congestion_measure', BENCHMARK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deadline_ms(seconds):
    return int((time.time() + seconds) * 1000)


def test_queue_wait_is_charged_and_a_concurrent_resend_is_a_duplicate(benchmark):
    authority = benchmark.FifoAuthority(
        transaction_seconds=0.3, row_seconds=0.0, foreign_seconds=0.3)
    row = {'task_id': 'measured', 'sequence': 1, 'event': {'type': 'delta'}}
    holder_entered = threading.Event()
    outcomes = {}

    def hold():
        holder_entered.set()
        outcomes['holder'] = authority.attempt(
            'event.append', row, timeout=10, command_id=None,
            deadline_unix_ms=_deadline_ms(10))

    holder = threading.Thread(target=hold)
    holder.start()
    assert holder_entered.wait(5)
    # The holder's ticket enters the FIFO in the same step that counts it.
    registered_by = time.monotonic() + 5
    while not authority.attempts['event.append'] and time.monotonic() < registered_by:
        time.sleep(0.001)
    with pytest.raises(StorageError) as refused:
        authority.attempt('event.append', row, timeout=0.05, command_id=None,
                          deadline_unix_ms=_deadline_ms(0.05))
    holder.join(10)

    assert refused.value.code == 'database_timeout'
    assert outcomes['holder'] == {'inserted': True, 'task_id': 'measured', 'sequence': 1}
    assert authority.refusals['event.append'] == 1
    assert authority.concurrent_duplicate_sends == 1
    assert authority.row_sends == 2
    assert list(authority.committed) == [('measured', 1)]


def test_worker_commits_the_exact_log_without_concurrent_duplicates(tmp_path):
    command = [sys.executable, str(BENCHMARK), '--worker', '--source-root', str(ROOT),
               '--lanes', '2', '--events', '3', '--foreign-writers', '1',
               '--foreign-ms', '50', '--gil-threads', '1', '--warmup-commands', '0',
               '--budget-s', '120']
    environment = dict(os.environ, PYTHONPATH=str(ROOT),
                       TOFU_DATA_DIR=str(tmp_path / 'data'))
    completed = subprocess.run(command, cwd=tmp_path, env=environment,
                               capture_output=True, text=True, timeout=300, check=False)
    assert completed.returncode == 0, completed.stderr[-4000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result['event_batcher'] == str(ROOT / 'lib' / 'storage' / 'event_batcher.py')
    assert result['complete'] is True and result['failures'] == []
    assert result['committed'] == result['rows'] == 6
    assert result['concurrent_duplicate_sends'] == 0
    assert result['row_sends'] >= result['rows']
