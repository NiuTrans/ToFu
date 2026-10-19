"""The optional generated FFI preserves sealed-image admission and cleanup."""
import hashlib
import os
from types import SimpleNamespace

import pytest

from lib import native_binary as binary
from lib.knowledge.generated_native_html_cffi import ffi

pytestmark = pytest.mark.unit


def test_generated_ffi_seals_exact_bytes_without_python_memfd(tmp_path, monkeypatch):
    monkeypatch.delattr(os, 'memfd_create', raising=False)
    source = tmp_path / 'image'
    raw = b'\x7fELFsource bytes'
    source.write_bytes(raw)
    before = set(os.listdir('/proc/self/fd'))
    with binary._seal(source, hashlib.sha256(raw).hexdigest(), len(raw), ffi=ffi) as image:
        assert os.pread(image.fileno(), len(raw), 0) == raw
        with pytest.raises(PermissionError):
            os.write(image.fileno(), b'changed')
    with pytest.raises(binary.NativeConfigurationError, match='checksum'):
        binary._seal(source, '0' * 64, len(raw), ffi=ffi)
    assert set(os.listdir('/proc/self/fd')) == before


def test_python_memfd_remains_the_preferred_port(monkeypatch):
    calls = []
    monkeypatch.setattr(os, 'memfd_create', lambda *args: calls.append(args) or 72,
                        raising=False)
    assert binary._memfd(ffi=object()) == 72
    assert calls == [('tofu-native', 3)]


def fake_ffi(monkeypatch, syscall, close):
    monkeypatch.delattr(os, 'memfd_create', raising=False)
    library = SimpleNamespace(syscall=syscall)
    return SimpleNamespace(sizeof=ffi.sizeof, new=ffi.new, cast=ffi.cast,
                           dlopen=lambda name: library, dlclose=close)


def test_failed_syscall_unloads_libc_and_preserves_typed_error(monkeypatch):
    closed = []
    selected = fake_ffi(monkeypatch, lambda *_: -1, closed.append)
    with pytest.raises(binary.NativeProcessError, match='unavailable'):
        binary._memfd(ffi=selected)
    assert len(closed) == 1


def test_libc_close_failure_releases_new_descriptor(monkeypatch, tmp_path):
    descriptor = os.open(tmp_path, os.O_RDONLY)
    failure = KeyboardInterrupt('close control')
    def close(_):
        raise failure
    selected = fake_ffi(monkeypatch, lambda *_: descriptor, close)
    with pytest.raises(KeyboardInterrupt) as caught:
        binary._memfd(ffi=selected)
    assert caught.value is failure
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_call_and_libc_close_failures_both_survive(monkeypatch):
    primary, cleanup = KeyboardInterrupt('call'), OSError('close')
    def syscall(*_):
        raise primary
    def close(_):
        raise cleanup
    selected = fake_ffi(monkeypatch, syscall, close)
    with pytest.raises(BaseExceptionGroup) as caught:
        binary._memfd(ffi=selected)
    assert caught.value.exceptions == (primary, cleanup)


def test_libc_and_descriptor_cleanup_failures_both_survive(monkeypatch):
    primary, cleanup = KeyboardInterrupt('libc close'), OSError('fd close')
    def close(_):
        raise primary
    def fd_close(_):
        raise cleanup
    selected = fake_ffi(monkeypatch, lambda *_: 72, close)
    monkeypatch.setattr(os, 'close', fd_close)
    with pytest.raises(BaseExceptionGroup) as caught:
        binary._memfd(ffi=selected)
    assert caught.value.exceptions == (primary, cleanup)


@pytest.mark.parametrize('machine,width', [('unknown', 8), ('x86_64', 4)])
def test_unknown_platform_abi_refuses_before_loading_libc(monkeypatch, machine, width):
    monkeypatch.delattr(os, 'memfd_create', raising=False)
    monkeypatch.setattr(os, 'uname', lambda: SimpleNamespace(machine=machine))
    selected = SimpleNamespace(sizeof=lambda _: width)
    with pytest.raises(binary.NativeConfigurationError, match='ABI'):
        binary._memfd(ffi=selected)
