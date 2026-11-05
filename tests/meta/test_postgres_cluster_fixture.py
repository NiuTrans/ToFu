"""Private PostgreSQL clusters: diagnostics, owner-bound lifetime, exact reclaim."""

from pathlib import Path
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import time
from types import ModuleType

import pytest


pytestmark = pytest.mark.unit

_CHECKOUT = Path(__file__).resolve().parents[2]
_linux_only = pytest.mark.skipif(
    not sys.platform.startswith('linux'),
    reason='owner binding and segment proofs use PR_SET_PDEATHSIG and /proc')
_OWNER = (
    'import sys\n'
    'from tests.support.postgres_cluster import owned_postgres_cluster\n'
    'with owned_postgres_cluster() as dsn:\n'
    '    print(dsn, flush=True)\n'
    '    sys.stdin.read()\n'
)


@pytest.mark.skipif(os.name != 'posix', reason='Private PostgreSQL fixture is POSIX-only')
@pytest.mark.parametrize(('stderr', 'stdout', 'expected'), [
    ('synthetic initdb refusal', 'bootstrap started', 'synthetic initdb refusal'),
    ('', 'synthetic bootstrap refusal', 'synthetic bootstrap refusal'),
])
def test_initdb_failure_exposes_diagnostic_and_removes_private_directory(
        monkeypatch, stderr, stdout, expected):
    from tests.support import postgres_cluster

    directories = []

    def fail(command, **kwargs):
        directory = Path(command[command.index('-D') + 1])
        directory.mkdir()
        directories.append(directory)
        raise subprocess.CalledProcessError(
            1, command, output=stdout, stderr=stderr)

    monkeypatch.setattr(postgres_cluster.os, 'getuid', lambda: 1000)
    monkeypatch.setitem(sys.modules, 'psycopg', ModuleType('psycopg'))
    monkeypatch.setattr(postgres_cluster.shutil, 'which', lambda name: '/test/' + name)
    monkeypatch.setattr(postgres_cluster.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match=expected) as raised:
        with postgres_cluster.owned_postgres_cluster():
            pytest.fail('failed cluster must not yield')
    assert isinstance(raised.value.__cause__, subprocess.CalledProcessError)
    assert directories and not directories[0].parent.exists()


def _require_cluster_binaries():
    if os.getuid() == 0:
        pytest.skip('Local PostgreSQL tests need an ordinary POSIX user')
    pytest.importorskip('psycopg')
    if not shutil.which('initdb') or not shutil.which('postgres'):
        pytest.skip('Install PostgreSQL test binaries to exercise cluster lifetime')


def _segment(shmid):
    """The kernel's row for one SysV segment, or None once it is removed."""
    with open('/proc/sysvipc/shm', encoding='ascii') as table:
        header = table.readline().split()
        for line in table:
            row = dict(zip(header, map(int, line.split())))
            if row['shmid'] == shmid:
                return row
    return None


def _cluster_root(dsn):
    from psycopg.conninfo import conninfo_to_dict

    return Path(conninfo_to_dict(dsn)['host']).parent


def _postmaster_record(root):
    """(postmaster PID, SysV segment id) recorded in the cluster's postmaster.pid."""
    lines = (root / 'data' / 'postmaster.pid').read_text().splitlines()
    return int(lines[0]), int(lines[6].split()[1])


def _await_exit(pid):
    """Return once ``pid`` has exited (a zombie has); the hang guard bounds it."""
    while True:
        try:
            stat = Path(f'/proc/{pid}/stat').read_text()
        except FileNotFoundError:
            return
        if stat.rpartition(')')[2].split()[0] in ('Z', 'X'):
            return
        time.sleep(0.01)


def _await_detached(shmid):
    while (row := _segment(shmid)) is not None and row['nattch']:
        time.sleep(0.01)


def _start_fixture_without_cluster(monkeypatch):
    """Run one fixture start whose own initdb is refused, so it adds no cluster."""
    from tests.support import postgres_cluster

    def refuse(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr='synthetic refusal')

    with monkeypatch.context() as patch:
        patch.setattr(postgres_cluster.subprocess, 'run', refuse)
        with pytest.raises(RuntimeError, match='synthetic refusal'):
            with postgres_cluster.owned_postgres_cluster():
                pytest.fail('a refused cluster must not yield')


@pytest.fixture
def cluster_owner(tmp_path):
    """A separate owner process holding one private cluster until it is killed."""
    _require_cluster_binaries()
    diagnostics = tmp_path / 'owner.log'
    with diagnostics.open('w') as log:
        owner = subprocess.Popen(
            [sys.executable, '-c', _OWNER], cwd=_CHECKOUT,
            env={**os.environ, 'PYTHONPATH': str(_CHECKOUT)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            dsn = owner.stdout.readline().strip()
            assert dsn, diagnostics.read_text()[-4000:]
            yield owner, _cluster_root(dsn)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait()
            owner.stdin.close()
            owner.stdout.close()


@_linux_only
def test_teardown_stops_the_cluster_and_removes_its_segment_and_root():
    from tests.conftest import _PYTEST_ROOT_PATTERN
    from tests.support.postgres_cluster import owned_postgres_cluster

    _require_cluster_binaries()
    import psycopg

    with owned_postgres_cluster() as dsn:
        root = _cluster_root(dsn)
        _, shmid = _postmaster_record(root)
        owner = _PYTEST_ROOT_PATTERN.fullmatch(root.name)
        assert owner and owner['kind'] == 'postgres'
        assert int(owner['pid']) == os.getpid()
        assert _segment(shmid)['nattch'] > 0
        with psycopg.connect(dsn) as connection:
            setting = connection.execute('SHOW dynamic_shared_memory_type').fetchone()
        assert setting == ('mmap',)
        assert list((root / 'data' / 'pg_dynshmem').glob('mmap.*'))
    assert not root.exists()
    assert _segment(shmid) is None


@_linux_only
def test_sigkilled_owner_stops_its_cluster_and_the_next_fixture_removes_its_root(
        cluster_owner, monkeypatch):
    owner, root = cluster_owner
    postmaster, shmid = _postmaster_record(root)
    assert _segment(shmid)['nattch'] > 0

    owner.kill()
    owner.wait()
    # Nothing but the kernel's parent-death signal tells the postmaster.
    _await_exit(postmaster)
    assert _segment(shmid) is None

    _start_fixture_without_cluster(monkeypatch)
    assert not root.exists()


@_linux_only
def test_fixture_start_removes_the_recorded_segment_of_a_killed_cluster(
        cluster_owner, monkeypatch):
    owner, root = cluster_owner
    postmaster, shmid = _postmaster_record(root)
    # SIGKILL skips the postmaster's own segment removal; its children exit on
    # postmaster death. The live owner keeps every reclaim away meanwhile.
    os.kill(postmaster, signal.SIGKILL)
    _await_exit(postmaster)
    _await_detached(shmid)
    assert _segment(shmid)['nattch'] == 0 and root.is_dir()
    owner.kill()
    owner.wait()

    _start_fixture_without_cluster(monkeypatch)
    assert _segment(shmid) is None
    assert not root.exists()


@_linux_only
def test_reclaim_removes_only_recorded_segments_it_can_prove(tmp_path, monkeypatch):
    import tests.conftest as packaged

    libc = ctypes.CDLL(None, use_errno=True)
    libc.shmget.argtypes = (ctypes.c_int, ctypes.c_size_t, ctypes.c_int)
    libc.shmat.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    libc.shmat.restype = ctypes.c_void_p
    libc.shmdt.argtypes = (ctypes.c_void_p,)
    created, attachments, sleepers = [], [], []

    def segment():
        # IPC_PRIVATE (key 0), IPC_CREAT | 0600, PostgreSQL's interlock size.
        shmid = libc.shmget(0, 56, 0o1600)
        assert shmid >= 0, os.strerror(ctypes.get_errno())
        created.append(shmid)
        return shmid

    def leftover(name, *, pid=None, record=''):
        data = tmp_path / name / 'data'
        data.mkdir(parents=True)
        (data / 'postmaster.pid').write_text(
            f'{pid or os.getpid()}\n{data}\n1\n5432\n{tmp_path}\n\n{record}\nready   \n')
        return data.parent

    try:
        leaked, rekeyed, recreated, attached, owned = (segment() for _ in range(5))
        attachments.append(libc.shmat(attached, None, 0))
        assert attachments[0] not in (None, ctypes.c_void_p(-1).value)
        proven = leftover('tofu-test-postgres-pid-43-proven', record=f'0 {leaked}')
        foreign_key = leftover('tofu-test-postgres-pid-44-rekeyed', record=f'1 {rekeyed}')
        foreign_creator = leftover(
            'tofu-test-postgres-pid-45-recreated', pid=1, record=f'0 {recreated}')
        busy = leftover('tofu-test-postgres-pid-46-busy', record=f'0 {attached}')
        live = leftover('tofu-test-postgres-pid-42-live', record=f'0 {owned}')
        serving = tmp_path / 'tofu-test-postgres-pid-47-serving' / 'data'
        serving.mkdir(parents=True)
        sleepers.append(subprocess.Popen(['sleep', '600'], cwd=serving))
        (serving / 'postmaster.pid').write_text(f'{sleepers[0].pid}\n{serving}\n')
        monkeypatch.setattr(packaged, '_pytest_root_owner_is_alive', lambda pid: pid == 42)

        first = packaged._reclaim_stale_pytest_roots(tmp_path, current_pid=42)

        assert sorted(first['removed']) == sorted(
            [proven.name, foreign_key.name, foreign_creator.name])
        assert first['errors'] == []
        assert _segment(leaked) is None
        assert all(_segment(shmid) is not None for shmid in (rekeyed, recreated, owned))
        assert _segment(attached)['nattch'] == 1 and busy.is_dir() and live.is_dir()
        # The serving process was asked for a fast shutdown; its root waits for it.
        assert sleepers[0].wait() == -signal.SIGINT
        assert serving.is_dir()

        second = packaged._reclaim_stale_pytest_roots(tmp_path, current_pid=42)
        assert second['removed'] == [serving.parent.name]
        assert busy.is_dir() and live.is_dir()
    finally:
        for sleeper in sleepers:
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait()
        for address in attachments:
            libc.shmdt(address)
        for shmid in created:
            libc.shmctl(shmid, 0, None)
