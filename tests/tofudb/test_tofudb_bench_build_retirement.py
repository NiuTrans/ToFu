"""An interrupted bench engine build retires its whole toolchain group.

The bench harness cargo-builds tofu-db when no binary is supplied. cargo forks
rustc, cc, collect2 and the linker; whether the build stops at its own deadline
or because pytest-timeout raises into the wait, no descendant may keep running
once the resolution call has returned.
"""
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from lib.project_mod.process_group import group_has_live_members

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts import tofudb_bench  # noqa: E402

pytestmark = pytest.mark.unit


class _Interrupted(BaseException):
    """Stands in for the Failed outcome pytest-timeout raises from SIGALRM."""


def _slow_builder(tmp_path: Path) -> tuple[Path, Path]:
    """A cargo stand-in that forks a TERM-ignoring compiler and never finishes."""
    state = tmp_path / 'build-state'
    state.mkdir()
    builder = tmp_path / 'cargo'
    builder.write_text(
        '#!/bin/sh\n'
        f'echo $$ > "{state}/builder.pid"\n'
        "(trap '' TERM; exec sleep 600) &\n"
        f'echo $! > "{state}/compiler.pid.new"\n'
        f'mv "{state}/compiler.pid.new" "{state}/compiler.pid"\n'
        'wait\n')
    builder.chmod(0o755)
    return builder, state


def _running(pid: int) -> bool:
    try:
        stat = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        return False
    return stat.rsplit(b')', 1)[1].split()[0] not in (b'Z', b'X')


def _kill_leaked_compiler(state: Path) -> None:
    """Reap a compiler a broken build left behind, never a reused PID."""
    try:
        pid = int((state / 'compiler.pid').read_text())
        if Path(f'/proc/{pid}/cmdline').read_bytes() == b'sleep\x00600\x00':
            os.kill(pid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass  # No compiler was started, or it has already exited.


def _resolve_until_interrupted(state: Path) -> None:
    """Raise into the build's wait the way pytest-timeout's signal method does."""
    main = threading.get_ident()
    finished = threading.Event()

    def interrupt() -> None:
        deadline = time.monotonic() + 60
        while not (state / 'compiler.pid').exists() and time.monotonic() < deadline:
            if finished.wait(0.01):
                return
        time.sleep(0.2)  # Let the build re-enter its blocking wait.
        if not finished.is_set():
            signal.pthread_kill(main, signal.SIGALRM)

    def raise_interrupted(signum, frame):
        raise _Interrupted()

    previous = signal.signal(signal.SIGALRM, raise_interrupted)
    interrupter = threading.Thread(target=interrupt, daemon=True)
    try:
        interrupter.start()
        with pytest.raises(_Interrupted):
            tofudb_bench._resolve_tofudb_binary()  # noqa: SLF001
    finally:
        finished.set()
        interrupter.join()
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize('stop', ['deadline', 'interrupted'])
def test_stopped_bench_engine_build_leaves_no_toolchain_descendants(tmp_path, monkeypatch, stop):
    builder, state = _slow_builder(tmp_path)
    monkeypatch.delenv('TOFUDB_BENCH_BIN', raising=False)
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    monkeypatch.setattr(tofudb_bench, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(tofudb_bench, '_CARGO_CANDIDATES', (builder,))
    try:
        if stop == 'deadline':
            monkeypatch.setattr(tofudb_bench, '_CARGO_BUILD_TIMEOUT_SECONDS', 2)
            with pytest.raises(RuntimeError, match='no tofu-db binary available'):
                tofudb_bench._resolve_tofudb_binary()  # noqa: SLF001
        else:
            _resolve_until_interrupted(state)
        assert (state / 'compiler.pid').exists(), 'the build stopped before its compiler started'
        compiler = int((state / 'compiler.pid').read_text())
        assert not _running(compiler), 'the compiler outlived the stopped build'
        assert not group_has_live_members(int((state / 'builder.pid').read_text()))
    finally:
        _kill_leaked_compiler(state)
