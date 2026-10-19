"""Preparation reports live work and owns compiler descendants through failure."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = pytest.mark.unit


def test_output_is_forwarded_before_child_exit(tmp_path, monkeypatch):
    acknowledgement = tmp_path / 'progress-seen'
    chunks = []

    class Output:
        def write(self, text):
            chunks.append(text)
            if 'compiling-marker' in ''.join(chunks):
                acknowledgement.touch()

        def flush(self):
            pass

    monkeypatch.setattr(sys, 'stderr', Output())
    program = (
        'import time; from pathlib import Path; '
        'print("compiling-marker", flush=True); '
        f'p = Path({str(acknowledgement)!r})\n'
        'while not p.exists(): time.sleep(0.01)\n'
        'print("build complete", flush=True)')
    result = engines._run([sys.executable, '-c', program], cwd=tmp_path,
                          environment=dict(os.environ), timeout=3,
                          progress='Compiling storage engine')
    assert 'build complete' in result
    assert 'build complete' in ''.join(chunks)


def test_silent_work_reports_elapsed_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(engines, 'PROGRESS_INTERVAL_SECONDS', 0.05)
    engines._run([sys.executable, '-c', 'import time; time.sleep(0.2)'],
                 cwd=tmp_path, environment=dict(os.environ), timeout=3,
                 progress='Checking storage engine')
    assert 'Checking storage engine' in capsys.readouterr().err


def test_timeout_keeps_last_diagnostic(tmp_path):
    program = 'import time; print("waiting-for-linker", flush=True); time.sleep(60)'
    with pytest.raises(engines.EnginePreparationError, match='timed out.*') as error:
        engines._run([sys.executable, '-c', program], cwd=tmp_path,
                     environment=dict(os.environ), timeout=0.3)
    assert 'waiting-for-linker' in str(error.value)


def test_successful_exit_cannot_bypass_log_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, 'MAX_LOG_BYTES', 128)
    with pytest.raises(engines.EnginePreparationError, match='log budget'):
        engines._run([sys.executable, '-c', 'print("x" * 1024)'], cwd=tmp_path,
                     environment=dict(os.environ), timeout=3)


def test_identity_output_stays_capture_only(tmp_path, capsys):
    result = engines._run([sys.executable, '-c', 'print("machine-readable")'],
                          cwd=tmp_path, environment=dict(os.environ), timeout=3)
    assert result == 'machine-readable'
    assert capsys.readouterr().err == ''


def test_broken_progress_sink_does_not_break_preparation(tmp_path, monkeypatch):
    class ClosedOutput:
        def write(self, _):
            raise BrokenPipeError('consumer closed')

    monkeypatch.setattr(sys, 'stderr', ClosedOutput())
    result = engines._run([sys.executable, '-c', 'print("complete")'],
                          cwd=tmp_path, environment=dict(os.environ), timeout=3,
                          progress='Compiling storage engine')
    assert result == 'complete'


def test_expired_preparation_does_not_spawn_more_work(tmp_path, monkeypatch):
    token = engines._deadline.set(time.monotonic() - 1)
    monkeypatch.setattr(engines.subprocess, 'Popen', lambda *a, **k: pytest.fail('deadline already expired'))
    try:
        with pytest.raises(engines.EnginePreparationError, match='timed out'):
            engines._run(['unused'], cwd=tmp_path, environment={}, timeout=3)
    finally:
        engines._deadline.reset(token)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file lock')
def test_contended_lock_reports_wait_and_honors_shared_deadline(tmp_path, capsys):
    path = tmp_path / 'prepare.lock'
    with engines._prepare_lock(path):
        token = engines._deadline.set(time.monotonic() + 0.1)
        try:
            with pytest.raises(engines.EnginePreparationError, match='Another storage engine update'):
                with engines._prepare_lock(path):
                    pytest.fail('must not acquire the competing lock')
        finally:
            engines._deadline.reset(token)
    assert 'Waiting for another storage engine preparation' in capsys.readouterr().err


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal cancellation')
@pytest.mark.parametrize('cancel_signal, expected_status', [(signal.SIGTERM, 1), (signal.SIGINT, 130)])
def test_worker_cancellation_reaps_compiler_and_returns_without_traceback(tmp_path, cancel_signal, expected_status):
    pid_file = tmp_path / 'compiler.pid'
    compiler = (
        'import os,signal,time; from pathlib import Path; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)')
    worker = (
        'import os,sys; from pathlib import Path; '
        'from serverctl_pkg import native_engine as e; '
        f'e.prepare = lambda _: e._run([sys.executable, "-c", {compiler!r}], '
        f'cwd=Path({str(tmp_path)!r}), environment=dict(os.environ), timeout=30, termination_grace=0.05); '
        f'sys.argv = ["prepare", "--project", {str(tmp_path)!r}]; sys.exit(e.main())')
    process = subprocess.Popen([sys.executable, '-c', worker],
                               cwd=Path(__file__).resolve().parents[2],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        process.send_signal(cancel_signal)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == expected_status
        assert b'cancelled' in stderr and b'Traceback' not in stderr
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass  # The worker has already reaped its compiler.


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group lifecycle')
def test_timeout_kills_descendant_even_when_group_leader_exits(tmp_path):
    pid_file = tmp_path / 'stubborn.pid'
    descendant = (
        'import os,signal,time; from pathlib import Path; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)')
    parent = (
        'import subprocess,sys,time; '
        f'subprocess.Popen([sys.executable, "-c", {descendant!r}]); time.sleep(60)')
    try:
        with pytest.raises(engines.EnginePreparationError):
            engines._run([sys.executable, '-c', parent], cwd=tmp_path,
                         environment=dict(os.environ), timeout=0.5)
        pid = int(pid_file.read_text())
        stat = Path(f'/proc/{pid}/stat')
        deadline = time.monotonic() + 2
        while stat.exists() and stat.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
            time.sleep(0.01)  # Allow the kernel to complete SIGKILL delivery.
        if stat.exists():
            assert stat.read_text().split()[2] == 'Z'
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass  # Already reaped by the operating system.


def test_structured_progress_crosses_child_boundary_without_raw_logs(tmp_path):
    frames = []
    previous_phase = engines._progress_phase.get()
    program = ('from serverctl_pkg.native_engine import _report_stage; '
               '_report_stage("compiling", "private compiler log")')
    with engines.observe_preparation(frames.append):
        engines._run([sys.executable, '-c', program], cwd=Path.cwd(),
                     environment=dict(os.environ), timeout=5,
                     progress='Preparing storage engine')
    assert frames == [{'phase': 'compiling'}]
    assert 'private' not in str(frames)
    assert engines._progress_observer.get() is None
    assert engines._progress_phase.get() == previous_phase


def test_progress_parser_accepts_split_frames_and_ignores_unstructured_output():
    frames = []
    with engines.observe_preparation(frames.append):
        reader = engines._progress_observer.get()
        reader('compiler details\nTOFU_ENGINE_PRO')
        reader('GRESS {"phase": "validating"}\n')
        reader('TOFU_ENGINE_PROGRESS malformed\n')
    assert frames == [{'phase': 'validating'}]


@pytest.mark.parametrize('reported, reason', [
    ('b' * 64 + ':1', 'source identity differs'),
    ('a' * 64 + ':0', 'allocator is incompatible'),
    ('invalid', 'invalid build metadata'),
])
def test_candidate_attestation_explains_metadata_refusal(tmp_path, monkeypatch, reported, reason):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    monkeypatch.setattr(engines.sys, 'platform', 'linux')
    monkeypatch.setattr(engines, 'build_identity', lambda _: {'source_sha256': 'a' * 64})
    monkeypatch.setattr(engines, '_run', lambda *_a, **_kw: reported)
    with pytest.raises(engines.EnginePreparationError, match=reason):
        engines._validate_candidate(candidate, tmp_path, {})


def test_candidate_attestation_preserves_loader_failure(tmp_path, monkeypatch):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    monkeypatch.setattr(engines, 'build_identity', lambda _: {'source_sha256': 'a' * 64})

    def load(*_args, **_kwargs):
        raise engines.EnginePreparationError('loader failed: cannot allocate memory in static TLS block')

    monkeypatch.setattr(engines, '_run', load)
    with pytest.raises(engines.EnginePreparationError, match='cannot allocate memory in static TLS block'):
        engines._validate_candidate(candidate, tmp_path, {})
    assert not engines._cached_library_matches_source(candidate, 'a' * 64)


def test_candidate_attestation_runs_semantic_probe_only_after_matching_pair(tmp_path, monkeypatch):
    candidate = tmp_path / 'tofu-db'
    candidate.with_name('libtofu_db.so').write_bytes(b'candidate')
    monkeypatch.setattr(engines.sys, 'platform', 'linux')
    monkeypatch.setattr(engines, 'build_identity', lambda _: {'source_sha256': 'a' * 64})
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return 'a' * 64 + ':1' if len(calls) == 1 else ''

    monkeypatch.setattr(engines, '_run', run)
    engines._validate_candidate(candidate, tmp_path, {})
    assert len(calls) == 2
    assert calls[1][1:] == [str(tmp_path / 'scripts/check_tofudb_engine_drift.py'), '--binary', str(candidate), '--json']
