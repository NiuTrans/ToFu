"""Cross-platform compatibility tests.

Migrated from debug/test_cross_platform.py. Validates the compat layer,
optional dependency flags, command safety analysis, and key module imports.
"""

import os
import platform
import signal
import sys
import time

import pytest

from lib.compat import (
    HAS_PROCFS,
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    get_shell_args,
    get_temp_dir,
    get_username,
    is_process_alive,
    safe_select_pipes,
    safe_signal,
    set_pipe_nonblocking,
)

# ═══════════════════════════════════════════════════════════
#  Platform detection
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPlatformDetection:
    def test_exactly_one_platform_flag(self):
        count = sum([IS_WINDOWS, IS_MACOS, IS_LINUX])
        assert count == 1, f'Expected exactly 1 platform flag, got {count}'

    def test_linux_has_procfs(self):
        if IS_LINUX:
            assert HAS_PROCFS
        elif IS_WINDOWS or IS_MACOS:
            assert not HAS_PROCFS


# ═══════════════════════════════════════════════════════════
#  Shell invocation
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestShellArgs:
    def test_returns_list(self):
        args = get_shell_args('echo hello')
        assert isinstance(args, list)
        assert len(args) == 3

    def test_correct_shell(self):
        args = get_shell_args('echo hello')
        if IS_WINDOWS:
            assert args[0] == 'cmd.exe'
            assert args[1] == '/c'
        else:
            assert args[0] == '/bin/sh'
            assert args[1] == '-c'


# ═══════════════════════════════════════════════════════════
#  Username detection
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUsername:
    def test_non_empty(self):
        username = get_username()
        assert isinstance(username, str)
        assert len(username) > 0


# ═══════════════════════════════════════════════════════════
#  Temp directory
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTempDir:
    def test_exists(self):
        tmp = get_temp_dir()
        assert os.path.isdir(tmp)


# ═══════════════════════════════════════════════════════════
#  Process introspection
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProcessAlive:
    def test_own_pid_alive(self):
        assert is_process_alive(os.getpid())

    def test_bogus_pid_not_alive(self):
        assert not is_process_alive(99999999)


# ═══════════════════════════════════════════════════════════
#  Pipe I/O
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPipeIO:
    def test_safe_select_pipes_returns_list(self):
        assert isinstance(safe_select_pipes([], timeout=0.01), list)


@pytest.fixture
def descriptor_room():
    """A soft descriptor limit that admits numbers above select()'s ceiling."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 4096
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip(f'descriptor hard limit {hard} is below {wanted}')
    raised = soft != resource.RLIM_INFINITY and soft < wanted
    if raised:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    yield
    if raised:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _pipe_above_select_ceiling():
    """A pipe whose read end is numbered above FD_SETSIZE (1024)."""
    import fcntl
    read_fd, write_fd = os.pipe()
    high = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 1500)
    os.close(read_fd)
    return os.fdopen(high, 'rb', buffering=0), write_fd


@pytest.mark.unit
@pytest.mark.skipif(IS_WINDOWS, reason='POSIX pipe descriptors')
class TestPipeWaitAboveSelectCeiling:
    """Long-lived servers reach descriptor numbers that select() rejects
    (live 2026-11-07: a command's pipes were fds 1817 and 4009). The wait
    still reports readiness, and a quiet or closed pipe still waits."""

    def test_readable_pipe_above_the_ceiling_is_reported(self, descriptor_room):
        stream, write_fd = _pipe_above_select_ceiling()
        try:
            os.write(write_fd, b'x')
            assert safe_select_pipes([stream], timeout=5) == [stream]
        finally:
            stream.close()
            os.close(write_fd)

    def test_quiet_pipe_above_the_ceiling_waits_for_the_timeout(self, descriptor_room):
        stream, write_fd = _pipe_above_select_ceiling()
        try:
            started = time.monotonic()
            assert safe_select_pipes([stream], timeout=0.3) == []
            assert time.monotonic() - started >= 0.25
        finally:
            stream.close()
            os.close(write_fd)

    def test_end_of_file_is_reported_so_the_reader_sees_it(self, descriptor_room):
        stream, write_fd = _pipe_above_select_ceiling()
        try:
            os.close(write_fd)
            assert safe_select_pipes([stream], timeout=5) == [stream]
            assert stream.read() == b''
        finally:
            stream.close()

    def test_closed_stream_waits_instead_of_returning_at_once(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, 'rb', buffering=0)
        stream.close()
        try:
            started = time.monotonic()
            assert safe_select_pipes([stream], timeout=0.3) == []
            assert time.monotonic() - started >= 0.25
        finally:
            os.close(write_fd)

    def test_set_pipe_nonblocking_on_windows(self):
        if IS_WINDOWS:
            assert set_pipe_nonblocking(sys.stdout) is False


# ═══════════════════════════════════════════════════════════
#  Signal compatibility
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSignals:
    def test_sigint_handled(self):
        prev = safe_signal(signal.SIGINT, signal.SIG_DFL)
        assert prev is not None
        safe_signal(signal.SIGINT, prev)  # restore


# ═══════════════════════════════════════════════════════════
#  Key module imports
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestKeyModuleImports:
    @pytest.mark.parametrize('mod_name', [
        'lib.compat',
        'lib.log',
        'lib.fs_keepalive',
        'lib.project_mod.config',
        'lib.project_mod.scanner',
    ])
    def test_import_succeeds(self, mod_name):
        import importlib
        module = importlib.import_module(mod_name)
        assert module.__name__ == mod_name


# ═══════════════════════════════════════════════════════════
#  FS keepalive graceful skip
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFsKeepalive:
    def test_start_no_error(self, monkeypatch):
        import lib.fs_keepalive as keepalive
        # Force the ordinary local-disk no-op so this unit test never starts a
        # real daemon against the developer's mount.
        monkeypatch.setattr(keepalive, '_is_network_mount', lambda _p: False)
        before = keepalive._thread
        result = keepalive.start_fs_keepalive()
        assert result is None
        assert keepalive._thread is before

    def test_network_mount_detection(self):
        from lib.fs_keepalive import _is_network_mount
        if IS_LINUX:
            assert _is_network_mount('/mnt/data')
            assert not _is_network_mount('/home/user')


# ═══════════════════════════════════════════════════════════
#  Optional dependency flags
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestOptionalDependencyFlags:
    def test_fetch_utils_flags(self):
        from tofu_search.fetch.utils import HAS_FITZ, HAS_PLAYWRIGHT, HAS_TRAFILATURA
        assert isinstance(HAS_TRAFILATURA, bool)
        assert isinstance(HAS_PLAYWRIGHT, bool)
        assert isinstance(HAS_FITZ, bool)

    def test_pdf_parser_flags(self):
        from lib.pdf_parser._common import HAS_PYMUPDF, HAS_PYMUPDF4LLM
        assert isinstance(HAS_PYMUPDF, bool)
        assert isinstance(HAS_PYMUPDF4LLM, bool)


# ═══════════════════════════════════════════════════════════
#  Command safety analysis
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCommandSafety:
    def test_echo_safe(self):
        from lib.project_mod.command_analysis import _is_destructive_command
        assert not _is_destructive_command('echo hello')

    def test_rm_rf_destructive(self):
        from lib.project_mod.command_analysis import _is_destructive_command
        assert _is_destructive_command('rm -rf /tmp/foo')

    def test_git_status_safe(self):
        from lib.project_mod.command_analysis import _is_destructive_command
        assert not _is_destructive_command('git status')
