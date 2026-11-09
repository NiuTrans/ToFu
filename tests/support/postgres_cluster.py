"""Disposable PostgreSQL for real row-lock and semantic authority tests.

Entry points: postgres_cluster, owned_postgres_cluster, postgres_bootstrap,
retire_cluster_root.
Each cluster owns a private Unix socket and foreground postmaster inside a
pytest root named after its owning process (``tofu-test-postgres-pid-<owner>-``
under the root parent of tests/conftest.py). Dynamic shared memory lives in the
data directory. On Linux the kernel fast-stops the postmaster when its owner
exits, even by SIGKILL, and that shutdown removes the SysV interlock segment.
The shared dead-owner root reclaim, also run at every fixture start, retires
leftover roots through retire_cluster_root. The fixture copies one pristine
bootstrap per pytest worker; each case still owns its roles, WAL and server.
Explicit restart retains a durable fast shutdown. Final disposal uses PostgreSQL
immediate shutdown, then waits for exit before removing the private files and
IPC resources. It does not checkpoint a database that is about to be deleted.
They use no existing service, database, authentication profile or application.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from tests.support.postgres_cleanup import retire_cluster_root as retire_cluster_root

# semantic_authority consumes the session clock fixture; modules that opt into
# a real PostgreSQL cluster alone (goals, router, orchestration) must still see
# it, not only modules that also register semantic_authorities.
pytest_plugins = ('tests.support.postgres_worker_clock',)


_LINUX = sys.platform.startswith("linux")


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


def _postgres_tools():
    if os.name != "posix" or os.getuid() == 0:
        pytest.skip("Local PostgreSQL tests need an ordinary POSIX user")
    psycopg = pytest.importorskip("psycopg")
    initdb = shutil.which("initdb")
    postgres = shutil.which("postgres")
    if not initdb or not postgres:
        pytest.skip("Install PostgreSQL test binaries to exercise row locks")
    return psycopg, initdb, postgres


def _cluster_root(*, template=False):
    # Imported late: tests.conftest owns the pytest root parent and its
    # dead-owner reclaim; startup reclamation imports only postgres_cleanup.
    from tests.conftest import _PYTEST_ROOT_PARENT, _reclaim_stale_pytest_roots

    _reclaim_stale_pytest_roots()
    # Unix socket paths have a small platform limit; the checkout's mount
    # prefix is too long. The root parent lives in the system temp directory.
    label = 'postgres-template' if template else 'postgres'
    return Path(tempfile.mkdtemp(prefix=f"tofu-test-{label}-pid-{os.getpid()}-",
                                dir=str(_PYTEST_ROOT_PARENT)))


def _initialize_data(data, initdb, environment):
    # Keep the existing bootstrap semantics: durability remains enabled when
    # the independently owned postmaster starts.
    try:
        subprocess.run(
            [initdb, "-D", str(data), "--auth=trust", "--no-locale", "--encoding=UTF8",
             "--no-sync"],
            env=environment, check=True, capture_output=True, text=True, timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"Private PostgreSQL initdb failed: {diagnostic}") from exc


def _bootstrap_inputs(initdb, postgres, environment):
    identities = []
    for executable in (initdb, postgres):
        path = Path(executable).resolve()
        stat = path.stat()
        identities.append((str(path), stat.st_dev, stat.st_ino, stat.st_size,
                           stat.st_mtime_ns, stat.st_ctime_ns))
    return (*identities, tuple(sorted(environment.items())))


@dataclass(frozen=True)
class PostgresBootstrap:
    """An unstarted initialization owned by this pytest session, never a server."""
    data: Path
    tools: tuple


@contextmanager
def postgres_bootstrap():
    """Initialize once; private copies retain independent roles, WAL and restart."""
    _, initdb, postgres = _postgres_tools()
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    root = _cluster_root(template=True)
    try:
        inputs = _bootstrap_inputs(initdb, postgres, environment)
        _initialize_data(root / 'data', initdb, environment)
        yield PostgresBootstrap(root / 'data', inputs)
    finally:
        if retire_cluster_root(root):
            shutil.rmtree(root)


@contextmanager
def owned_postgres_cluster(*, max_connections: int = 16, bootstrap: PostgresBootstrap | None = None):
    """Own one cluster with explicit test demand; the fixture retains 16 slots."""
    if type(max_connections) is not int or max_connections < 4:
        raise ValueError('A private PostgreSQL cluster needs at least four slots')
    psycopg, initdb, postgres = _postgres_tools()
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    root = _cluster_root()
    server = None
    try:
        data = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        if bootstrap is not None and bootstrap.tools == _bootstrap_inputs(initdb, postgres, environment):
            shutil.copytree(bootstrap.data, data)
        else:
            _initialize_data(data, initdb, environment)
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
                # The pytest/harness deadline bounds readiness. A shorter
                # fixture timer used to kill live starts under concurrent load.
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Private PostgreSQL exited during startup")
                    try:
                        with psycopg.connect(dsn):
                            return
                    except psycopg.OperationalError:
                        time.sleep(0.05)

            def restart_server():
                assert server is not None and server.poll() is None
                server.send_signal(signal.SIGINT)
                server.wait()
                start_server()

            start_server()
            yield RestartablePostgresDsn(dsn, restart_server)
    finally:
        # The test and any explicit restart have finished: these private files
        # will be deleted. SIGQUIT asks PostgreSQL itself to retire its children
        # and release IPC without a final checkpoint. Unlike SIGKILL, the
        # postmaster still owns that cleanup. Keep the exit barrier and exact
        # root/segment proof; interrupted waits retain evidence for reclaim.
        # https://www.postgresql.org/docs/current/server-shutdown.html
        if server is not None and server.poll() is None:
            server.send_signal(signal.SIGQUIT)
            server.wait()
        if retire_cluster_root(root):
            shutil.rmtree(root)


@pytest.fixture(scope='session')
def _postgres_bootstrap():
    with postgres_bootstrap() as bootstrap:
        yield bootstrap


@pytest.fixture
def postgres_cluster(_postgres_bootstrap):
    with owned_postgres_cluster(bootstrap=_postgres_bootstrap) as dsn:
        yield dsn
