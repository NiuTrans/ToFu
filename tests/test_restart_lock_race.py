"""tests/test_restart_lock_race.py — guards for restart_15000.sh's instance-lock race.

Measured incident (pt_0c1d75f7eb824467, 2026-07-31 18:10): the old server's
graceful shutdown (~285 threads) keeps the single-instance flock on
``data/.server.lock`` alive ~10s AFTER the port frees. The script's [2/5]
waited only for the PORT, so [3/5] relaunched into the still-held lock:

    18:10:15  launched pid 3243972
    18:10:19  [CRITICAL] [Lock] instance lock held by a LIVE local server
              (pid=3459968) — new instance EXITED; the script did not retry.

Two fixes, both guarded here:
  [2b/5] wait for the old PROCESS to exit (zombie counts as exited — flock
         dies with the process) + probe the instance flock itself;
  [3/5]  a launch that still dies on the lock signature gets a BOUNDED retry
         (3 attempts, 10s cooldown) instead of an immediate exit-4.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'restart_15000.sh')


def _script() -> str:
    with open(SCRIPT, encoding='utf-8') as f:
        return f.read()


# ═══════════════════════════════════════════════════════
#  1. The load-bearing mechanism: flock(1) sees Python's fcntl.flock
# ═══════════════════════════════════════════════════════

def test_flock_cli_sees_python_fcntl_flock(tmp_path):
    """The [2b/5] probe (`flock -n <file> -c true`) is only meaningful if the
    flock CLI contends with the server's ``fcntl.flock(LOCK_EX|LOCK_NB)`` —
    both must live in the same lock namespace. Functional proof."""
    lock = tmp_path / '.server.lock'
    lock.write_text('placeholder')

    holder = subprocess.Popen(
        [sys.executable, '-c',
         'import fcntl, os, time, sys\n'
         f'fd = os.open({str(lock)!r}, os.O_RDWR)\n'
         'fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
         'print("held", flush=True)\n'
         'time.sleep(30)\n'],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == 'held', \
            'python holder failed to take the flock'

        busy = subprocess.run(['flock', '-n', str(lock), '-c', 'true'],
                              capture_output=True)
        assert busy.returncode != 0, \
            'flock(1) reported FREE while python fcntl.flock held the file — ' \
            'the [2b/5] probe would be blind to the real lock'
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    free = subprocess.run(['flock', '-n', str(lock), '-c', 'true'],
                          capture_output=True)
    assert free.returncode == 0, \
        'flock(1) still reports BUSY after the holder exited — the probe ' \
        'would block every relaunch forever'


# ═══════════════════════════════════════════════════════
#  2. [2b/5]: wait for the old process, not just the port
# ═══════════════════════════════════════════════════════

def test_script_waits_for_old_process_exit_before_relaunch():
    src = _script()
    assert '[2b/5]' in src, \
        'no [2b/5] step — the script still relaunches on port-free alone, ' \
        'which is the exact race that killed pid 3243972 on the instance lock'
    step2b = src.index('[2b/5]')
    step3 = src.index('echo "[3/5]')
    step2 = src.index('echo "[2/5]')
    assert step2 < step2b < step3, \
        '[2b/5] must run AFTER the port-free wait and BEFORE the relaunch'
    # It must check the killed pids' PROCESS STATE (not the port), allow
    # zombies (flock dies with the process), and stay bounded.
    assert re.search(r'ps -o stat= -p', src), \
        '[2b/5] does not inspect process state'
    assert re.search(r'Z\*\)', src), \
        'a zombie must count as exited — its flock is already released'
    assert 'SIGKILL' in src, \
        'no SIGKILL fallback when the old process outlives the wait'


def test_script_probes_instance_flock_before_relaunch():
    """The EXACT precondition: data/.server.lock must be flock-acquirable."""
    src = _script()
    assert '.server.lock' in src, \
        'the script never probes the instance lock file itself'
    # The probe may name the file via a variable — assert BOTH the binding
    # and the non-blocking probe of it (a bare `flock -n <file>` literal is
    # not required; the indirection is).
    assert re.search(r'ILOCK=.*data/\.server\.lock', src), \
        'no ILOCK binding to data/.server.lock'
    assert re.search(r'flock -n "\$\{ILOCK\}"', src), \
        'no non-blocking flock probe of the instance lock — a still-held ' \
        'lock is only discovered by the new instance dying on it'


# ═══════════════════════════════════════════════════════
#  3. [3/5]-[4/5]: bounded retry on the lock signature
# ═══════════════════════════════════════════════════════

def test_script_retries_lock_conflict_with_bound():
    src = _script()
    assert re.search(r'for attempt in 1 2 3', src), \
        'no bounded retry loop around the relaunch — a lock-conflict death ' \
        'is still a single-shot exit'
    assert 'instance lock held by a LIVE local server' in src, \
        'the retry does not recognise the lock-conflict signature in the log'
    assert re.search(r'sleep 10', src), \
        'no cooldown between retry attempts — a hot retry loop would race ' \
        'the old shutdown repeatedly'
    # The fast-fail path for NON-lock deaths must survive.
    assert 'exit 4' in src, \
        'the fail-fast exit for a non-lock startup death is gone'


def test_script_still_fails_fast_on_non_lock_death():
    """A launch that dies for any OTHER reason must exit 4 immediately —
    the retry is for the lock race only, not a universal mask."""
    src = _script()
    # The lock-death check and the exit-4 must coexist inside the retry loop:
    # retry ONLY when the signature matched.
    retry_region = src[src.index('for attempt in 1 2 3'):]
    assert 'instance lock held by a LIVE local server' in retry_region
    assert 'exit 4' in retry_region


def test_script_syntax_is_valid_bash():
    proc = subprocess.run(['bash', '-n', SCRIPT], capture_output=True, text=True)
    assert proc.returncode == 0, f'bash -n rejected the script: {proc.stderr}'


def test_help_and_argument_gate_precede_all_host_specific_restart_work():
    source = _script()
    gate_at = source.index('case "$#:${1:-}" in')
    project_at = source.index('PROJ=')
    lifecycle_at = source.index('[lifecycle-gate]')

    assert gate_at < project_at < lifecycle_at
    assert '1:-h|1:--help) _restart_usage; exit 0' in source
    assert 'unsupported arguments: $*' in source
    assert 'python serverctl.py restart' in source
