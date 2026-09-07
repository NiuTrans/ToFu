"""Regression tests for the test-storage data-loss guard.

The original incident involved a test process inheriting production database
configuration.  Storage is now owned by the Sidecar, so the durable invariant
is backend-independent: every pytest worker and standalone runner must be
bound to its exact disposable project root before application boot.
"""

from __future__ import annotations

import ast
import glob
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

conftest = importlib.import_module('tests.conftest')


def test_sqlite_backend_is_safe(monkeypatch):
    """The pytest worker's exact disposable Sidecar root is accepted."""
    monkeypatch.setenv(
        'TOFU_STORAGE_PROJECT_ROOT', conftest._PYTEST_STORAGE_ROOT)
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    ok, detail = conftest._storage_is_test_safe()
    assert ok, detail
    conftest._assert_isolated_storage('unit-sqlite')


def test_pg_production_db_is_refused(monkeypatch):
    """A repository-root authority is refused before backend resolution."""
    repository = Path(conftest.__file__).resolve().parents[1]
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(repository))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.setattr(
        conftest, '_PYTEST_STORAGE_ROOT', str(repository))
    ok, detail = conftest._storage_is_test_safe()
    assert not ok
    assert 'source checkout' in detail
    with pytest.raises(pytest.UsageError):
        conftest._assert_isolated_storage('unit-pg-prod')


def test_pg_without_optin_refused_even_for_testname(monkeypatch):
    """A matching root is unsafe without the explicit test-authority gate."""
    monkeypatch.setenv(
        'TOFU_STORAGE_PROJECT_ROOT', conftest._PYTEST_STORAGE_ROOT)
    monkeypatch.delenv(
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', raising=False)
    ok, _ = conftest._storage_is_test_safe()
    assert not ok


def test_pg_optin_but_production_dbname_refused(monkeypatch):
    """An explicit gate cannot authorize a different project root."""
    other = Path(conftest._PYTEST_STORAGE_ROOT).with_name(
        'different-test-authority')
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(other))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    ok, detail = conftest._storage_is_test_safe()
    assert not ok
    assert 'differs from worker root' in detail


def test_pg_optin_with_testname_allowed(monkeypatch):
    """A missing project root always fails closed."""
    monkeypatch.delenv('TOFU_STORAGE_PROJECT_ROOT', raising=False)
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    ok, detail = conftest._storage_is_test_safe()
    assert not ok
    assert 'missing' in detail
    with pytest.raises(pytest.UsageError):
        conftest._assert_isolated_storage('unit-missing-root')


def _set_repository_authority(monkeypatch) -> None:
    repository = Path(conftest.__file__).resolve().parents[1]
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(repository))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')


def test_sdk_e2e_boot_refuses_production_db(monkeypatch):
    """The standalone SDK server refuses an unsafe root before boot."""
    _set_repository_authority(monkeypatch)
    sdk_e2e = importlib.import_module('tests.test_sdk_e2e')
    monkeypatch.setitem(sdk_e2e._STATE, 'app', None)
    monkeypatch.setitem(sdk_e2e._STATE, 'tmp', None)
    with pytest.raises(pytest.UsageError):
        sdk_e2e._boot_real_server()
    assert sdk_e2e._STATE['tmp'] is None, (
        '_boot_real_server proceeded past the storage guard')


def test_sdk_parity_setup_refuses_production_db(monkeypatch):
    """The SDK parity server independently enforces the storage guard."""
    _set_repository_authority(monkeypatch)
    parity = importlib.import_module('tests.test_sdk_parity_e2e')
    monkeypatch.setitem(parity._STATE, 'app', None)
    monkeypatch.setitem(parity._STATE, 'tmp', None)
    with pytest.raises(pytest.UsageError):
        parity._setup_once()
    assert parity._STATE['tmp'] is None


def test_headless_api_setup_refuses_production_db(monkeypatch):
    """The headless API server independently enforces the storage guard."""
    _set_repository_authority(monkeypatch)
    headless = importlib.import_module('tests.test_e2e_headless_api')
    monkeypatch.setitem(headless._STATE, 'app', None)
    monkeypatch.setitem(headless._STATE, 'tmp', None)
    with pytest.raises(pytest.UsageError):
        headless._setup_once()
    assert headless._STATE['tmp'] is None


# Standalone custom runners bypass pytest's conftest import.  Discover every
# executable test module that can write durable state and require it to enter
# through the shared standalone Sidecar guard.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

_DB_WRITE_SIGNATURES = (
    'get_thread_db',
    'upsert(',
    'create_task',
    '_seed_conv',
    'persist_task_result',
    'INSERT INTO',
    'dispatch_next_queued',
    'append_event',
    'append_persistent_event',
)

_SAFE_PATTERNS = (
    re.compile(r'guard_standalone_storage'),
    re.compile(r'from\s+(?:tests\.)?conftest\s+import'),
    re.compile(r'pytest\.main\s*\('),
)

_KNOWN_EXEMPT: dict[str, str] = {
    'test_chat_flow_dispatch.py': (
        'pure in-memory unittest; persistence is replaced by a no-op'),
    'test_task_runtime.py': (
        'uses the in-memory bare TaskRuntime, not the durable manager hook'),
    'test_lib_orchestrator_wire_parity.py': (
        'append_event is only a monkeypatch observation target'),
    'test_paper_migration.py': (
        'the paper report event path is an in-memory report runtime'),
    'test_paper_media_ux.py': (
        '__main__ delegates back to pytest, which loads conftest'),
}


def _has_main_block(src: str) -> bool:
    return bool(re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", src))


def _real_code_identifiers(src: str) -> str:
    """Return executable identifiers, excluding comments and string fixtures."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    tokens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
    return '\n'.join(tokens)


def _touches_db(src: str) -> bool:
    """Return whether executable code can reach a durable-write surface."""
    identifier_surface = _real_code_identifiers(src)
    for signature in _DB_WRITE_SIGNATURES:
        identifier = signature.rstrip('(').split()[0]
        if signature == 'INSERT INTO':
            if 'INSERT INTO' in src and 'execute' in identifier_surface:
                return True
            continue
        if identifier in identifier_surface:
            return True
    return False


def _is_guarded(src: str) -> bool:
    return any(pattern.search(src) for pattern in _SAFE_PATTERNS)


def _discover_db_touching_standalone_runners() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in glob.glob(os.path.join(_TESTS_DIR, 'test_*.py')):
        filename = os.path.basename(path)
        try:
            source = open(path, encoding='utf-8').read()
        except OSError:
            continue
        if _has_main_block(source) and _touches_db(source):
            found[filename] = source
    return found


def test_force_sqlite_env_overrides_ambient_postgres(monkeypatch):
    """Standalone execution composes a disposable personal authority."""
    guard = importlib.import_module('tests._standalone_guard')
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
    monkeypatch.setenv('TOFU_POSTGRES_DSN_FILE', '/production/postgres')
    monkeypatch.setenv('TOFU_REDIS_URL_FILE', '/production/redis')
    with guard.temporary_standalone_storage_environment() as root:
        assert os.environ['TOFU_DEPLOYMENT_MODE'] == 'personal'
        assert os.environ['TOFU_PROCESS_ROLE'] == 'all'
        assert os.environ['TOFU_STORAGE_PROJECT_ROOT'] == str(root)
        assert os.environ['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] == '1'
        assert all(name not in os.environ for name in (
            'TOFU_POSTGRES_DSN_FILE',
            'TOFU_REDIS_URL_FILE',
            'TOFU_REPLICA_ID',
        ))
    assert os.environ['TOFU_DEPLOYMENT_MODE'] == 'distributed'
    assert os.environ['TOFU_POSTGRES_DSN_FILE'] == '/production/postgres'


def test_force_sqlite_env_honours_pg_optin():
    """Two standalone runners never inherit or share one authority root."""
    guard = importlib.import_module('tests._standalone_guard')
    with guard.temporary_standalone_storage_environment() as first:
        assert os.environ['TOFU_STORAGE_PROJECT_ROOT'] == str(first)
    with guard.temporary_standalone_storage_environment() as second:
        assert os.environ['TOFU_STORAGE_PROJECT_ROOT'] == str(second)
    assert first != second


def test_all_standalone_runners_are_guarded():
    """Every durable standalone runner must use the shared storage guard."""
    discovered = _discover_db_touching_standalone_runners()
    assert len(discovered) >= 13, (
        f'scanner found only {len(discovered)} DB-touching standalone runners '
        f'(expected >=13): {sorted(discovered)}')
    unguarded = [
        filename
        for filename, source in sorted(discovered.items())
        if filename not in _KNOWN_EXEMPT and not _is_guarded(source)
    ]
    assert not unguarded, (
        'DB-touching standalone runners are unguarded; add '
        f'guard_standalone_storage(...): {unguarded}')


def test_ratchet_would_catch_an_unguarded_newcomer(tmp_path, monkeypatch):
    """Negative control proves the discovery ratchet is load-bearing."""
    bad = tmp_path / 'test_synthetic_unguarded.py'
    bad.write_text(
        "import sys\n"
        "def main():\n"
        "    db = get_thread_db('chat')\n"
        "    upsert(db, 'conversations', {'id': 'x'})\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding='utf-8',
    )
    source = bad.read_text(encoding='utf-8')
    assert _has_main_block(source) and _touches_db(source), (
        'scanner heuristic broke')
    assert not _is_guarded(source), 'unguarded synthetic runner passed'
    guarded = source.replace(
        'def main():\n',
        "def main():\n"
        "    from tests._standalone_guard import guard_standalone_storage\n"
        "    guard_standalone_storage('synthetic')\n",
    )
    assert _is_guarded(guarded), 'guarded synthetic runner was rejected'


def _probe_backend_subprocess(*, run_guard: bool) -> str:
    """Resolve deployment storage in a child with dangerous ambient config."""
    body = (
        "import sys; sys.path.insert(0, %r)\n"
        "import quart as q, sys as _s; _s.modules['flask'] = q\n"
    ) % _REPO_ROOT
    if run_guard:
        body += (
            'from tests._standalone_guard import guard_standalone_storage\n'
            "guard_standalone_storage('probe', start_authority=False)\n"
        )
    body += (
        'from runtime_guards import load_deployment_configuration\n'
        'd = load_deployment_configuration()\n'
        "print('BACKEND=' + str(d.storage_backend))\n"
    )
    environment = dict(os.environ)
    environment['TOFU_DEPLOYMENT_MODE'] = 'distributed'
    environment['TOFU_PROCESS_ROLE'] = 'api'
    environment.pop('TOFU_POSTGRES_DSN_FILE', None)
    environment.pop('TOFU_REDIS_URL_FILE', None)
    environment.pop('TOFU_REPLICA_ID', None)
    process = subprocess.run(
        [sys.executable, '-c', body],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = process.stdout + process.stderr
    for line in output.splitlines():
        if line.startswith('BACKEND='):
            return line[len('BACKEND='):].strip()
    raise AssertionError(f'probe did not report a backend. output:\n{output}')


@pytest.mark.slow
def test_guard_forces_sqlite_in_subprocess_under_ambient_pg():
    """The standalone guard replaces distributed ambient authority."""
    assert _probe_backend_subprocess(run_guard=True) == 'sqlite'


def test_double_neuter_without_guard_resolves_pg():
    """Without the guard, incomplete distributed configuration fails closed."""
    with pytest.raises(AssertionError, match='probe did not report a backend'):
        _probe_backend_subprocess(run_guard=False)
