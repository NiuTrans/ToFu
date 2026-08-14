"""Static guard for the new process-isolated storage boundary."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DRIVERS = {'sqlite3', 'psycopg', 'psycopg2'}
LEGACY_DRIVER_IMPORT_ALLOWLIST = {
    'lib/database/_core.py',
    'lib/database/backup.py',
    'lib/database/integration_control_repository.py',
    'lib/database/knowledge_repository.py',
    'lib/database/sqlite_cutover.py',
    'lib/database/sqlite_driver_guard.py',
    'lib/database/sqlite_tooling.py',
}
pytestmark = pytest.mark.unit


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
    roots = ['lib', 'routes', 'scripts', 'server.py']
    result = subprocess.run(
        ['rg', '-l', '--glob', '*.py', pattern, *roots],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return [ROOT / relative for relative in result.stdout.splitlines()]


def test_client_and_business_routes_cannot_import_database_drivers():
    offenders = {}
    candidates = list((ROOT / 'lib' / 'storage').rglob('*.py'))
    candidates += list((ROOT / 'routes').rglob('*.py'))
    for path in candidates:
        imports = _driver_imports(path)
        if imports:
            offenders[str(path.relative_to(ROOT))] = imports
    assert offenders == {}


def test_legacy_driver_import_inventory_can_only_shrink():
    """Make phased migration debt explicit without authorizing new owners."""
    offenders = set()
    for path in _matching_files(r'^(?:import|from) (?:sqlite3|psycopg|psycopg2)'):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith('lib/storage_sidecar/'):
            continue
        if _driver_imports(path):
            offenders.add(relative)
    assert offenders <= LEGACY_DRIVER_IMPORT_ALLOWLIST, (
        'a new process imported a database driver outside the Sidecar: '
        + ', '.join(sorted(offenders - LEGACY_DRIVER_IMPORT_ALLOWLIST)))


def test_legacy_connection_and_plugin_callback_inventories_can_only_shrink():
    thread_db_files = [
        path.relative_to(ROOT).as_posix()
        for path in _matching_files('get_thread_db')
    ]
    tofu_schema_files = [
        path.relative_to(ROOT).as_posix()
        for path in _matching_files(r'tofu\.schema')
    ]
    assert len(thread_db_files) <= 58, (
        'get_thread_db migration inventory grew: ' + ', '.join(thread_db_files))
    assert len(tofu_schema_files) <= 5, (
        'tofu.schema callback inventory grew: ' + ', '.join(tofu_schema_files))


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
