"""Static guard for the new process-isolated storage boundary."""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/business.py', 'lib/service.py', 'scripts/offline_migration.py',
    'lib/untracked_driver.py',
}

import ast
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
DRIVERS = {'sqlite3', 'psycopg', 'psycopg2'}
OFFLINE_DRIVER_IMPORT_ALLOWLIST = {
    # Offline operator tools run while the server/sidecar is stopped; routing
    # them through StorageClient is impossible by design.
    'scripts/migrate_pg_to_sqlite.py',
    'scripts/migrate_sqlite_to_postgres.py',
    'scripts/storage_deep_clean.py',
    # Offline measurement harness: the rawsqlite engine-floor baseline opens
    # only its own disposable tempfile databases, never a live authority.
    'scripts/tofudb_perf_compare.py',
}
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('indexed', [False, True])
@pytest.mark.parametrize('lexical_prefilter', [False, True])
def test_runtime_boundary_has_same_scope_with_or_without_git_and_rg(
        tmp_path, monkeypatch, indexed, lexical_prefilter):
    from lib.storage import storage_boundary

    if indexed:
        subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    for relative in ('lib/business.py', 'scripts/offline_migration.py'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import sqlite3\nconn = sqlite3.connect('offline.db')\n")
    monkeypatch.setattr(storage_boundary, '_candidate_python_files',
        lambda root: {'lib/business.py'} if lexical_prefilter else None)

    report = storage_boundary.boundary_report(tmp_path)
    assert report['files'] == ['lib/business.py']
    assert not report['ready']
    # Removing the real application violation admits the Web boundary while
    # the separate strict inventory continues to report the offline tool.
    (tmp_path / 'lib/business.py').unlink()
    assert storage_boundary.boundary_report(tmp_path)['ready']
    assert 'scripts/offline_migration.py' in storage_boundary.strict_inventory(tmp_path)['files']


def test_exclusive_boundary_scanner_rejects_business_sql_but_exempts_sidecar(
        tmp_path):
    from lib.storage.storage_boundary import boundary_report

    business = tmp_path / 'lib' / 'business.py'
    business.parent.mkdir(parents=True)
    business.write_text(
        "import sqlite3\n"
        "conn = sqlite3.connect('x')\n"
        "cursor = conn.cursor()\n"
        "conn.execute('SELECT 1')\n"
        "conn.commit()\n",
        encoding='utf-8')
    adapter = tmp_path / 'lib' / 'storage_authority_sql' / 'adapter.py'
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        "import sqlite3\nconn = sqlite3.connect('x')\n", encoding='utf-8')

    report = boundary_report(tmp_path)
    assert report['ready'] is False
    assert report['files'] == ['lib/business.py']
    assert {item['capability'] for item in report['violations']} == {
        'database_driver_import', 'direct_connect', 'direct_cursor',
        'direct_execute', 'direct_commit',
    }


def test_transaction_method_names_on_domain_services_are_not_database_owners(
        tmp_path):
    from lib.storage.storage_boundary import boundary_report

    service = tmp_path / 'routes' / 'sync.py'
    service.parent.mkdir(parents=True)
    service.write_text(
        "class SyncService:\n"
        "    def cursor(self, conversation_id, user_id, sequence):\n"
        "        return f'{conversation_id}:{user_id}:{sequence}'\n"
        "\n"
        "service = SyncService()\n"
        "value = service.cursor('conversation', 1, 2)\n",
        encoding='utf-8',
    )

    assert boundary_report(tmp_path) == {
        'ready': True,
        'violation_count': 0,
        'file_count': 0,
        'files': [],
        'violations': [],
    }


def test_transaction_methods_on_database_shaped_receivers_remain_blocked(
        tmp_path):
    from lib.storage.storage_boundary import boundary_report

    business = tmp_path / 'routes' / 'business.py'
    business.parent.mkdir(parents=True)
    business.write_text(
        "def write(db_connection):\n"
        "    db_cursor = db_connection.cursor()\n"
        "    db_connection.commit()\n"
        "    return db_cursor\n",
        encoding='utf-8',
    )

    report = boundary_report(tmp_path)
    assert report['ready'] is False
    assert {item['capability'] for item in report['violations']} == {
        'direct_cursor', 'direct_commit',
    }


@pytest.mark.parametrize('source,blocked', [
    ("class UploadBudget:\n    def request(self):\n        self.connect()\n", False),
    ("transport.connect()\n", False),
    ("db_connection.connect()\n", True),
    ("self.connection.connect()\n", True),
    ("factory.connect('database-path')\n", True),
    ("factory.connect(database='database-path')\n", True),
    ("import sqlite3 as driver\nservice.connect()\n", True),
    ("service.execute('SELECT 1')\n", True),
])
def test_connect_methods_require_database_evidence(tmp_path, source, blocked):
    from lib.storage.storage_boundary import boundary_report

    service = tmp_path / 'lib' / 'service.py'
    service.parent.mkdir(parents=True)
    service.write_text(source, encoding='utf-8')
    report = boundary_report(tmp_path)
    assert report['ready'] is not blocked
    if blocked:
        assert report['violation_count'] > 0


def test_connect_policy_invalidates_cached_legacy_false_positive(tmp_path):
    from lib.storage.storage_boundary import boundary_report

    service = tmp_path / 'lib' / 'service.py'
    service.parent.mkdir(parents=True)
    service.write_text('transport.connect()\n', encoding='utf-8')
    stat = service.stat()
    cache = tmp_path / 'data' / '.tofu-storage-boundary-cache.json'
    cache.parent.mkdir()
    cache.write_text(json.dumps({'version': 22, 'files': {
        'lib/service.py': {
            'signature': {'mtime_ns': stat.st_mtime_ns,
                          'ctime_ns': stat.st_ctime_ns, 'size': stat.st_size},
            'violations': [{'path': 'lib/service.py', 'line': 1,
                            'capability': 'direct_connect'}],
        },
    }}), encoding='utf-8')
    assert boundary_report(tmp_path)['ready'] is True


def test_real_repo_passes_exclusive_sidecar_boundary():
    """The startup gate must never be the first place a violation surfaces.

    ``require_exclusive_sidecar_boundary`` runs once at server startup; a
    refactor that removes a sidecar fence (or adds a direct DB capability)
    previously failed only there.  Scan the real checkout here so a normal
    test run catches it seconds after the edit instead.  The scanner caches
    per-file results by content signature, so the warm path is fast.
    """
    from lib.storage.storage_boundary import boundary_report

    report = boundary_report(ROOT)
    assert report['ready'], (
        'production files still own direct storage capabilities:\n'
        + json.dumps(report['violations'][:20], indent=1))


def test_exclusive_boundary_scanner_reuses_unchanged_file_results(
        tmp_path, monkeypatch):
    import lib.storage.storage_boundary as boundary

    source = tmp_path / 'lib' / 'business.py'
    source.parent.mkdir(parents=True)
    source.write_text("note = 'execute'\n", encoding='utf-8')
    original = boundary._scan_python_file
    scanned = []

    def counting_scan(path, relative):
        scanned.append(relative)
        return original(path, relative)

    monkeypatch.setattr(boundary, '_scan_python_file', counting_scan)
    boundary.scan_storage_boundary(tmp_path)
    first_count = len(scanned)
    boundary.scan_storage_boundary(tmp_path)
    assert first_count == 1
    assert len(scanned) == first_count

    source.write_text("note = 'executemany'\n", encoding='utf-8')
    boundary.scan_storage_boundary(tmp_path)
    assert len(scanned) == first_count + 1


def _driver_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                alias.name for alias in node.names
                if alias.name.split('.')[0] in DRIVERS)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if module.split('.')[0] in DRIVERS:
                found.append(module)
    return found


def _matching_files(pattern: str) -> list[Path]:
    # This ratchet inspects tracked sources, excluding unrelated untracked
    # operator scripts in shared checkouts. Archives without Git inspect their
    # available sources. The separate runtime gate also covers untracked code.
    roots = ['lib', 'routes', 'scripts', 'server.py']
    try:
        tracked = subprocess.run(
            ['git', 'ls-files', '-z', '--', *roots], cwd=ROOT, text=True,
            capture_output=True, check=True, timeout=10)
        candidates = [ROOT / relative for relative in tracked.stdout.split('\0')
                      if relative]
    except (OSError, subprocess.SubprocessError):
        # Source archives still run the guard. Missing optional search binaries
        # must not turn a real boundary assertion into a skipped/failed check.
        candidates = []
        for root in (ROOT / name for name in roots):
            if root.is_file():
                candidates.append(root)
            elif root.is_dir():
                for directory, names, files in os.walk(root):
                    names[:] = [name for name in names if not name.startswith('.')
                                and name not in {'__pycache__', 'node_modules'}]
                    candidates.extend(Path(directory) / name for name in files
                                      if name.endswith('.py'))
    matcher = re.compile(pattern, re.MULTILINE)
    return [path for path in candidates
            if path.suffix == '.py' and path.is_file() and not path.is_symlink()
            and matcher.search(path.read_text(encoding='utf-8'))]


def test_client_and_business_routes_cannot_import_database_drivers():
    offenders = {}
    candidates = list((ROOT / 'lib' / 'storage').rglob('*.py'))
    candidates += list((ROOT / 'routes').rglob('*.py'))
    for path in candidates:
        imports = _driver_imports(path)
        if imports:
            offenders[str(path.relative_to(ROOT))] = imports
    assert offenders == {}


def test_only_offline_tools_import_drivers_outside_sidecar():
    """Direct drivers outside the Sidecar are limited to stopped-server tools."""
    offenders = set()
    for path in _matching_files(r'^(?:import|from) (?:sqlite3|psycopg|psycopg2)'):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith('lib/storage_authority_sql/'):
            continue
        if _driver_imports(path):
            offenders.add(relative)
    assert offenders <= OFFLINE_DRIVER_IMPORT_ALLOWLIST, (
        'a new process imported a database driver outside the Sidecar: '
        + ', '.join(sorted(offenders - OFFLINE_DRIVER_IMPORT_ALLOWLIST)))


@pytest.mark.parametrize('indexed', [False, True])
def test_offline_driver_gate_still_enforces_imports_without_search_binaries(
        tmp_path, monkeypatch, indexed):
    business = tmp_path / 'lib' / 'business.py'
    offline = tmp_path / 'scripts' / 'offline_migration.py'
    for path in (business, offline):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('import sqlite3\n', encoding='utf-8')
    if indexed:
        subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
        subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    original_run = subprocess.run

    def unavailable_search(command, *args, **kwargs):
        if command[0] == 'rg' or command[0] == 'git' and not indexed:
            raise FileNotFoundError(command[0])
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', unavailable_search)
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    monkeypatch.setitem(globals(), 'OFFLINE_DRIVER_IMPORT_ALLOWLIST',
                        {'scripts/offline_migration.py'})
    with pytest.raises(AssertionError, match='lib/business.py'):
        test_only_offline_tools_import_drivers_outside_sidecar()
    business.write_text('value = 1\n', encoding='utf-8')
    test_only_offline_tools_import_drivers_outside_sidecar()
    untracked = tmp_path / 'lib' / 'untracked_driver.py'
    untracked.write_text('import sqlite3\n', encoding='utf-8')
    if indexed:
        test_only_offline_tools_import_drivers_outside_sidecar()
    else:
        with pytest.raises(AssertionError, match='lib/untracked_driver.py'):
            test_only_offline_tools_import_drivers_outside_sidecar()


def test_only_sidecar_owns_new_storage_connections_and_paths():
    for path in (ROOT / 'lib' / 'storage').rglob('*.py'):
        source = path.read_text(encoding='utf-8')
        assert '.connect(' not in source
        assert 'TOFU_DB_PATH' not in source
        assert 'tofu.db' not in source


def test_sidecar_protocol_has_no_sql_field_or_transaction_handle():
    client = (ROOT / 'lib' / 'storage' / 'client.py').read_text(encoding='utf-8')
    assert "'sql'" not in client
    assert 'transaction_id' not in client
    assert 'connection_handle' not in client


def test_postgres_sidecar_cannot_manage_a_local_database_cluster():
    """The enterprise adapter owns connections, never PostgreSQL processes."""
    path = ROOT / 'lib' / 'storage_authority_sql' / 'adapters' / 'postgres.py'
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split('.')[0])

    assert not imported_roots.intersection({'subprocess', 'socket', 'shutil'})
    for removed_cluster_capability in (
            'initdb', 'pg_ctl', 'pg_basebackup', 'pg_verifybackup', 'pgdata'):
        assert removed_cluster_capability not in source
    assert 'ConnectionPool(' in source
    assert 'self.config.postgres_dsn' in source
