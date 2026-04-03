"""Cross-platform compatibility tests.

Migrated from debug/test_cross_platform.py. Validates the compat layer,
optional dependency flags, command safety analysis, and key module imports.
"""

import os
import platform
import signal
import sys

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
        __import__(mod_name)


# ═══════════════════════════════════════════════════════════
#  FS keepalive graceful skip
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFsKeepalive:
    def test_start_no_error(self):
        from lib.fs_keepalive import start_fs_keepalive
        start_fs_keepalive()  # should not raise

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
        from lib.fetch.utils import HAS_FITZ, HAS_PIL, HAS_PLAYWRIGHT, HAS_TRAFILATURA
        assert isinstance(HAS_TRAFILATURA, bool)
        assert isinstance(HAS_PLAYWRIGHT, bool)
        assert isinstance(HAS_FITZ, bool)
        assert isinstance(HAS_PIL, bool)

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
        from lib.project_mod.tools import _is_destructive_command
        assert not _is_destructive_command('echo hello')

    def test_rm_rf_destructive(self):
        from lib.project_mod.tools import _is_destructive_command
        assert _is_destructive_command('rm -rf /tmp/foo')

    def test_git_status_safe(self):
        from lib.project_mod.tools import _is_destructive_command
        assert not _is_destructive_command('git status')
