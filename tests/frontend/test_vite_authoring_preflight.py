"""Content-fresh lifecycle validation keeps framing, bounds, and filesystem errors."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import vite_assets as assets

pytestmark = pytest.mark.unit


def _sources(tmp_path, monkeypatch):
    source = tmp_path / 'frontend/src'
    source.mkdir(parents=True)
    monkeypatch.setattr(assets, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(assets, 'VITE_AUTHORING_CONFIG_PATHS', ())
    return source


def test_scan_preserves_symlink_file_inputs_without_following_directories(tmp_path, monkeypatch):
    source = _sources(tmp_path, monkeypatch)
    (source / 'a.ts').write_text('a')
    nested = source / 'nested'
    nested.mkdir()
    (nested / 'b.ts').write_text('b')
    (source / 'alias.ts').symlink_to(nested / 'b.ts')
    (source / 'directory-alias').symlink_to(nested, target_is_directory=True)
    (source / 'missing.ts').symlink_to(source / 'missing')
    assert [str(Path(p).relative_to(source)) for p in assets.vite_authoring_inputs()] == ['a.ts', 'alias.ts', 'nested/b.ts']


@pytest.mark.parametrize('cpus,memory', [(1, 0), (4, 512)])
def test_parallel_digest_matches_streaming_framing_and_detects_same_mtime_edits(tmp_path, monkeypatch, cpus, memory):
    source = _sources(tmp_path, monkeypatch)
    (source / 'small.ts').write_bytes(b'original')
    (source / 'large.ts').write_bytes(b'large' * 300000)
    import runtime_guards
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda: SimpleNamespace(
        effective_cpu_count=cpus, effective_memory_available_mb=memory))
    expected = hashlib.sha256(b'tofu-vite-authoring-v1\0')
    for path in sorted(assets.vite_authoring_inputs()):
        expected.update(Path(path).relative_to(tmp_path).as_posix().encode() + b'\0')
        expected.update(Path(path).read_bytes() + b'\0')
    before = assets._source_vite_authoring_digest()
    assert before == expected.hexdigest()
    changed = source / 'small.ts'
    stat = changed.stat()
    changed.write_bytes(b'modified')
    os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert assets._source_vite_authoring_digest() != before


def test_source_read_failure_invalidates_preflight(tmp_path, monkeypatch):
    source = _sources(tmp_path, monkeypatch)
    missing = str(source / 'deleted.ts')
    monkeypatch.setattr(assets, 'vite_authoring_inputs', lambda: (missing,))
    with pytest.raises(FileNotFoundError):
        assets._source_vite_authoring_digest()


def test_all_source_reads_start_with_idle_hint_one_and_release_spool(tmp_path, monkeypatch):
    import builtins
    import threading
    import time
    import tempfile
    import runtime_guards

    source = _sources(tmp_path, monkeypatch)
    for i in range(32):
        (source / f'{i:02}.ts').write_bytes(bytes([i]) * 65536)
    inputs = sorted(assets.vite_authoring_inputs())
    expected = assets._stream_authoring_digest(inputs)
    barrier = threading.Barrier(32, timeout=10)
    spools, futures = [], []
    original_open = builtins.open
    original_spool = tempfile.TemporaryFile
    original_submit = assets.InstrumentedThreadPoolExecutor.submit

    def read(path, *args, **kwargs):
        if threading.current_thread().name.startswith('vite-source-read'):
            barrier.wait()
        return original_open(path, *args, **kwargs)

    def spool(*args, **kwargs):
        file = original_spool(*args, **kwargs)
        spools.append(file)
        return file

    def submit(pool, *args, **kwargs):
        future = original_submit(pool, *args, **kwargs)
        futures.append(future)
        return future

    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda: SimpleNamespace(
        effective_cpu_count=1, effective_memory_available_mb=0))
    monkeypatch.setattr(assets, 'open', read, raising=False)
    monkeypatch.setattr(assets.tempfile, 'TemporaryFile', spool)
    monkeypatch.setattr(assets.InstrumentedThreadPoolExecutor, 'submit', submit)
    started = time.monotonic()
    assert assets._source_vite_authoring_digest() == expected
    assert len(futures) == 32
    assert all(all(type(offset) is int and type(size) is int
                   for offset, size in future.result()) for future in futures)
    assert len(spools) == 1 and spools[0].closed
    assert not any(t.name.startswith('vite-source-read') for t in threading.enumerate())
    print(f'32 source reads / 2097152 bytes: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers, 0 spool FDs')


def test_unstarted_source_read_falls_back_without_duplicate_read(tmp_path, monkeypatch):
    import builtins
    import threading

    source = _sources(tmp_path, monkeypatch)
    for i in range(8):
        (source / f'{i}.ts').write_text(str(i))
    inputs = sorted(assets.vite_authoring_inputs())
    expected = assets._stream_authoring_digest(inputs)
    original_start = threading.Thread.start
    original_open = builtins.open
    called = []
    lock = threading.Lock()
    starts = 0

    def start(thread):
        nonlocal starts
        if thread.name.startswith('vite-source-read'):
            starts += 1
            if starts == 1:
                raise RuntimeError('unstarted source read')
        return original_start(thread)

    def read(path, *args, **kwargs):
        with lock:
            called.append(str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(assets, 'open', read, raising=False)
    assert assets._source_vite_authoring_digest() == expected
    assert sorted(called) == inputs
    assert not any(t.name.startswith('vite-source-read') for t in threading.enumerate())


@pytest.mark.parametrize('failure', ['create', 'write', 'read'])
def test_optional_spool_failure_uses_exact_streaming_fallback(tmp_path, monkeypatch, failure):
    import tempfile
    import threading

    source = _sources(tmp_path, monkeypatch)
    for i in range(8):
        (source / f'{i}.ts').write_bytes(b'large' * 300000)
    expected = assets._stream_authoring_digest(sorted(assets.vite_authoring_inputs()))
    original = tempfile.TemporaryFile
    files = []

    class BrokenWrite:
        def __init__(self, **kwargs):
            self.file = original(**kwargs)
            files.append(self.file)
        def __getattr__(self, name):
            return getattr(self.file, name)
        def write(self, data):
            if failure == 'write':
                raise OSError('temporary spool storage unavailable')
            return self.file.write(data)
        def read(self, size):
            raise OSError('temporary spool read unavailable')

    def factory(**kwargs):
        if failure == 'create':
            raise OSError('temporary spool creation unavailable')
        return BrokenWrite(**kwargs)

    monkeypatch.setattr(assets.tempfile, 'TemporaryFile', factory)
    assert assets._source_vite_authoring_digest() == expected
    assert all(file.closed for file in files)
    assert not any(t.name.startswith('vite-source-read') for t in threading.enumerate())
