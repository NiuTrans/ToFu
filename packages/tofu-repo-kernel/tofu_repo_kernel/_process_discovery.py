"""One digest-verified Rust process per discovery, with joined terminal cleanup.

The caller owns authorization and concurrency. No pool, queue or repository cache
is introduced. The child changes cwd after exec, keeping blocked filesystem
setup inside its parent's timeout/cancellation ownership.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import stat
from threading import Lock
import sys
import time

from ._discovery import DiscoveryError, validate_request


class DiscoveryTimeout(TimeoutError):
    """Deadline outcome; cancellation and joined exit remain separate facts."""
    def __init__(self, message, *, cancelled=False):
        super().__init__(message)
        self.timed_out = True
        self.cancelled = cancelled
        self.returncode = None


class DiscoveryCancelled(Exception):
    """Cancellation outcome; deadline and joined exit remain separate facts."""
    def __init__(self, message, *, timed_out=False):
        super().__init__(message)
        self.timed_out = timed_out
        self.cancelled = True
        self.returncode = None


class ProcessDiscovery:
    def __init__(self, path, expected_sha256):
        path = Path(path).resolve()
        self.path = str(path)  # Receipt identity only; execution uses the sealed fd.
        self._lock = Lock()
        self._image = seal_executable(path, expected_sha256)

    def close(self):
        """Refuse future launches; children already launched own their image fd."""
        with self._lock:
            self._image.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _start(self, root, timeout_ns):
        descriptor = None
        try:
            with self._lock:
                descriptor = os.dup(self._image.fileno())
            command = [f'/proc/self/fd/{descriptor}',
                       os.fsdecode(os.path.abspath(root)), str(timeout_ns)]
            # Never chdir in the pre-exec child: filesystem setup belongs inside
            # the Rust process after Popen has returned its cleanup-owned PID.
            return subprocess.Popen(command, pass_fds=(descriptor,),
                                    env=discovery_environment(),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def list_files(self, root, *, timeout=60.0, cancellation=None):
        root, timeout_ns = validate_request(root, timeout)
        if cancellation is not None and cancellation.is_set():
            raise DiscoveryCancelled('repository discovery cancelled before launch', timed_out=timeout_ns == 0)
        if timeout_ns == 0:
            raise DiscoveryTimeout('repository discovery deadline elapsed before launch')
        deadline = time.monotonic() + timeout
        process = self._start(root, timeout_ns)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if cancellation is not None and cancellation.is_set():
                    raise DiscoveryCancelled('repository discovery cancelled', timed_out=remaining <= 0)
                if remaining <= 0:
                    raise DiscoveryTimeout('repository discovery deadline elapsed')
                try:
                    output, _errors = process.communicate(
                        timeout=remaining if cancellation is None else min(remaining, .05))
                    break
                except subprocess.TimeoutExpired:
                    if cancellation is None:
                        raise DiscoveryTimeout('repository discovery deadline elapsed') from None
            timed_out = process.returncode == 3 or time.monotonic() >= deadline
            if cancellation is not None and cancellation.is_set():
                raise DiscoveryCancelled('repository discovery cancelled', timed_out=timed_out)
            if timed_out:
                raise DiscoveryTimeout('native repository discovery deadline elapsed')
            if process.returncode:
                raise DiscoveryError(process.returncode)
            if output and (not output.endswith(b'\0') or output.startswith(b'\0') or b'\0\0' in output):
                raise ValueError('native repository discovery returned an invalid path frame')
            return output[:-1].split(b'\0') if output else []
        finally:
            primary = sys.exc_info()[1]
            try:
                retire_process(process)
            except BaseException as cleanup:
                if primary is None:
                    raise
                if hasattr(primary, 'add_note'):
                    primary.add_note('discovery process cleanup failed: ' + type(cleanup).__name__)
            finally:
                if isinstance(primary, (DiscoveryTimeout, DiscoveryCancelled)):
                    primary.returncode = process.returncode
                    primary.timed_out |= process.returncode == 3


def retire_process(process):
    """Kill then join the owned child, preserving repeated user interrupts."""
    interrupted = None
    try:
        while True:
            try:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass  # The owned process exited between poll and kill.
                process.wait()
                break
            except KeyboardInterrupt as error:
                interrupted = error
                # Retry kill as well as wait: the signal may have arrived
                # before kill reached the kernel.
    finally:
        process.stdout.close()
        process.stderr.close()
    if interrupted is not None:
        raise interrupted


def seal_executable(path, expected_sha256):
    """Hash the copied bytes and seal them before any execution (Linux only).

    The file object owns the single retained fd until close or owner collection.
    Each launch duplicates it, so concurrent close cannot redirect an fd reused
    by another thread. This standalone package has no application dependencies.
    """
    if sys.platform != 'linux':
        raise RuntimeError('native discovery process requires Linux sealed execution')
    import fcntl
    descriptor = create_memfd()
    try:
        source_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('native discovery executable must be a regular file')
            digest = hashlib.sha256()
            remaining = info.st_size
            while remaining:
                block = os.read(source_fd, min(65536, remaining))
                if not block:
                    raise ValueError('native discovery executable changed during verification')
                remaining -= len(block)
                digest.update(block)
                offset = 0
                while offset < len(block):
                    written = os.write(descriptor, block[offset:])
                    if written <= 0:
                        raise OSError('native discovery executable copy failed')
                    offset += written
            if os.read(source_fd, 1) or digest.hexdigest() != expected_sha256:
                raise ValueError('native discovery executable digest differs')
        finally:
            os.close(source_fd)
        os.fchmod(descriptor, 0o500)
        # Linux UAPI seals: SEAL, SHRINK, GROW, WRITE. Older Python headers may
        # omit these constants even when the running kernel supports them.
        fcntl.fcntl(descriptor, getattr(fcntl, 'F_ADD_SEALS', 1033), 15)
        if fcntl.fcntl(descriptor, getattr(fcntl, 'F_GET_SEALS', 1034)) & 15 != 15:
            raise OSError('native discovery executable was not sealed')
        return os.fdopen(descriptor, 'rb', buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def create_memfd():
    if hasattr(os, 'memfd_create'):
        return os.memfd_create('tofu-repository-discovery', 1 | 2)
    # Known Linux 64-bit syscall ABIs for Python built with older libc headers.
    import ctypes
    number = {'x86_64': 319, 'aarch64': 279}.get(os.uname().machine)
    if number is None or ctypes.sizeof(ctypes.c_long) != 8:
        raise RuntimeError('native discovery memfd syscall ABI is unsupported')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    descriptor = libc.syscall(ctypes.c_long(number),
                              ctypes.c_char_p(b'tofu-repository-discovery'),
                              ctypes.c_uint(1 | 2))
    if descriptor < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return int(descriptor)


# Local path/config inputs only. The worker performs no network/authentication
# operation and must not inherit application/provider credentials or loader hooks.
_DISCOVERY_ENVIRONMENT = frozenset({
    'HOME', 'XDG_CONFIG_HOME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE',
    'RIPGREP_CONFIG_PATH',
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
    'GIT_COMMON_DIR', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CEILING_DIRECTORIES',
    'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_CONFIG', 'GIT_CONFIG_GLOBAL',
    'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_COUNT',
    'GIT_CONFIG_PARAMETERS', 'GIT_NO_REPLACE_OBJECTS', 'GIT_REPLACE_REF_BASE',
    'GIT_NAMESPACE', 'GIT_OPTIONAL_LOCKS',
})


def discovery_environment(environment=None):
    """Project declared Git inputs without reading unrelated environment values.

    An explicit mapping supports a caller-owned environment; omission borrows
    the launching process's environment. Indexed keys retain ASCII digits only.
    """
    source = os.environ if environment is None else environment
    result = {}
    for name in _DISCOVERY_ENVIRONMENT:
        value = source.get(name)
        if value is not None:
            result[name] = value
    for name in source:
        if name.startswith('GIT_CONFIG_KEY_'):
            suffix = name[len('GIT_CONFIG_KEY_'):]
        elif name.startswith('GIT_CONFIG_VALUE_'):
            suffix = name[len('GIT_CONFIG_VALUE_'):]
        else:
            continue
        if suffix.isascii() and suffix.isdecimal():
            result[name] = source[name]
    return result
