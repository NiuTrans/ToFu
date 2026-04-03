"""lib/compat.py — Cross-platform compatibility helpers.

Provides platform-aware abstractions for:
  - Shell invocation (Linux/macOS: /bin/sh, Windows: cmd.exe)
  - Non-blocking pipe I/O (fcntl on Unix, threading on Windows)
  - Process introspection (stdin detection via /proc on Linux, disabled elsewhere)
  - Temp directory paths
  - Username detection
  - Process existence checks
  - Signal compatibility

Usage::

    from lib.compat import (
        get_shell_args, get_username, get_temp_dir,
        is_process_alive, is_process_named, HAS_PROCFS,
        set_pipe_nonblocking, safe_select_pipes,
    )
"""

import getpass
import os
import sys
import tempfile

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Platform detection
# ═══════════════════════════════════════════════════════════════════════

IS_WINDOWS = (os.name == 'nt')
IS_MACOS = (sys.platform == 'darwin')
IS_LINUX = (sys.platform.startswith('linux'))

# /proc filesystem is only available on Linux
HAS_PROCFS = IS_LINUX and os.path.isdir('/proc')


# ═══════════════════════════════════════════════════════════════════════
#  Shell invocation
# ═══════════════════════════════════════════════════════════════════════

def get_shell_args(command: str) -> list:
    """Return the argv list to execute *command* via the platform's shell.

    - Linux/macOS: ``['/bin/sh', '-c', command]``
    - Windows: ``['cmd.exe', '/c', command]``

    Args:
        command: The shell command string to execute.

    Returns:
        List of arguments suitable for ``subprocess.Popen`` / ``subprocess.run``.
    """
    if IS_WINDOWS:
        # cmd.exe is always available on Windows
        return ['cmd.exe', '/c', command]
    else:
        return ['/bin/sh', '-c', command]


# ═══════════════════════════════════════════════════════════════════════
#  Username
# ═══════════════════════════════════════════════════════════════════════

def get_username(fallback: str = 'postgres') -> str:
    """Get the current OS username, cross-platform.

    Uses ``getpass.getuser()`` which works on Linux (``USER``),
    macOS (``USER``), and Windows (``USERNAME`` / win32api).

    Args:
        fallback: Value to return if username detection fails.

    Returns:
        The current username string.
    """
    try:
        return getpass.getuser()
    except Exception:
        return fallback


# ═══════════════════════════════════════════════════════════════════════
#  Temp directory
# ═══════════════════════════════════════════════════════════════════════

def get_temp_dir() -> str:
    """Return the platform-appropriate temp directory.

    - Linux: ``/tmp``
    - macOS: ``/var/folders/...`` or ``/tmp``
    - Windows: ``C:\\Users\\<user>\\AppData\\Local\\Temp``

    Returns:
        Absolute path to the temp directory.
    """
    return tempfile.gettempdir()


# ═══════════════════════════════════════════════════════════════════════
#  Process introspection
# ═══════════════════════════════════════════════════════════════════════

def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID exists, cross-platform.

    Args:
        pid: Process ID to check.

    Returns:
        True if the process exists and we have permission to signal it.
    """
    if IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but we can't signal it


def is_process_named(pid: int, name_substring: str) -> bool:
    """Check if a running process's command name contains *name_substring*.

    Args:
        pid: Process ID to check.
        name_substring: Substring to look for in the process name (case-insensitive).

    Returns:
        True if the process exists and its name matches, False otherwise.
        Returns False on any error (process exited, no permission, etc.).
    """
    import subprocess
    name_substring = name_substring.lower()

    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=5
            )
            return name_substring in result.stdout.lower()
        except Exception:
            return False
    else:
        # Unix: use ps (works on both Linux and macOS)
        try:
            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'comm='],
                capture_output=True, text=True, timeout=5
            )
            return name_substring in result.stdout.lower()
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════
#  Non-blocking pipe I/O
# ═══════════════════════════════════════════════════════════════════════

def set_pipe_nonblocking(fd) -> bool:
    """Set a file descriptor / pipe to non-blocking mode.

    Args:
        fd: A file object with a ``fileno()`` method (e.g., ``proc.stdout``).

    Returns:
        True if non-blocking mode was set successfully, False on Windows
        (where fcntl is not available — use threading-based reading instead).
    """
    if IS_WINDOWS:
        # fcntl not available on Windows — caller must use threading
        return False
    try:
        import fcntl
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return True
    except Exception as e:
        logger.debug('[compat] set_pipe_nonblocking failed: %s', e)
        return False


def safe_select_pipes(read_fds, timeout=0.2):
    """Platform-safe select() on pipe file descriptors.

    On Unix, uses ``select.select()``.
    On Windows, ``select()`` only works on sockets, so this returns
    all fds as "readable" (caller should use non-blocking read with
    exception handling).

    Args:
        read_fds: List of file descriptors to check for readability.
        timeout: Timeout in seconds.

    Returns:
        List of readable file descriptors.
    """
    if IS_WINDOWS:
        # select() doesn't work on pipes on Windows.
        # Return all fds — caller should handle BlockingIOError.
        import time
        time.sleep(timeout)
        return list(read_fds)
    else:
        import select
        try:
            readable, _, _ = select.select(read_fds, [], [], timeout)
            return readable
        except (ValueError, OSError):
            return []


# ═══════════════════════════════════════════════════════════════════════
#  Signal compatibility
# ═══════════════════════════════════════════════════════════════════════

def safe_signal(signum, handler):
    """Register a signal handler, returning the previous one.

    Silently skips if the signal doesn't exist on this platform
    (e.g., SIGTERM on Windows).

    Args:
        signum: Signal number (e.g., ``signal.SIGTERM``).
        handler: Signal handler function.

    Returns:
        The previous handler, or None if the signal was not available.
    """
    import signal
    try:
        return signal.signal(signum, handler)
    except (OSError, ValueError) as e:
        logger.debug('[compat] Cannot register signal %s: %s', signum, e)
        return None


# ═══════════════════════════════════════════════════════════════════════
#  Network mount detection
# ═══════════════════════════════════════════════════════════════════════

def is_network_mount(path: str) -> bool:
    """Detect if *path* is on a network/FUSE mount.

    Platform-specific heuristics:
      - Linux: path starts with ``/mnt/`` (DolphinFS, NFS, CIFS convention)
      - macOS: path under ``/Volumes/`` (excluding the boot volume)
      - Windows: UNC path (``\\\\server\\share``) or non-local drive

    This is a best-effort heuristic — not all network mounts are caught.

    Args:
        path: Absolute filesystem path.

    Returns:
        True if the path appears to be on a network/FUSE mount.
    """
    if IS_LINUX:
        return path.startswith('/mnt/')
    if IS_MACOS:
        # /Volumes/<name> is used for mounted volumes; the boot volume
        # is /Volumes/Macintosh HD (or similar) but we can't easily
        # distinguish local vs network here — be conservative.
        return False
    if IS_WINDOWS:
        # UNC paths are always network
        return path.startswith('\\\\')
    return False


# ═══════════════════════════════════════════════════════════════════════
#  Shell argument splitting
# ═══════════════════════════════════════════════════════════════════════

def safe_shlex_split(command: str) -> list:
    """Split a shell command string into tokens, cross-platform.

    On Unix, uses POSIX-mode splitting (handles backslash escapes).
    On Windows, uses non-POSIX mode (backslashes are path separators,
    only double-quotes for quoting).

    Args:
        command: Shell command string to split.

    Returns:
        List of argument tokens.
    """
    import shlex
    return shlex.split(command, posix=not IS_WINDOWS)
