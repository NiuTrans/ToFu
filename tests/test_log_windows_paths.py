"""Windows startup/ACL boundary contracts, executable on POSIX via Win32 fakes."""
import ctypes
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from lib import log_retention as retention
from lib import log_windows_paths as windows

pytestmark = pytest.mark.unit


@pytest.fixture
def win_api(monkeypatch):
    state = SimpleNamespace(attributes=0, closed=[], fail_open=False,
                            fail_query=False, fail_transfer=False, calls=[], transfers=[])

    def create(*args):
        state.calls.append(args)
        return ctypes.c_void_p(-1).value if state.fail_open else 42

    def query(handle, kind, buffer, size):
        buffer._obj.attributes = state.attributes
        return not state.fail_query

    class Function:
        def __init__(self, fn):
            self.fn = fn
        def __call__(self, *args):
            return self.fn(*args)

    kernel = SimpleNamespace(CreateFileW=Function(create),
                             GetFileInformationByHandleEx=Function(query),
                             CloseHandle=Function(state.closed.append))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda n: PermissionError(n, 'denied'),
                        raising=False)

    def transfer(handle, flags):
        state.transfers.append((handle, flags))
        if state.fail_transfer:
            raise OSError('CRT transfer failed')
        return 123

    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(open_osfhandle=transfer))
    # O_BINARY is absent on POSIX; emulate Windows CRT constants locally.
    monkeypatch.setattr(os, 'O_BINARY', 0x8000, raising=False)
    return state


def test_windows_directory_boot_never_opens_crt_directory(tmp_path, monkeypatch, win_api):
    monkeypatch.setattr(retention, '_WINDOWS', True)
    win_api.attributes = 0x10
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('CRT directory open'))
    monkeypatch.setattr(os, 'chmod', lambda *a, **k: pytest.fail('not an ACL'))
    root = tmp_path / 'logs'
    assert retention.ensure_private_log_directory(root) is False
    assert root.is_dir()
    assert win_api.closed == [42]
    assert win_api.calls[0][5] & 0x00200000


@pytest.mark.parametrize('attributes,directory', [(0x410, True), (0x400, False),
                                                 (0x10, False), (0, True)])
def test_native_handle_rejects_reparse_and_wrong_type(win_api, attributes, directory):
    win_api.attributes = attributes
    with pytest.raises(OSError):
        windows.open_log_path('log', os.O_RDONLY, directory=directory)
    assert win_api.closed == [42]


@pytest.mark.parametrize('failure', ['fail_open', 'fail_query', 'fail_transfer'])
def test_native_errors_fail_closed_and_release_handles(win_api, failure):
    setattr(win_api, failure, True)
    with pytest.raises(OSError):
        windows.open_log_path('log', os.O_WRONLY | os.O_CREAT)
    assert win_api.closed == ([] if failure == 'fail_open' else [42])


def test_native_file_transfers_ownership_without_truncation(win_api):
    assert windows.open_log_path('log', os.O_WRONLY | os.O_APPEND | os.O_CREAT) == 123
    assert win_api.calls[0][4] == 4  # OPEN_ALWAYS, not CREATE_ALWAYS
    assert win_api.calls[0][1:3] == (0x40000000, 7)  # write, share read/write/delete
    assert win_api.transfers == [(42, os.O_BINARY | os.O_WRONLY | os.O_APPEND)]
    assert win_api.closed == []


def test_windows_file_append_and_maintenance_do_not_chmod(tmp_path, monkeypatch):
    target = tmp_path / 'app.log'
    target.write_bytes(b'first\n')
    monkeypatch.setattr(retention, '_WINDOWS', True)
    monkeypatch.setattr(windows, 'open_log_path',
                        lambda path, flags, **kw: os.open(path, flags, 0o600))
    monkeypatch.setattr(os, 'chmod', lambda *a, **k: pytest.fail('chmod on Windows'))
    monkeypatch.setattr(os, 'fchmod', lambda *a, **k: pytest.fail('fchmod on Windows'),
                        raising=False)
    assert retention.ensure_private_log_file(target, create=True) is False
    retention.append_bytes_locked(target, b'second\n')
    assert target.read_bytes() == b'first\nsecond\n'
    result = {'permissions_hardened': [], 'errors': []}
    for dry_run in (True, False):
        retention._harden_file_permissions(target, tmp_path, result, dry_run=dry_run)
    assert result == {'permissions_hardened': [], 'errors': []}
    result = retention.copytruncate_if_oversize(target, max_bytes=7, backup_count=1)
    assert result['errors'] == []
    assert target.read_bytes() == b''
    assert target.with_name('app.log.1').read_bytes() == b'second\n'


def test_windows_validation_permission_error_is_not_swallowed(tmp_path, monkeypatch, win_api):
    monkeypatch.setattr(retention, '_WINDOWS', True)
    win_api.fail_open = True
    with pytest.raises(PermissionError):
        retention.ensure_private_log_directory(tmp_path / 'logs')
    with pytest.raises(PermissionError):
        retention.ensure_private_log_file(tmp_path / 'app.log', create=True)
    result = {'permissions_hardened': [], 'errors': []}
    retention._harden_directory_permissions(tmp_path, result, dry_run=False)
    assert len(result['errors']) == 1
    assert result['permissions_hardened'] == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode and symlink contract')
def test_posix_modes_and_symlink_rejection(tmp_path):
    root = tmp_path / 'logs'
    root.mkdir(mode=0o755)

    root.chmod(0o755)
    assert retention.ensure_private_log_directory(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    target = root / 'app.log'
    target.write_bytes(b'keep')
    target.chmod(0o644)
    assert retention.ensure_private_log_file(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    link = tmp_path / 'link'
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        retention.ensure_private_log_directory(link)
    link.unlink()
    link.symlink_to(target)
    with pytest.raises(OSError):
        retention.ensure_private_log_file(link, create=True)
    assert target.read_bytes() == b'keep'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX descriptor identity contract')
def test_posix_directory_identity_race_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / 'logs'
    root.mkdir()
    root.chmod(0o755)
    real_fstat = os.fstat
    descriptors = []

    def replaced(fd):
        info = real_fstat(fd)
        descriptors.append(fd)
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)

    monkeypatch.setattr(os, 'fstat', replaced)
    with pytest.raises(OSError, match='changed while securing'):
        retention.ensure_private_log_directory(root)
    with pytest.raises(OSError):
        real_fstat(descriptors[0])
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission failure contract')
def test_posix_permission_errors_are_not_windows_fallback(tmp_path, monkeypatch):
    root = tmp_path / 'logs'
    root.mkdir()
    root.chmod(0o755)

    def denied(*args, **kwargs):
        raise PermissionError('denied')

    monkeypatch.setattr(os, 'open', denied)
    with pytest.raises(PermissionError):
        retention.ensure_private_log_directory(root)


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows filesystem/CRT')
def test_native_windows_directory_file_and_binary_append(tmp_path):
    root = tmp_path / 'logs'
    assert retention.ensure_private_log_directory(root) is False
    target = root / 'app.log'
    assert retention.ensure_private_log_file(target, create=True) is False
    retention.append_bytes_locked(target, b'first\n')
    retention.append_bytes_locked(target, b'second\n')
    assert target.read_bytes() == b'first\nsecond\n'
    result = retention.copytruncate_if_oversize(target, max_bytes=7, backup_count=1)
    assert result['errors'] == []
    assert target.with_name('app.log.1').read_bytes() == b'second\n'
