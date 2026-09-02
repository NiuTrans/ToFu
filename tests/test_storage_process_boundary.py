"""Static guard for the new process-isolated storage boundary."""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/business.py'}

import ast
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRIVERS = {'sqlite3', 'psycopg', 'psycopg2'}
OFFLINE_DRIVER_IMPORT_ALLOWLIST = {
    # Offline operator tools run while the server/sidecar is stopped; routing
    # them through StorageClient is impossible by design.
    'scripts/migrate_pg_to_sqlite.py',
    'scripts/migrate_sqlite_to_postgres.py',
    'scripts/storage_deep_clean.py',
}
pytestmark = pytest.mark.unit


def test_exclusive_boundary_scanner_rejects_business_sql_but_exempts_sidecar(
        tmp_path):
    from lib.storage_boundary import boundary_report

    business = tmp_path / 'lib' / 'business.py'
    business.parent.mkdir(parents=True)
    business.write_text(
        "import sqlite3\n"
        "conn = sqlite3.connect('x')\n"
        "cursor = conn.cursor()\n"
        "conn.execute('SELECT 1')\n"
        "conn.commit()\n",
        encoding='utf-8')
    adapter = tmp_path / 'lib' / 'storage_sidecar' / 'adapter.py'
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
    from lib.storage_boundary import boundary_report

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
    from lib.storage_boundary import boundary_report

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


def test_real_repo_passes_exclusive_sidecar_boundary():
    """The startup gate must never be the first place a violation surfaces.

    ``require_exclusive_sidecar_boundary`` runs once at server startup; a
    refactor that removes a sidecar fence (or adds a direct DB capability)
    previously failed only there.  Scan the real checkout here so a normal
    test run catches it seconds after the edit instead.  The scanner caches
    per-file results by content signature, so the warm path is fast.
    """
    from lib.storage_boundary import boundary_report

    report = boundary_report(ROOT)
    assert report['ready'], (
        'production files still own direct storage capabilities:\n'
        + json.dumps(report['violations'][:20], indent=1))


def test_exclusive_boundary_scanner_reuses_unchanged_file_results(
        tmp_path, monkeypatch):
    import lib.storage_boundary as boundary

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
    # Ratchet candidates are COMMITTED files only: untracked/gitignored
    # one-shots (e.g. a sibling's in-flight
    # scripts/repair_turn_archive_straddlers.py) flicker in and out of
    # shared dev checkouts and made this inventory non-deterministic red
    # with no committed change (2026-08-23, same class fix as
    # tests/test_database_access_boundary.py).  CI fresh checkouts see
    # every file tracked, so gating on ``git ls-files`` changes nothing.
    roots = ['lib', 'routes', 'scripts', 'server.py']
    result = subprocess.run(
        ['rg', '-l', '--glob', '*.py', pattern, *roots],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    tracked = subprocess.run(
        ['git', 'ls-files'], cwd=ROOT, text=True,
        capture_output=True, check=False)
    if tracked.returncode != 0:  # fail open: no git → scan everything
        return [ROOT / relative for relative in result.stdout.splitlines()]
    tracked_set = set(tracked.stdout.splitlines())
    return [ROOT / relative for relative in result.stdout.splitlines()
            if relative in tracked_set]


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
        if relative.startswith('lib/storage_sidecar/'):
            continue
        if _driver_imports(path):
            offenders.add(relative)
    assert offenders <= OFFLINE_DRIVER_IMPORT_ALLOWLIST, (
        'a new process imported a database driver outside the Sidecar: '
        + ', '.join(sorted(offenders - OFFLINE_DRIVER_IMPORT_ALLOWLIST)))


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
    path = ROOT / 'lib' / 'storage_sidecar' / 'adapters' / 'postgres.py'
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
