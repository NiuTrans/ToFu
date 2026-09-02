"""Pure contract tests for the fail-closed PostgreSQL→SQLite migrator."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _load():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate_pg_to_sqlite.py'
    spec = importlib.util.spec_from_file_location('tofu_pg_to_sqlite', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def migrator():
    return _load()


def test_live_pg_types_have_explicit_non_lossy_mapping(migrator):
    assert migrator._sqlite_type('int8') == 'INTEGER'
    assert migrator._sqlite_type('bool') == 'INTEGER'
    assert migrator._sqlite_type('float4') == 'REAL'
    assert migrator._sqlite_type('jsonb') == 'TEXT'
    assert migrator._sqlite_type('timestamptz') == 'TEXT'
    for lossy_or_unknown in ('money', 'numeric'):
        with pytest.raises(RuntimeError, match='unsupported PostgreSQL type'):
            migrator._sqlite_type(lossy_or_unknown)


def test_value_conversion_matches_sqlite_readback_types(migrator):
    assert migrator._convert_value(True, 'bool') == 1
    assert migrator._convert_value('42', 'int8') == 42
    assert migrator._convert_value('1.25', 'float8') == 1.25
    assert migrator._convert_value(memoryview(b'abc'), 'bytea') == b'abc'
    stamp = dt.datetime(2026, 8, 7, 12, 3, 4, tzinfo=dt.timezone.utc)
    assert migrator._convert_value(stamp, 'timestamptz') == stamp.isoformat(sep=' ')


def test_digest_is_order_independent_but_duplicate_sensitive(migrator):
    left = migrator.RowDigest()
    right = migrator.RowDigest()
    for row in [(1, 'a'), (2, 'b'), (1, 'a')]:
        left.add(row)
    for row in [(1, 'a'), (1, 'a'), (2, 'b')]:
        right.add(row)
    assert left.signature() == right.signature()

    missing_duplicate = migrator.RowDigest()
    for row in [(1, 'a'), (2, 'b')]:
        missing_duplicate.add(row)
    assert left.signature() != missing_duplicate.signature()


def test_archive_table_preserves_primary_key_and_portable_columns(
        migrator, tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / 'archive.db')
    columns = [
        {'name': 'id', 'udt_name': 'int8', 'nullable': False, 'pk': True},
        {'name': 'payload', 'udt_name': 'jsonb', 'nullable': False, 'pk': False},
        {'name': 'search_tsv', 'udt_name': 'tsvector', 'nullable': True, 'pk': False},
    ]
    migrator._create_archive_table(db, 'retired_plugin_rows', columns)
    info = db.execute('PRAGMA table_info(retired_plugin_rows)').fetchall()
    assert [row[1] for row in info] == ['id', 'payload']
    assert next(row for row in info if row[1] == 'id')[5] == 1
    db.execute('INSERT INTO retired_plugin_rows VALUES (?, ?)', (1, '{"x":1}'))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO retired_plugin_rows VALUES (?, ?)', (1, '{"x":2}'))


def test_target_path_is_never_overwritten(migrator, tmp_path):
    target = tmp_path / 'existing.db'
    target.write_bytes(b'owner data')
    with pytest.raises(FileExistsError, match='never overwrite'):
        migrator._initialize_target(target)
    assert target.read_bytes() == b'owner data'


def test_schema_bootstrap_runs_in_bounded_disposable_child(
        migrator, tmp_path, monkeypatch):
    import sqlite3

    target = tmp_path / 'fresh.db'

    def fake_run(argv, *, cwd, env, capture_output, text, timeout):
        assert argv[0] == migrator.sys.executable
        assert argv[1] == '-c'
        assert argv[3] == str(target)
        assert cwd == str(migrator._PROJECT_ROOT)
        assert capture_output is True and text is True and timeout == 600
        assert env['TOFU_DEPLOYMENT_MODE'] == 'personal'
        assert env['TOFU_PROCESS_ROLE'] == 'all'
        assert 'TOFU_DB_BACKEND' not in env
        assert 'TOFU_REQUIRE_PG' not in env
        assert 'TOFU_POSTGRES_DSN_FILE' not in env
        assert not any(name.startswith('TOFU_PG_') for name in env)
        for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                     'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
                     'ORT_NUM_THREADS', 'TOFU_ONNX_THREADS'):
            assert env[name] == '1'
        db = sqlite3.connect(target)
        db.execute('CREATE TABLE schema_meta (version INTEGER)')
        db.commit()
        db.close()
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(migrator.subprocess, 'run', fake_run)
    migrator._initialize_target(target)
    assert target.is_file()


def test_postgres_authority_requires_bounded_tls_secret_file(
        migrator, tmp_path):
    relative = Path('postgres.dsn')
    with pytest.raises(RuntimeError, match='absolute path'):
        migrator._read_dsn_secret(relative)

    insecure = tmp_path / 'insecure.dsn'
    insecure.write_text(
        'postgresql://db.example/tofu?sslmode=require', encoding='utf-8')
    with pytest.raises(RuntimeError, match='sslmode=verify-full'):
        migrator._read_dsn_secret(insecure)

    secure = tmp_path / 'secure.dsn'
    secure.write_text(
        'postgresql://db.example/tofu?sslmode=verify-full', encoding='utf-8')
    assert migrator._read_dsn_secret(secure).endswith('sslmode=verify-full')


def test_cli_does_not_accept_retired_or_plaintext_postgres_configuration(
        migrator):
    help_text = migrator._parser().format_help()
    assert '--postgres-dsn-file' in help_text
    assert '--source-dsn' not in help_text
    assert 'TOFU_PG_' not in help_text


def test_cli_authority_artifacts_cannot_escape_project_data(migrator):
    allowed = migrator._PROJECT_ROOT / 'data' / 'candidate.db'
    migrator._require_project_data_path(allowed, 'target')
    with pytest.raises(ValueError, match='project data directory'):
        migrator._require_project_data_path(Path('/tmp/tofu.db'), 'target')


def test_migration_lock_refuses_a_second_copy(migrator, tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(migrator, '_PROJECT_ROOT', tmp_path)
    with migrator._migration_lock('data/first.db'):
        metadata = json.loads(
            (tmp_path / 'data' / '.pg_to_sqlite_migration.lock').read_text())
        assert metadata['pid'] > 0
        assert metadata['target'] == 'data/first.db'
        with pytest.raises(RuntimeError, match='already running'):
            with migrator._migration_lock('data/second.db'):
                pytest.fail('second migration unexpectedly acquired lock')


def test_bulk_load_suspends_only_rebuildable_secondary_indexes(
        migrator, tmp_path, monkeypatch):
    import sqlite3
    db = sqlite3.connect(tmp_path / 'indexes.db')
    db.executescript("""
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            stable_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            score INTEGER NOT NULL
        );
        CREATE INDEX idx_items_category ON items(category);
        CREATE INDEX idx_items_score_partial ON items(score) WHERE score > 0;
    """)
    before = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'sqlite_autoindex_items_1' in before

    suspended = migrator._suspend_nonunique_indexes(db)
    during = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert during == {'sqlite_autoindex_items_1'}

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.commits = 0
            self.checkpoints = 0
            self.shrinks = 0

        def execute(self, sql):
            if sql == 'PRAGMA wal_checkpoint(TRUNCATE)':
                self.checkpoints += 1
            if sql == 'PRAGMA shrink_memory':
                self.shrinks += 1
            return self.connection.execute(sql)

        def commit(self):
            self.commits += 1
            self.connection.commit()

    tracked = TrackedConnection(db)
    trims = []
    monkeypatch.setattr(migrator, '_trim_process_heap',
                        lambda: trims.append(True))
    migrator._restore_indexes(tracked, suspended)
    assert tracked.commits == len(suspended)
    assert tracked.checkpoints == len(suspended)
    assert tracked.shrinks == len(suspended)
    assert len(trims) == len(suspended)
    after = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert after == before


@pytest.mark.parametrize(('relation_bytes', 'rows', 'expected'), [
    (10 * 1024 * 1024, 10_000, 10_000),       # ~1 KiB rows
    (2 * 1024 * 1024 * 1024, 20_000, 64),    # ~100 KiB toasted rows
    (4 * 1024 * 1024 * 1024, 4_000, 8),      # ~1 MiB toasted rows
])
def test_fetch_batch_caps_wide_toasted_relations(
        migrator, relation_bytes, rows, expected):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, *_args): pass
        def fetchone(self): return relation_bytes, rows

    class Source:
        def cursor(self): return Cursor()

    got = migrator._recommended_fetch_rows(
        Source(), 'wide_table', requested=10_000,
        batch_bytes=64 * 1024 * 1024)
    assert got == expected


@pytest.mark.parametrize(('stored_bytes', 'rows', 'expected'), [
    (10 * 1024 * 1024, 10_000, 10_000),
    (2 * 1024 * 1024 * 1024, 20_000, 64),
    (4 * 1024 * 1024 * 1024, 4_000, 8),
])
def test_target_digest_batch_uses_materialized_sqlite_size(
        migrator, stored_bytes, rows, expected):
    class Result:
        def fetchone(self): return (stored_bytes,)

    class Target:
        def execute(self, sql, params):
            assert 'dbstat' in sql
            assert params == ('wide_table',)
            return Result()

    got = migrator._recommended_target_fetch_rows(
        Target(), 'wide_table', expected_rows=rows, requested=10_000,
        batch_bytes=64 * 1024 * 1024)
    assert got == expected


def test_target_digest_batch_fails_closed_when_dbstat_unavailable(migrator):
    class Target:
        def execute(self, *_args):
            raise RuntimeError('dbstat disabled')

    assert migrator._recommended_target_fetch_rows(
        Target(), 'wide_table', expected_rows=1, requested=10_000,
        batch_bytes=64 * 1024 * 1024) == 64


def test_consumed_source_snapshot_cleanup_cannot_mask_verified_copy(migrator):
    class RestartedSource:
        closed = False

        def rollback(self):
            raise RuntimeError('server closed the connection')

        def close(self):
            self.closed = True

    source = RestartedSource()
    assert migrator._release_source_snapshot(source) == (
        'already_disconnected:RuntimeError')
    assert source.closed is True


def test_online_end_probe_outage_is_recorded_but_cutover_probe_fails(
        migrator, monkeypatch):
    def unavailable(*_args, **kwargs):
        assert kwargs['exclude_pids'] == ()
        raise RuntimeError('server unavailable\nsecret detail not retained')

    monkeypatch.setattr(migrator, '_probe_source_quiescence', unavailable)
    state = migrator._probe_source_quiescence_at_end(
        'redacted', required=False)
    assert state['default_transaction_read_only'] is None
    assert state['other_client_sessions'] is None
    assert state['probe_error'] == 'RuntimeError: server unavailable'

    with pytest.raises(RuntimeError, match='server unavailable'):
        migrator._probe_source_quiescence_at_end(
            'redacted', required=True)


def test_closed_candidate_removes_derived_sidecars_only_with_empty_wal(
        migrator, tmp_path):
    target = tmp_path / 'candidate.db'
    target.write_bytes(b'SQLite candidate placeholder')
    wal = Path(str(target) + '-wal')
    shm = Path(str(target) + '-shm')
    wal.write_bytes(b'')
    shm.write_bytes(b'derived shared-memory state')

    assert set(migrator._remove_closed_sqlite_sidecars(target)) == {
        wal.name, shm.name}
    assert not wal.exists() and not shm.exists()

    wal.write_bytes(b'committed pages must not be discarded')
    shm.write_bytes(b'derived state')
    with pytest.raises(RuntimeError, match='non-empty WAL'):
        migrator._remove_closed_sqlite_sidecars(target)
    assert wal.exists() and shm.exists()


def test_cross_reopen_verifies_each_tables_own_expected_digest(
        migrator, tmp_path):
    import sqlite3

    target = tmp_path / 'reopen.db'
    db = sqlite3.connect(target)
    db.executescript('CREATE TABLE first (id INTEGER, value TEXT);'
                     'CREATE TABLE second (id INTEGER, value TEXT);')
    rows = {
        'first': [(1, 'a')],
        'second': [(2, 'b'), (3, 'c')],
    }
    report = {'tables': {}}
    columns = {}
    for table, values in rows.items():
        db.executemany(f'INSERT INTO {table} VALUES (?, ?)', values)
        digest = migrator.RowDigest()
        for row in values:
            digest.add(row)
        report['tables'][table] = {'source': digest.as_dict()}
        columns[table] = [{'name': 'id'}, {'name': 'value'}]
    db.commit()
    db.close()

    assert migrator._verify_reopened_target(
        target, columns, report, 64 * 1024 * 1024) == 'ok'


def test_cross_reopen_mismatch_fails_closed(migrator, tmp_path):
    import sqlite3

    target = tmp_path / 'mismatch.db'
    db = sqlite3.connect(target)
    db.execute('CREATE TABLE items (id INTEGER)')
    db.execute('INSERT INTO items VALUES (1)')
    db.commit()
    db.close()

    wrong = migrator.RowDigest()
    wrong.add((2,))
    report = {'tables': {'items': {'source': wrong.as_dict()}}}
    with pytest.raises(RuntimeError, match='cross-reopen verification failed'):
        migrator._verify_reopened_target(
            target, {'items': [{'name': 'id'}]}, report, 1024 * 1024)


def test_live_snapshot_parity_is_not_a_cutover_attestation(migrator):
    status, ready, reason = migrator._migration_verdict(
        is_full=True, source_quiesced=False,
        read_only_at_start=False, read_only_at_end=False)
    assert status == 'snapshot_verified'
    assert ready is False
    assert reason == 'source_writes_were_not_declared_quiesced'


def test_cutover_attestation_requires_server_read_only_at_both_ends(migrator):
    with pytest.raises(RuntimeError, match='default_transaction_read_only=on'):
        migrator._migration_verdict(
            is_full=True, source_quiesced=True,
            read_only_at_start=True, read_only_at_end=False)

    assert migrator._migration_verdict(
        is_full=True, source_quiesced=True,
        read_only_at_start=True, read_only_at_end=True) == (
            'verified', True,
            'source_quiesced_and_server_default_read_only')

    with pytest.raises(RuntimeError, match='zero other PostgreSQL client'):
        migrator._migration_verdict(
            is_full=True, source_quiesced=True,
            read_only_at_start=True, read_only_at_end=True,
            peer_sessions_at_start=1, peer_sessions_at_end=0)


def test_partial_copy_can_never_be_cutover_ready(migrator):
    assert migrator._migration_verdict(
        is_full=False, source_quiesced=True,
        read_only_at_start=True, read_only_at_end=True) == (
            'partial_verified', False, 'only_selected_tables_were_copied')
