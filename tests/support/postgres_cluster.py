"""Disposable PostgreSQL for real row-lock and semantic authority tests.

Entry points: postgres_cluster, owned_postgres_cluster, retire_cluster_root.
Each cluster owns a private Unix socket and foreground postmaster inside a
pytest root named after its owning process (``tofu-test-postgres-pid-<owner>-``
under the root parent of tests/conftest.py). Dynamic shared memory lives in the
data directory. On Linux the kernel fast-stops the postmaster when its owner
exits, even by SIGKILL, and that shutdown removes the SysV interlock segment.
The shared dead-owner root reclaim, also run at every fixture start, retires
leftover roots through retire_cluster_root. They use no existing service,
database, authentication profile or application.
"""

from contextlib import contextmanager
from pathlib import Path
import ctypes
import errno
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest


_LINUX = sys.platform.startswith("linux")
# <sys/ipc.h>.
_IPC_RMID = 0
# Zero-based postmaster.pid lines (PostgreSQL src/include/utils/pidfile.h).
_LOCK_FILE_LINE_PID = 0
_LOCK_FILE_LINE_DATA_DIR = 1
_LOCK_FILE_LINE_SHMEM_KEY = 6


class RestartablePostgresDsn(str):
    """A normal DSN with an explicit restart hook for its owning fixture."""
    def __new__(cls, value, restart):
        result = super().__new__(cls, value)
        result.restart = restart
        return result


# Arms the parent-death signal, then becomes the given program under the same
# PID. Running it as a separate executable keeps Python code out of the forked
# copy of a multi-threaded pytest process, where preexec_fn can deadlock.
_PARENT_DEATH_EXEC = (
    "import ctypes, os, sys\n"
    "owner, death_signal = int(sys.argv[1]), int(sys.argv[2])\n"
    "# PR_SET_PDEATHSIG is 1 in <linux/prctl.h>.\n"
    "if ctypes.CDLL(None, use_errno=True).prctl(1, death_signal, 0, 0, 0):\n"
    "    raise OSError(ctypes.get_errno(), 'prctl(PR_SET_PDEATHSIG) failed')\n"
    "if os.getppid() != owner:\n"
    "    raise SystemExit('PostgreSQL cluster owner exited before startup')\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n"
)


def _bound_to_owner(command: list[str]) -> list[str]:
    """Make the kernel fast-stop ``command`` (SIGINT) when this process exits.

    The signal follows the spawning thread, which for pytest is the main
    thread, and also fires when the owner is SIGKILLed.
    """
    if not _LINUX:
        return command
    return [sys.executable, "-I", "-S", "-c", _PARENT_DEATH_EXEC,
            str(os.getpid()), str(int(signal.SIGINT)), *command]


def _serves(pid: int, data: str) -> bool:
    """True while ``pid`` is a live process of the cluster in ``data``.

    PostgreSQL processes run in their data directory. A zombie or a missing
    process has no working directory; another user's process reusing the PID
    is not this user's cluster.
    """
    try:
        return os.path.samefile(f"/proc/{pid}/cwd", data)
    except (FileNotFoundError, PermissionError):
        return False


def _sysv_segment(shmid: int):
    with open("/proc/sysvipc/shm", encoding="ascii") as table:
        header = table.readline().split()
        for line in table:
            row = dict(zip(header, map(int, line.split())))
            if row["shmid"] == shmid:
                return row
    return None


def _remove_recorded_segment(key: int, shmid: int, creator: int) -> bool:
    """Remove a dead cluster's recorded interlock; True when none remains."""
    row = _sysv_segment(shmid)
    if (row is None or (row["key"] & 0xFFFFFFFF) != (key & 0xFFFFFFFF)
            or row["cpid"] != creator):
        # Already removed, or the identifier now names another segment.
        return True
    if row["uid"] != os.getuid() or row["cuid"] != os.getuid() or row["nattch"]:
        # Unproven ownership, or orphaned cluster processes still attached.
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.shmctl(shmid, _IPC_RMID, None) != 0:
        failure = ctypes.get_errno()
        if failure not in (errno.EINVAL, errno.EIDRM):
            raise OSError(failure, os.strerror(failure))
    return True


def retire_cluster_root(root: Path) -> bool:
    """Release what a cluster root still holds; True once ``root`` may be removed.

    A process still serving the recorded data directory is asked for a fast
    shutdown and the root is kept for a later reclaim. Otherwise the SysV
    segment recorded in postmaster.pid is removed only while it carries the
    recorded key and creator, belongs to this user and has nothing attached.
    A record that cannot be read or proven keeps the root.
    """
    try:
        lines = (root / "data" / "postmaster.pid").read_text().splitlines()
    except FileNotFoundError:
        # Never started, or a clean shutdown already released everything.
        return True
    if not _LINUX or len(lines) <= _LOCK_FILE_LINE_DATA_DIR:
        return False
    try:
        # A standalone (initdb bootstrap) backend records its PID negated.
        pid = abs(int(lines[_LOCK_FILE_LINE_PID]))
    except ValueError:
        return False
    if _serves(pid, lines[_LOCK_FILE_LINE_DATA_DIR]):
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass  # It exited after the check; the next reclaim proves the rest.
        return False
    segment = (lines[_LOCK_FILE_LINE_SHMEM_KEY].split()
               if len(lines) > _LOCK_FILE_LINE_SHMEM_KEY else [])
    if not segment:
        # The process exited before it created shared memory.
        return True
    try:
        key, shmid = (int(field) for field in segment)
    except ValueError:
        return False
    return _remove_recorded_segment(key, shmid, pid)


@contextmanager
def owned_postgres_cluster(*, max_connections: int = 16):
    """Own one cluster with explicit test demand; the fixture retains 16 slots."""
    if type(max_connections) is not int or max_connections < 4:
        raise ValueError('A private PostgreSQL cluster needs at least four slots')
    if os.name != "posix" or os.getuid() == 0:
        pytest.skip("Local PostgreSQL tests need an ordinary POSIX user")
    psycopg = pytest.importorskip("psycopg")
    initdb = shutil.which("initdb")
    postgres = shutil.which("postgres")
    if not initdb or not postgres:
        pytest.skip("Install PostgreSQL test binaries to exercise row locks")
    # Imported late: tests.conftest owns the pytest root parent and its
    # dead-owner reclaim, which imports this module while conftest loads.
    from tests.conftest import _PYTEST_ROOT_PARENT, _reclaim_stale_pytest_roots

    _reclaim_stale_pytest_roots()
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    # Unix socket paths have a small platform limit; the checkout's mount
    # prefix is too long. The root parent lives in the system temp directory.
    root = Path(tempfile.mkdtemp(prefix=f"tofu-test-postgres-pid-{os.getpid()}-",
                                 dir=str(_PYTEST_ROOT_PARENT)))
    server = None
    try:
        data = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        # A disposable cluster needs no crash-safe bootstrap; initdb's final
        # fsync of every data file alone can exceed the timeout on a busy disk.
        try:
            subprocess.run(
                [initdb, "-D", str(data), "--auth=trust", "--no-locale", "--encoding=UTF8",
                 "--no-sync"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Private PostgreSQL initdb failed: {diagnostic}") from exc
        dsn = psycopg.conninfo.make_conninfo(
            host=str(socket_dir), dbname="postgres", connect_timeout=1
        )
        with (root / "server.log").open("wb") as log:
            # mmap places dynamic shared memory in data/pg_dynshmem, so it is
            # removed with the root instead of accumulating in /dev/shm.
            arguments = [
                    postgres,
                    "-D",
                    str(data),
                    "-k",
                    str(socket_dir),
                    "-h",
                    "",
                    "-c",
                    f"max_connections={max_connections}",
                    "-c",
                    "shared_buffers=16MB",
                    "-c",
                    "dynamic_shared_memory_type=mmap",
                ]

            def start_server():
                nonlocal server
                # A separate session keeps signals for the owner's process
                # group from SIGKILLing the postmaster, which leaks its segment.
                server = subprocess.Popen(_bound_to_owner(arguments), env=environment,
                                          stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True)
                deadline = time.monotonic() + 20
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Private PostgreSQL exited during startup")
                    try:
                        with psycopg.connect(dsn):
                            return
                    except psycopg.OperationalError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Private PostgreSQL readiness deadline exceeded")
                        time.sleep(0.05)

            def restart_server():
                assert server is not None and server.poll() is None
                server.send_signal(signal.SIGINT)
                server.wait()
                start_server()

            start_server()
            yield RestartablePostgresDsn(dsn, restart_server)
    finally:
        # Teardown completes when the postmaster has exited, which removes its
        # SysV segment. An interrupted wait, or a crashed server whose processes
        # still hold its recorded segment, leaves the root to the dead-owner
        # reclaim instead of deleting files under a running cluster.
        if server is not None and server.poll() is None:
            server.send_signal(signal.SIGINT)
            server.wait()
        if retire_cluster_root(root):
            shutil.rmtree(root)


@pytest.fixture
def postgres_cluster():
    with owned_postgres_cluster() as dsn:
        yield dsn
