"""Several processes write the same rotating log files.

The API host and the resident execution host each install the application log
handlers on one ``logs/`` directory (docs/LOGGING.md, "Shared rotating
files"). Two handler instances on one path stand for the two processes: each
opens the file itself, exactly as separate processes do.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib.incident_journal import _PrivateRotatingFileHandler as IncidentSink
from lib.server.server_logging import (
    _PrivateRotatingFileHandler,
    _SizeAndTimeRotatingFileHandler,
)

pytestmark = pytest.mark.unit

FAMILIES = {
    'app': lambda path: _SizeAndTimeRotatingFileHandler(
        path, when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=200, total_budget_bytes=0),
    'error': lambda path: _PrivateRotatingFileHandler(
        path, maxBytes=200, backupCount=5, encoding='utf-8'),
    'incident': lambda path: IncidentSink(
        path, maxBytes=200, backupCount=5, encoding='utf-8', delay=True),
}


def _record(text, name='lib.shared_rotation_test'):
    return logging.LogRecord(name, logging.INFO, __file__, 1, text, (), None)


def _writers(path, family):
    handlers = [FAMILIES[family](str(path)) for _ in range(2)]
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(message)s'))
    return handlers


def _lines(path):
    return path.read_text(encoding='utf-8').splitlines()


def _rotated(directory, base):
    return sorted(path for path in directory.iterdir() if path != base)


@pytest.mark.parametrize('family', sorted(FAMILIES))
def test_record_after_a_peer_rollover_goes_to_the_current_file(tmp_path, family):
    base = tmp_path / 'shared.log'
    first, second = _writers(base, family)
    try:
        first.emit(_record('a' * 150))
        second.emit(_record('b' * 10))
        first.emit(_record('c' * 60))  # crosses the ceiling: first rolls the shared file over
        second.emit(_record('after-peer-rollover'))
    finally:
        first.close()
        second.close()
    assert [_lines(path) for path in _rotated(tmp_path, base)] == [['a' * 150, 'b' * 10]]
    assert _lines(base) == ['c' * 60, 'after-peer-rollover']


def test_a_peer_time_rollover_is_adopted_not_repeated(tmp_path):
    base = tmp_path / 'app.log'
    first, second = (_SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=1_000_000, total_budget_bytes=0) for _ in range(2))
    for handler in (first, second):
        handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        first.emit(_record('first-before'))
        second.emit(_record('second-before'))
        first.rolloverAt = second.rolloverAt = int(time.time()) - 1
        first.emit(_record('first-after'))
        second.emit(_record('second-after'))
    finally:
        first.close()
        second.close()
    assert [_lines(path) for path in _rotated(tmp_path, base)] == [['first-before', 'second-before']]
    assert _lines(base) == ['first-after', 'second-after']


_WRITER = '''
import logging, os, sys, time
from lib.server.server_logging import _SizeAndTimeRotatingFileHandler
path, writer, count, ready, go = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
handler = _SizeAndTimeRotatingFileHandler(
    path, when='midnight', backupCount=10_000, encoding='utf-8',
    max_bytes=4096, total_budget_bytes=0)
handler.setFormatter(logging.Formatter('%(message)s'))
failures = []
handler.handleError = failures.append
open(ready, 'w').close()
deadline = time.monotonic() + 60
while not os.path.exists(go):
    if time.monotonic() > deadline:
        sys.exit(4)
    time.sleep(0.001)
for seq in range(count):
    text = f'W{writer}-{seq:04d} ' + 'x' * (20 + (seq * 37) % 280)
    handler.emit(logging.LogRecord('lib.shared_rotation_test', logging.INFO, 'writer', 1, text, (), None))
handler.close()
sys.exit(3 if failures else 0)
'''


def test_concurrent_processes_keep_every_record_exactly_once(tmp_path):
    """Separately launched interpreters, like the API and execution hosts."""
    logs = tmp_path / 'logs'
    logs.mkdir()
    go = tmp_path / 'go'
    ready = [tmp_path / f'ready-{writer}' for writer in range(2)]
    workers = [subprocess.Popen(
        [sys.executable, '-c', _WRITER, str(logs / 'app.log'), str(writer), '400',
         str(ready[writer]), str(go)],
        cwd=Path(__file__).resolve().parents[2]) for writer in range(2)]
    try:
        deadline = time.monotonic() + 120
        while not all(path.exists() for path in ready) and time.monotonic() < deadline:
            if any(worker.poll() is not None for worker in workers):
                break
            time.sleep(0.01)
        go.touch()
        codes = [worker.wait(timeout=120) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
    assert codes == [0, 0]
    keys = []
    for path in logs.iterdir():
        data = path.read_bytes()
        assert b'\x00' not in data, path.name
        keys += [line.split(' ', 1)[0] for line in data.decode('utf-8').splitlines()]
    assert sorted(keys) == sorted(f'W{w}-{s:04d}' for w in range(2) for s in range(400))


def test_a_record_logged_by_the_rollover_itself_is_appended(tmp_path):
    base = tmp_path / 'app.log'
    old = tmp_path / 'app.log.2000-01-01'
    old.write_text('old\n' * 50, encoding='utf-8')
    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=200, total_budget_bytes=300)
    handler.setFormatter(logging.Formatter('%(message)s'))
    retention = logging.getLogger('lib.logging_pkg.log_retention')
    previous = retention.propagate, retention.level
    retention.addHandler(handler)
    retention.propagate = False
    retention.setLevel(logging.INFO)
    done = threading.Event()

    def emit_all():
        for text in ('a' * 150, 'b' * 10, 'c' * 60):
            handler.handle(_record(text))
        done.set()

    try:
        worker = threading.Thread(target=emit_all, daemon=True)
        worker.start()
        worker.join(30)
        assert done.is_set(), 'a rollover that logs must not wait on its own file lock'
    finally:
        retention.removeHandler(handler)
        retention.propagate, retention.level = previous
        handler.close()
    assert not old.exists()
    current = _lines(base)
    assert current[-1] == 'c' * 60
    assert any('Log retention removed' in line and str(old) in line for line in current)
