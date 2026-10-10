"""TofuSQL gate — drives ``tofusql.query`` through a real ``tofu-db serve``.

Pins the storage.v2 wire path end to end: daemon dispatch of the
daemon-native operation, seeded-rows round trip with key-prefix pushdown,
fail-closed rejections (non-SELECT, multi-statement, unknown table, bad
payload), and the ``tofu-db sql`` operator CLI (SQL on stdin) including
owner isolation against one shared authority. Engine internals (statement
parser, snapshot isolation, entity read budget, key decoding) are pinned
by the Rust unit tests in ``tofusql.rs``.

Wire contract (current catalog revision): the request payload is
``{'query': sql}``; the response is ``{'rows': [{column: value}],
'rowCount': n, 'catalogVersion': ..., 'statementCeilings': {...}}`` with
rows as column-keyed objects and bodies surfaced as JSON text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.tofudb.test_tofudb_differential import (  # noqa: E402
    _OWNER_ID,
    _TENANT_ID,
    _TEST_SECRET,
    TofuDbDriver,
    _resolve_tofudb_binary,
)
from scripts.tofudb_v2_client import StorageV2Session  # noqa: E402

# serial: builds/spawns the native tofu-db engine (flock-serialized
# cargo build); queued behind the build, parallel-lane tests hit
# their 300s timeout — dedicated no-xdist phase per TESTING_STRATEGY.
pytestmark = [pytest.mark.unit, pytest.mark.serial]
class _Daemon:
    """One fresh-authority ``tofu-db serve`` with an explicit stop()."""

    def __init__(self, binary: Path):
        self.binary = binary
        self.data_dir = Path(tempfile.mkdtemp(prefix='tofudb-tofusql-authority-'))
        self._projection_parent = Path(tempfile.mkdtemp(
            prefix='tofudb-tofusql-projection-parent-',
        ))
        self._env = dict(os.environ)
        self._env['TOFU_STORAGE_TOKEN'] = _TEST_SECRET.decode('ascii')
        subprocess.run(
            [str(binary), 'init', '--data-dir', str(self.data_dir)],
            check=True, capture_output=True, text=True, timeout=120,
            env=self._env,
        )
        self._process = subprocess.Popen(
            [
                str(binary), 'serve',
                '--data-dir', str(self.data_dir),
                '--owner-id', str(_OWNER_ID),
                '--tenant-id', str(_TENANT_ID),
                '--search-projection-dir',
                str(self._projection_parent / 'projection'),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env,
            text=True,
        )
        readiness_line = self._process.stdout.readline().strip()
        if not readiness_line:
            raise RuntimeError('tofu-db serve exited before readiness')
        port = int(json.loads(readiness_line)['port'])
        self._session = StorageV2Session(
            '127.0.0.1', port, _TEST_SECRET,
            owner_id=_OWNER_ID, tenant_id=_TENANT_ID, timeout=15.0,
        )
        self.driver = TofuDbDriver(self._session)

    def stop(self) -> None:
        """EOF-supervise the daemon down, leaving the authority on disk."""
        self._session.close()
        if self._process.stdin:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)

    def cleanup(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self._projection_parent, ignore_errors=True)

    def sql_cli(self, owner_id: int, sql: str) -> dict:
        """Run ``tofu-db sql`` (statement on stdin) against the stopped authority."""
        result = subprocess.run(
            [
                str(self.binary), 'sql',
                '--data-dir', str(self.data_dir),
                '--tenant-id', str(_TENANT_ID),
                '--owner-id', str(owner_id),
            ],
            input=sql, capture_output=True, text=True, timeout=120,
            env=self._env,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


@pytest.fixture(scope='module')
def tofusql_daemon():
    daemon = _Daemon(_resolve_tofudb_binary())
    try:
        yield daemon
    finally:
        if daemon._process.poll() is None:
            daemon.stop()
        daemon.cleanup()


def _query(driver: TofuDbDriver, sql: str):
    return driver.execute('tofusql.query', {'query': sql}, None)


_RECORD_NS = 'tofusql_gate'


def _seed(driver: TofuDbDriver) -> None:
    for namespace, key in (
        (_RECORD_NS, 'alpha'),
        (_RECORD_NS, 'beta'),
        ('tofusql_other', 'gamma'),
    ):
        outcome = driver.execute('record.put', {
            'namespace': namespace, 'key': key, 'value': {'k': key},
        }, uuid.uuid4().hex)
        assert outcome.ok, outcome.error_message


def test_tofusql_query_round_trip_and_pushdown(tofusql_daemon: _Daemon) -> None:
    driver = tofusql_daemon.driver
    _seed(driver)

    pushed = _query(
        driver,
        "SELECT record_namespace, record_key, body FROM storage_records "
        f"WHERE record_namespace = '{_RECORD_NS}'",
    )
    assert pushed.ok, pushed.error_message
    rows = pushed.value['rows']
    assert [row['record_key'] for row in rows] == ['alpha', 'beta']
    assert all(row['record_namespace'] == _RECORD_NS for row in rows)
    # JSON text bodies surface as JSON text carrying the structured value.
    assert all(json.loads(row['body']) == {'k': row['record_key']}
               for row in rows)
    assert pushed.value['rowCount'] == 2

    scanned = _query(driver, 'SELECT count(*) AS n FROM storage_records')
    assert scanned.ok, scanned.error_message
    assert scanned.value['rows'] == [{'n': 3}]


def test_tofusql_fail_closed_rejections(tofusql_daemon: _Daemon) -> None:
    driver = tofusql_daemon.driver

    denied = _query(driver, 'DELETE FROM storage_records')
    assert not denied.ok

    # rusqlite prepares only the leading statement: a non-readonly tail is
    # never compiled or executed (effect-safety pinned by the surviving row
    # count; the read-only authorizer guards the leading statement itself).
    multi = _query(
        driver,
        'SELECT count(*) AS n FROM storage_records; DELETE FROM storage_records',
    )
    assert multi.ok, multi.error_message
    assert multi.value['rows'] == [{'n': 3}]  # the DELETE tail never ran

    unknown = _query(driver, 'SELECT record_key FROM definitely_not_a_table')
    assert not unknown.ok

    no_query_field = driver.execute(
        'tofusql.query', {'sql': 'SELECT record_key FROM storage_records'}, None,
    )
    assert not no_query_field.ok


def test_tofusql_cli_owner_isolation(tofusql_daemon: _Daemon) -> None:
    tofusql_daemon.stop()

    owner = tofusql_daemon.sql_cli(
        _OWNER_ID,
        f"SELECT record_key FROM storage_records "
        f"WHERE record_namespace = '{_RECORD_NS}'",
    )
    assert [row['record_key'] for row in owner['rows']] == ['alpha', 'beta']

    stranger = tofusql_daemon.sql_cli(
        _OWNER_ID + 1000, 'SELECT count(*) AS n FROM storage_records',
    )
    assert stranger['rows'] == [{'n': 0}]
