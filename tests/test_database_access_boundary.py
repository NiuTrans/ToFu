"""Static ratchets that keep storage-driver access inside the data layer."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOTS = ('lib', 'routes', 'server.py')
_PROJECT_PYTHON_ROOTS = ('lib', 'routes', 'scripts', 'debug', 'server.py')
_DATA_ACCESS_ROOTS = ('lib', 'routes', 'scripts', 'debug', 'server.py')

# Every SQLite authority belongs to the Sidecar or an explicitly offline
# migration tool. Application modules never own connections.
_SQLITE_CONNECT_ALLOWLIST = {
    'lib/storage_sidecar/adapters/sqlite.py',
    'lib/storage_sidecar/cli.py',
    'lib/storage_sidecar/cutover.py',
    # The fastpath front's file-level machinery: WAL-shipper online backup
    # (sqlite3 backup API needs raw handles), the candidate-dir scratch WAL
    # probe, and the restored-front verification open.  None can ride the
    # pooled data layer — they operate on the raw front/shadow files.
    'lib/storage_sidecar/shipper.py',
    'lib/storage_sidecar/fastpath.py',
    # The bounded turn-search materialization is a disposable, replayable
    # database with its own writer/read pool.  It must not borrow the durable
    # authority adapter because that would couple cache latency and failure to
    # authoritative transactions.
    'lib/storage_sidecar/turn_search_projection.py',
    # Launch-time capability discovery opens only a UUID-named scratch
    # database, verifies WAL recovery, then removes every artifact before
    # backend selection.  Keeping the probe stdlib-only is intentional.
    'lib/storage_sidecar/storage_capabilities.py',
    # Reviewed offline data-layer owners.  The reader hard-enforces mode=ro
    # plus query_only.  Maintenance writes require a live ProjectLease, and
    # candidate opens first prove they are not the canonical authority.
    'lib/storage_sidecar/offline.py',
    'lib/storage_sidecar/offline_maintenance.py',
    # Offline authority migrations run only under a stopped-project lease.
    'scripts/migrate_pg_to_sqlite.py',
    'scripts/migrate_sqlite_to_postgres.py',
}
_TRANSCRIPT_ARCHIVE_ADMIN_ALLOWLIST: set[str] = set()
_EXPLICIT_MAINTENANCE_AUTHORITY_ALLOWLIST = {
    'debug/_standalone_guard.py',
    'debug/backfill_search_text_originalcontent.py',
    'debug/backfill_translated_search.py',
    'debug/backfill_truncated_vu_parents.py',
    'debug/test_l1_compact_cache_tradeoff.py',
    'debug/test_l1_prefix_skip_vs_aggressive.py',
    'debug/test_l1_skip_vs_aggressive_v2.py',
}
_OFFLINE_TRANSACTION_SCRIPT_ALLOWLIST = {
    'scripts/migrate_pg_to_sqlite.py',
    'scripts/migrate_sqlite_to_postgres.py',
}
_DATA_LAYER_CALLBACK_SQL_ALLOWLIST = {
    # CAS operations are supplied to run_sqlite_tool_write; driver, owner,
    # BEGIN IMMEDIATE, retry, commit and rollback remain in sqlite_tooling.
    'scripts/storage_deep_clean.py',
}


_TRACKED_FILES: set[str] | None = None


def _is_tracked(rel: str) -> bool:
    """Ratchets measure the COMMITTED tree, never uncommitted scratch.

    Untracked/gitignored maintenance scratch (debug/*.py one-shots,
    gitignored scripts/) flickers in and out of shared dev checkouts as
    siblings stash and recreate it, which made this suite non-deterministic
    red without any committed change.  CI fresh checkouts see every file
    tracked, so gating on ``git ls-files`` changes nothing there; locally it
    keeps the suite a function of the tree state it can actually ratchet.
    """
    global _TRACKED_FILES
    if _TRACKED_FILES is None:
        result = subprocess.run(
            ['git', 'ls-files'], cwd=_ROOT, text=True,
            capture_output=True, check=False)
        _TRACKED_FILES = (set(result.stdout.splitlines())
                          if result.returncode == 0 else set())
        if result.returncode != 0:  # fail open: no git → scan everything
            return True
    return rel in _TRACKED_FILES


def _candidate_files(pattern: str, roots=_PRODUCTION_ROOTS):
    """Let ripgrep prefilter the small set that needs an AST parse."""
    roots = [name for name in roots if (_ROOT / name).exists()]
    result = subprocess.run(
        ['rg', '-l', '--glob', '*.py', pattern, *roots],
        cwd=_ROOT, text=True, capture_output=True, check=False)
    assert result.returncode in (0, 1), result.stderr
    for rel in result.stdout.splitlines():
        if _is_tracked(rel):
            yield _ROOT / rel


def _relative(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _is_data_owner(rel: str) -> bool:
    """Only the Sidecar implementation owns live database capabilities."""
    return rel.startswith('lib/storage_sidecar/')


def _is_sqlite_connect(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == 'connect'
        and isinstance(func.value, ast.Name)
        and func.value.id == 'sqlite3'
    )


def _is_database_driver_connect(call: ast.Call) -> bool:
    """Match direct driver entry points that bypass the shared pool/wrappers."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == 'connect'
        and isinstance(func.value, ast.Name)
        and func.value.id in {'sqlite3', 'psycopg', 'psycopg2', 'asyncpg'}
    )


def _is_low_level_connection_call(call: ast.Call) -> bool:
    """Match db.raw.execute(...) and db._conn.cursor()/commit()/rollback()."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in {
            'execute', 'executemany', 'executescript', 'cursor',
            'commit', 'rollback'}:
        return False
    receiver = func.value
    return isinstance(receiver, ast.Attribute) and receiver.attr in {
        'raw', '_conn'}


def test_sqlite_connections_have_an_explicit_storage_owner():
    violations = []
    for path in _candidate_files(r'sqlite3\s*\.\s*connect'):
        rel = _relative(path)
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and _is_sqlite_connect(node)
                    and rel not in _SQLITE_CONNECT_ALLOWLIST):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'New sqlite3.connect() bypasses the project data layer. Add an '
        'operation to the Sidecar instead of extending this allowlist: '
        + ', '.join(violations))


def test_application_cannot_open_database_driver_connections():
    """SQLite and PostgreSQL connection construction stays in the data layer."""
    violations = []
    for path in _candidate_files(
            r'(?:sqlite3|psycopg2?|asyncpg)\s*\.\s*connect'):
        rel = _relative(path)
        if _is_data_owner(rel):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_database_driver_connect(node):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code opens a raw SQLite/PostgreSQL connection. Use the '
        'semantic StorageClient API: ' + ', '.join(violations))


def test_project_sqlite_connections_belong_to_the_data_layer():
    """Offline/debug entry points cannot grow a second SQLite writer stack."""
    violations = []
    for path in _candidate_files(
            r'sqlite3\s*\.\s*connect', _PROJECT_PYTHON_ROOTS):
        rel = _relative(path)
        if _is_data_owner(rel):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_sqlite_connect(node):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Project tooling opens SQLite outside the Sidecar/offline allowlist and can bypass '
        'canonical write ownership/transaction rules. Add a data-layer '
        'tooling API: ' + ', '.join(violations))


def test_debug_and_new_scripts_cannot_self_authorize_canonical_writes():
    """Canonical maintenance authority is a reviewed, self-discovering list."""
    violations = []
    for path in _candidate_files(
            r'TOFU_SERVER_PROCESS|maintenance_write_authority',
            ('scripts', 'debug')):
        rel = _relative(path)
        source = path.read_text(encoding='utf-8')
        if rel in _EXPLICIT_MAINTENANCE_AUTHORITY_ALLOWLIST:
            continue
        # Setting the server role is never a maintenance API. The migration
        # copier's child explicitly clears it, which is safe and intentional.
        if 'TOFU_SERVER_PROCESS' in source and "['TOFU_SERVER_PROCESS'] = '0'" in source:
            source = source.replace("['TOFU_SERVER_PROCESS'] = '0'", '')
        if ('TOFU_SERVER_PROCESS' in source
                or 'maintenance_write_authority' in source):
            violations.append(rel)
    assert not violations, (
        'Debug/new script can grant itself canonical SQLite write authority. '
        'Move the operation into a reviewed data-layer maintenance API and '
        'add only its CLI entry point to the explicit allowlist: '
        + ', '.join(violations))


def test_removed_database_namespace_stays_absent():
    assert not (_ROOT / 'lib' / 'database').exists()


def test_application_code_cannot_reach_through_connection_wrappers():
    violations = []
    for path in _candidate_files(
            r'(?:\.raw|\._conn)\s*\.\s*'
            r'(?:execute|executemany|executescript|cursor|commit|rollback)'):
        rel = _relative(path)
        if _is_data_owner(rel):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_low_level_connection_call(node):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code bypasses the database wrapper/writer discipline. '
        'Move the operation into the Sidecar and expose a semantic API: '
        + ', '.join(violations))


_TRANSCRIPT_SELECT_RE = re.compile(
    r'\bselect\b(?:(?!;).)*\bmessages\b(?:(?!;).)*\bfrom\s+conversations\b'
    r'|\bselect\b(?:(?!;).)*\bfrom\s+conversations\b(?:(?!;).)*'
    r'\bmessages\b',
    re.IGNORECASE | re.DOTALL,
)
_TRANSCRIPT_WRITE_RE = re.compile(
    r'\bupdate\s+["`\[]?conversations["`\]]?\s+set\b'
    r'(?:(?!;).)*\bmessages\b\s*='
    r'|\b(?:insert|replace)(?:\s+or\s+\w+)?\s+into\s+'
    r'["`\[]?conversations["`\]]?\s*\('
    r'(?:(?!\)).)*\bmessages\b',
    re.IGNORECASE | re.DOTALL,
)
_RAW_TRANSACTION_SQL_RE = re.compile(
    r'(?:^|;)\s*(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b',
    re.IGNORECASE,
)
_DDL_SQL_RE = re.compile(
    r'\b(?:CREATE\s+(?:TABLE|INDEX)|ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX))\b',
    re.IGNORECASE,
)
_WRITE_SQL_RE = re.compile(
    r'^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b', re.IGNORECASE)


def _static_string(node: ast.AST) -> str | None:
    """Best-effort value for literal/concatenated/f-string SQL arguments."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(
            part.value if isinstance(part, ast.Constant)
            and isinstance(part.value, str) else '?'
            for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return None if left is None or right is None else left + right
    return None


def _call_name(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ''
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ''


def test_application_transcript_reads_use_the_conversation_repository():
    """Rows/archive authority selection belongs to one data-layer seam."""
    violations = []
    for path in _candidate_files(
            r'(?i)(?:select|messages|conversations)', _DATA_ACCESS_ROOTS):
        rel = _relative(path)
        if (_is_data_owner(rel)
                or rel in _TRANSCRIPT_ARCHIVE_ADMIN_ALLOWLIST):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _TRANSCRIPT_SELECT_RE.search(node.value)):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code reads conversations.messages directly and can '
        'resurrect the archive after row-authority cutover. Use '
        'load_conversation/list_conversation_snapshots: '
        + ', '.join(violations))


def test_application_transcript_writes_use_the_conversation_repository():
    """A plugin/caller must never write the frozen archive behind rows."""
    violations = []
    for path in _candidate_files(
            r'(?i)(?:update|insert|replace|messages)', _DATA_ACCESS_ROOTS):
        rel = _relative(path)
        if (_is_data_owner(rel)
                or rel in _TRANSCRIPT_ARCHIVE_ADMIN_ALLOWLIST):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _TRANSCRIPT_WRITE_RE.search(node.value)):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code writes conversations.messages directly and can '
        'lose updates after row-authority cutover. Use '
        'upsert_conversation/replace_messages/mutate_conversation: '
        + ', '.join(violations))


def test_application_cannot_issue_raw_transaction_sql():
    """Transaction boundaries belong to the shared data-layer primitive."""
    violations = []
    for path in _candidate_files(
            r'(?i)(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)',
            _PROJECT_PYTHON_ROOTS):
        rel = _relative(path)
        if _is_data_owner(rel):
            continue
        if rel in _OFFLINE_TRANSACTION_SCRIPT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {
                        'execute', 'executemany', 'executescript'}
                    and node.args):
                continue
            sql_arg = node.args[0]
            if (isinstance(sql_arg, ast.Constant)
                    and isinstance(sql_arg.value, str)
                    and _RAW_TRANSACTION_SQL_RE.search(sql_arg.value)):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code issues raw transaction SQL instead of a semantic '
        'Sidecar command: ' + ', '.join(violations))


def test_application_cannot_own_connection_transaction_boundaries():
    """No business module may grow a hand-written commit/rollback/begin path."""
    violations = []
    for path in _candidate_files(
            r'\.(?:commit|rollback|begin)\s*\(', _PROJECT_PYTHON_ROOTS):
        rel = _relative(path)
        if _is_data_owner(rel):
            continue
        if rel in _OFFLINE_TRANSACTION_SCRIPT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {'commit', 'rollback', 'begin'}):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code owns a connection transaction boundary instead of '
        'a semantic Sidecar command: ' + ', '.join(violations))


def test_direct_application_writes_require_an_owned_transaction():
    """A bare execute(UPDATE/INSERT/DELETE) must never await a later commit."""
    violations = []
    for path in _candidate_files(
            r'(?i)\b(?:INSERT|UPDATE|DELETE|REPLACE)\b',
            _PROJECT_PYTHON_ROOTS):
        rel = _relative(path)
        if _is_data_owner(rel):
            continue
        if (rel in _OFFLINE_TRANSACTION_SCRIPT_ALLOWLIST
                or rel in _DATA_LAYER_CALLBACK_SQL_ALLOWLIST):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        guarded_functions = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(_call_name(child) == 'assert_write_transaction'
                   for child in ast.walk(node)):
                guarded_functions.add(node)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {
                        'execute', 'executemany', 'executescript'}
                    and node.args):
                continue
            sql = _static_string(node.args[0])
            if sql is None or not _WRITE_SQL_RE.search(sql):
                continue

            owned = False
            current = node
            while current in parents:
                current = parents[current]
                if current in guarded_functions:
                    owned = True
                    break
                if isinstance(current, (ast.With, ast.AsyncWith)):
                    if any(_call_name(item.context_expr) in {
                            'write_transaction', 'pooled_write_transaction'}
                           for item in current.items):
                        owned = True
                        break
            if not owned:
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Direct application write has no lexical write_transaction and no '
        'assert_write_transaction helper contract; it can be committed by an '
        'unrelated later operation: ' + ', '.join(violations))


def test_runtime_schema_changes_are_owned_by_sidecar():
    """Business imports must not race startup with ad-hoc CREATE/ALTER DDL."""
    violations = []
    for path in _candidate_files(
            r'(?i)(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX)',
            _PROJECT_PYTHON_ROOTS):
        rel = _relative(path)
        if (_is_data_owner(rel)
                or rel in _OFFLINE_TRANSACTION_SCRIPT_ALLOWLIST):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {
                        'execute', 'executemany', 'executescript'}
                    and node.args):
                continue
            sql = _static_string(node.args[0])
            if sql is not None and _DDL_SQL_RE.search(sql):
                violations.append(f'{rel}:{node.lineno}')
    assert not violations, (
        'Application code owns runtime DDL. Define/migrate it in the Sidecar '
        'and expose a semantic API: '
        + ', '.join(violations))
