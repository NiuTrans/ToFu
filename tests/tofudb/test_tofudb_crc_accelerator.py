"""Optional CRC acceleration cannot block import or publish partial libraries."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def test_import_never_launches_a_compiler(tmp_path):
    root = str(Path(__file__).resolve().parents[2])
    program = '''import sys, tempfile, subprocess
sys.path.insert(0, sys.argv[1])
tempfile.tempdir = sys.argv[2]
calls = []
def forbidden(*args, **kwargs):
    calls.append(args)
    raise RuntimeError('compiler must not run during import')
subprocess.run = forbidden
subprocess.Popen = forbidden
from tofu_db import wire
assert not calls, 'import attempted to launch a compiler'
'''
    result = subprocess.run([sys.executable, '-I', '-c', program, root, str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def accelerator(monkeypatch):
    if os.name != "posix":
        pytest.skip("optional compiler uses POSIX ownership and process groups")
    from tofu_db import _crc32c_accelerator as module
    monkeypatch.setattr(module, '_started', False)
    monkeypatch.setattr(module, '_closing', False)
    monkeypatch.setattr(module, '_candidate', None)
    monkeypatch.setattr(module, '_compiler', None)
    monkeypatch.setattr(module, '_directory', None)
    monkeypatch.setattr(module, '_next_probe', 0.0)
    return module


@pytest.mark.parametrize('kind', ['symlink', 'writable', 'foreign'])
def test_cache_rejects_untrusted_directory(accelerator, tmp_path, monkeypatch, kind):
    import os
    directory = tmp_path / 'cache'
    directory.mkdir(mode=0o700)
    if kind == 'symlink':
        alias = tmp_path / 'alias'
        alias.symlink_to(directory, target_is_directory=True)
        directory = alias
    elif kind == 'writable':
        directory.chmod(0o777)
    else:
        owner = os.getuid()
        monkeypatch.setattr(accelerator.os, 'getuid', lambda: owner + 1)
    assert not accelerator._owned(directory, directory=True)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'writable', 'oversized'])
def test_cache_rejects_untrusted_library(accelerator, tmp_path, kind):
    library = tmp_path / 'library.so'
    library.write_bytes(b'not executed')
    if kind == 'symlink':
        alias = tmp_path / 'alias.so'
        alias.symlink_to(library)
        library = alias
    elif kind == 'hardlink':
        (tmp_path / 'alias.so').hardlink_to(library)
    elif kind == 'writable':
        library.chmod(0o666)
    else:
        with library.open('wb') as stream:
            stream.truncate(accelerator.MAX_LIBRARY_BYTES + 1)
    assert not accelerator._owned(library, directory=False)


def test_repeated_first_use_starts_at_most_one_worker(accelerator, tmp_path, monkeypatch):
    starts = []
    class Worker:
        def __init__(self, **kwargs):
            assert kwargs['daemon'] is True
        def start(self):
            starts.append(True)
    monkeypatch.setattr(accelerator, '_cache_directory', lambda source: tmp_path)
    monkeypatch.setattr(accelerator, '_load_cached', lambda *args: None)
    monkeypatch.setattr(accelerator.threading, 'Thread', Worker)
    for _ in range(100):
        assert accelerator.accelerator('source', None) is None
    assert starts == [True]


def test_real_concurrent_callers_never_wait_for_compilation(accelerator, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    starts = []
    def worker(*args):
        starts.append(True)
        entered.set()
        release.wait(3)
    monkeypatch.setattr(accelerator, '_cache_directory', lambda source: tmp_path)
    monkeypatch.setattr(accelerator, '_load_cached', lambda *args: None)
    monkeypatch.setattr(accelerator, '_worker', worker)
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(accelerator.accelerator, 'source', None) for _ in range(64)]
            assert all(future.result(timeout=1) is None for future in futures)
        assert entered.wait(1)
        assert starts == [True]
    finally:
        release.set()


@pytest.mark.parametrize('success', [True, False])
def test_compiler_publishes_only_complete_output_and_cleans_staging(accelerator, tmp_path, monkeypatch, success):
    library = tmp_path / '_tofudb_crc32c_native.so'
    library.write_bytes(b'old')
    library.chmod(0o600)
    candidate = object()
    monkeypatch.setattr(accelerator, '_load_cached', lambda *args:
                        candidate if library.read_bytes() == b'complete' else None)
    def compile_source(source, output):
        assert output.parent != tmp_path
        output.write_bytes(b'partial')
        assert library.read_bytes() == b'old'
        output.write_bytes(b'complete')
        return success
    monkeypatch.setattr(accelerator, '_compile', compile_source)
    accelerator._build('source', tmp_path, None)
    assert library.read_bytes() == (b'complete' if success else b'old')
    assert accelerator._candidate is (candidate if success else None)
    assert not list(tmp_path.glob('build-*'))


def test_compile_timeout_kills_the_process_group(accelerator, tmp_path, monkeypatch):
    import signal
    waits, kills = [], []
    class Process:
        pid = 1234567
        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired('compiler', timeout)
            return -9
    def spawn(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
        return Process()
    monkeypatch.setattr(accelerator.shutil, 'which', lambda name: '/usr/bin/cc')
    monkeypatch.setattr(accelerator.subprocess, 'Popen', spawn)
    monkeypatch.setattr(accelerator.os, 'killpg', lambda pid, sig: kills.append((pid, sig)))
    assert not accelerator._compile('source', tmp_path / 'module.so')
    assert waits == [accelerator.COMPILE_TIMEOUT_SECONDS, 1]
    assert kills == [(1234567, signal.SIGKILL)]
    assert accelerator._compiler is None


def test_real_compiler_accelerator_matches_reference(accelerator, tmp_path):
    from tofu_db import wire
    if not accelerator.shutil.which('cc'):
        pytest.skip('C compiler unavailable')
    accelerator._build(wire._NATIVE_CRC32C_SOURCE, tmp_path, wire._crc32c_fallback)
    candidate = accelerator._candidate
    assert candidate is not None
    payload = bytes(range(256)) * 4096
    assert candidate(payload) == wire._crc32c_fallback(payload)
    assert candidate(payload[500:], candidate(payload[:500])) == candidate(payload)
    assert candidate(bytearray(payload)) == candidate(payload)
    assert (tmp_path / '_tofudb_crc32c_native.so').stat().st_size <= accelerator.MAX_LIBRARY_BYTES


def test_cache_key_budget_evicts_only_idle_entries(accelerator, tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(accelerator.tempfile, 'gettempdir', lambda: str(tmp_path))
    first = accelerator._cache_directory('version-0')
    descriptor = accelerator._open_lock(first)
    try:
        for version in range(1, 25):
            directory = accelerator._cache_directory(f'version-{version}')
            assert directory is not None
            assert first.is_dir(), 'an active compiler cache was evicted'
            assert len([path for path in first.parent.iterdir() if path.is_dir()]) <= accelerator.MAX_CACHE_KEYS
    finally:
        os.close(descriptor)


def test_stale_build_is_cleaned_before_new_compilation(accelerator, tmp_path, monkeypatch):
    stale = tmp_path / 'build-interrupted'
    stale.mkdir(mode=0o700)
    (stale / '_tofudb_crc32c_native.c').write_bytes(b'partial source')
    monkeypatch.setattr(accelerator, '_load_cached', lambda *args: None)
    calls = []
    def compile_source(source, output):
        assert not stale.exists()
        calls.append(True)
        return False
    monkeypatch.setattr(accelerator, '_compile', compile_source)
    accelerator._build('source', tmp_path, None)
    assert calls == [True]
    assert not list(tmp_path.glob('build-*'))


def test_busy_compiler_is_not_duplicated_and_later_result_is_adopted(accelerator, tmp_path, monkeypatch):
    import os
    descriptor = accelerator._open_lock(tmp_path)
    monkeypatch.setattr(accelerator, '_compile', lambda *args: pytest.fail('duplicate compiler'))
    try:
        accelerator._build('source', tmp_path, None)
        assert accelerator._candidate is None
    finally:
        os.close(descriptor)
    candidate = object()
    monkeypatch.setattr(accelerator, '_started', True)
    monkeypatch.setattr(accelerator, '_directory', tmp_path)
    monkeypatch.setattr(accelerator, '_load_cached', lambda *args: candidate)
    assert accelerator.accelerator('source', None) is candidate


def test_shutdown_retires_active_compiler_and_fences_new_work(accelerator, tmp_path, monkeypatch):
    process = object()
    retired = []
    monkeypatch.setattr(accelerator, '_compiler', process)
    monkeypatch.setattr(accelerator, '_kill_compiler', retired.append)
    monkeypatch.setattr(accelerator.shutil, 'which', lambda name: '/usr/bin/cc')
    monkeypatch.setattr(accelerator.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('spawn after shutdown'))
    accelerator._shutdown()
    assert retired == [process]
    assert not accelerator._compile('source', tmp_path / 'module.so')


def test_busy_cache_allocation_retries_at_a_bounded_rate(accelerator, tmp_path, monkeypatch):
    now, calls = [0.0], []
    candidate = object()
    def directory(source):
        calls.append(True)
        if len(calls) == 1:
            raise BlockingIOError('another process allocating')
        return tmp_path
    monkeypatch.setattr(accelerator.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(accelerator, '_cache_directory', directory)
    monkeypatch.setattr(accelerator, '_load_cached', lambda *args: candidate)
    for _ in range(100):
        assert accelerator.accelerator('source', None) is None
    assert len(calls) == 1
    now[0] = accelerator.CACHE_PROBE_INTERVAL_SECONDS + 1
    assert accelerator.accelerator('source', None) is candidate
    assert len(calls) == 2


def test_source_budget_precedes_filesystem_or_compiler_work(accelerator, tmp_path, monkeypatch):
    monkeypatch.setattr(accelerator.tempfile, 'gettempdir', lambda: pytest.fail('filesystem admission'))
    monkeypatch.setattr(accelerator.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('compiler admission'))
    source = 'x' * (accelerator.MAX_SOURCE_BYTES + 1)
    assert accelerator._cache_directory(source) is None
    assert not accelerator._compile(source, tmp_path / 'module.so')


@pytest.mark.skipif(os.name != "posix", reason="POSIX optional compiler lifecycle")
def test_interpreter_exit_kills_real_background_compiler(tmp_path):
    import os
    import signal
    root = str(Path(__file__).resolve().parents[2])
    compiler = tmp_path / 'cc'
    pid_file = tmp_path / 'compiler.pid'
    compiler.write_text('#!' + sys.executable + '\nimport os, pathlib, time\n'
                        'pathlib.Path(os.environ["CRC_TEST_PID"]).write_text(str(os.getpid()))\n'
                        'time.sleep(30)\n')
    compiler.chmod(0o700)
    program = '''import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, sys.argv[1]); tempfile.tempdir=sys.argv[2]
from tofu_db import wire
wire.crc32c(b'first request')
deadline=time.monotonic()+3
while not Path(sys.argv[3]).exists() and time.monotonic()<deadline:
    time.sleep(.02)
assert Path(sys.argv[3]).exists(), 'compiler did not start'
'''
    try:
        result = subprocess.run([sys.executable, '-I', '-c', program, root, str(tmp_path), str(pid_file)],
                                capture_output=True, text=True, timeout=8,
                                env={**os.environ, 'PATH': str(tmp_path), 'CRC_TEST_PID': str(pid_file)})
        assert result.returncode == 0, result.stderr
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_unsupported_platform_keeps_checksum_without_compiler(tmp_path):
    root = str(Path(__file__).resolve().parents[2])
    program = '''import os, sys, subprocess
sys.path.insert(0, sys.argv[1])
if hasattr(os, 'getuid'):
    del os.getuid
def forbidden(*args, **kwargs):
    raise AssertionError('unsupported platform launched a compiler')
subprocess.Popen=forbidden
from tofu_db import wire
assert wire.crc32c(b'123456789') == 0xE3069283
assert wire._NATIVE_CRC32C is None
'''
    result = subprocess.run([sys.executable, '-I', '-c', program, root],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
