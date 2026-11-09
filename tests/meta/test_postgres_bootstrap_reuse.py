"""Reuse pristine initialization while every test retains its own real cluster."""
from contextlib import ExitStack
from pathlib import Path
import shutil
import signal
import subprocess
import os
import sys

import pytest

pytestmark = pytest.mark.api


@pytest.fixture
def clusters():
    # Collection registers plugins before fixture execution; importing earlier
    # prevents pytest from rewriting a later consumer's cluster plugin.
    from tests.support import postgres_cluster
    return postgres_cluster


def test_root_reclaim_does_not_load_cluster_fixture_machinery(tmp_path):
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, '-c',
        'import sys\nfrom pathlib import Path\nfrom tests import conftest\n'
        'assert conftest._retire_pytest_root("postgres", Path(sys.argv[1]))\n'
        'assert "tests.support.postgres_cluster" not in sys.modules\n', str(tmp_path)],
        cwd=root, env={**os.environ, 'PYTHONPATH': str(root)}, text=True, capture_output=True)
    assert child.returncode == 0, child.stderr


def test_pristine_bootstrap_is_built_once_and_clusters_remain_independent(monkeypatch, clusters):
    pytest.importorskip('psycopg')
    import psycopg

    initialized = []
    run = subprocess.run

    def observed(command, **kwargs):
        if Path(command[0]).name == 'initdb':
            initialized.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(clusters.subprocess, 'run', observed)
    with clusters.postgres_bootstrap() as bootstrap:
        with ExitStack() as owned:
            first = owned.enter_context(clusters.owned_postgres_cluster(bootstrap=bootstrap))
            second = owned.enter_context(clusters.owned_postgres_cluster(bootstrap=bootstrap))
            assert first != second
            with psycopg.connect(first, autocommit=True) as connection:
                connection.execute('CREATE TABLE private_values (value integer)')
                connection.execute('INSERT INTO private_values VALUES (42)')
                connection.execute('CREATE ROLE private_role')
                roots = [Path(connection.execute('SHOW data_directory').fetchone()[0]).parent]
                for setting in ('fsync', 'full_page_writes', 'synchronous_commit'):
                    assert connection.execute('SHOW ' + setting).fetchone() == ('on',)
            first.restart()
            with psycopg.connect(first) as connection:
                assert connection.execute('SELECT value FROM private_values').fetchone() == (42,)
            with psycopg.connect(second) as connection:
                assert connection.execute("SELECT to_regclass('private_values')").fetchone() == (None,)
                assert connection.execute("SELECT count(*) FROM pg_roles WHERE rolname='private_role'").fetchone() == (0,)
                roots.append(Path(connection.execute('SHOW data_directory').fetchone()[0]).parent)
        assert all(not root.exists() for root in roots)
        assert bootstrap.data.is_dir()
    assert not bootstrap.data.parent.exists()
    assert len(initialized) == 1


def test_failed_clone_removes_only_its_destination_and_preserves_bootstrap(monkeypatch, clusters):
    destinations = []

    def fail(source, destination, **kwargs):
        destination = Path(destination)
        destinations.append(destination.parent)
        destination.mkdir()
        raise OSError('injected clone failure')

    with clusters.postgres_bootstrap() as bootstrap:
        with monkeypatch.context() as patch:
            patch.setattr(shutil, 'copytree', fail)
            with pytest.raises(OSError, match='injected clone failure'):
                with clusters.owned_postgres_cluster(bootstrap=bootstrap):
                    pytest.fail('a partial clone must not start')
        assert destinations and all(not root.exists() for root in destinations)
        assert bootstrap.data.is_dir()
        with clusters.owned_postgres_cluster(bootstrap=bootstrap) as dsn:
            assert dsn


def test_new_binary_identity_requires_fresh_initialization(monkeypatch, clusters):
    from dataclasses import replace

    initialized = []
    run = subprocess.run

    def observed(command, **kwargs):
        if Path(command[0]).name == 'initdb':
            initialized.append(command)
        return run(command, **kwargs)

    with clusters.postgres_bootstrap() as bootstrap:
        stale = replace(bootstrap, tools=())
        monkeypatch.setattr(clusters.subprocess, 'run', observed)
        with clusters.owned_postgres_cluster(bootstrap=stale) as dsn:
            assert dsn
    assert len(initialized) == 1


def test_binary_change_during_bootstrap_does_not_relabel_old_data(monkeypatch, clusters):
    psycopg = pytest.importorskip('psycopg')
    initialize = clusters._initialize_data
    identity = ['before-build']
    initialized = []

    def replace_binary_after_initialization(*args):
        initialized.append(identity[0])
        initialize(*args)
        identity[0] = 'after-build'

    monkeypatch.setattr(clusters, '_bootstrap_inputs', lambda *_: tuple(identity))
    monkeypatch.setattr(clusters, '_initialize_data', replace_binary_after_initialization)
    with clusters.postgres_bootstrap() as bootstrap:
        with clusters.owned_postgres_cluster(bootstrap=bootstrap) as dsn:
            with psycopg.connect(dsn) as connection:
                assert connection.execute('SELECT 1').fetchone() == (1,)
    assert initialized == ['before-build', 'after-build']


def test_live_startup_can_finish_after_the_old_inner_deadline(monkeypatch, clusters):
    from types import SimpleNamespace
    import time

    psycopg = pytest.importorskip('psycopg')
    connect = psycopg.connect
    elapsed = [0]

    def delayed_first_probe(*args, **kwargs):
        if not elapsed[0]:
            elapsed[0] = 30
            raise psycopg.OperationalError('the database system is starting up')
        return connect(*args, **kwargs)

    monkeypatch.setattr(clusters, 'time', SimpleNamespace(
        monotonic=lambda: time.monotonic() + elapsed[0], sleep=time.sleep))
    monkeypatch.setattr(psycopg, 'connect', delayed_first_probe)
    with clusters.owned_postgres_cluster() as dsn:
        with psycopg.connect(dsn) as connection:
            assert connection.execute('SELECT 1').fetchone() == (1,)


def test_restart_flushes_but_final_disposal_retires_without_a_checkpoint(monkeypatch, clusters):
    psycopg = pytest.importorskip('psycopg')

    roots = set()
    requests = []
    send = subprocess.Popen.send_signal

    def observe(process, requested):
        if any(str(root / 'data') in process.args for root in roots):
            requests.append(requested)
        return send(process, requested)

    monkeypatch.setattr(clusters.subprocess.Popen, 'send_signal', observe)
    with clusters.owned_postgres_cluster() as dsn:
        with psycopg.connect(dsn) as connection:
            root = Path(connection.execute('SHOW data_directory').fetchone()[0]).parent
            roots.add(root)
            connection.execute('CREATE TABLE durable_value (value integer)')
            connection.execute('INSERT INTO durable_value VALUES (42)')
        dsn.restart()
        with psycopg.connect(dsn) as connection:
            assert connection.execute('SELECT value FROM durable_value').fetchone() == (42,)
    assert requests == [signal.SIGINT, signal.SIGQUIT]
    assert not root.exists()
