"""Explicit native candidates and attempted builds cannot become silent skips."""
import subprocess
import pytest

pytestmark = pytest.mark.unit


def _fail_without_skip(call, pattern):
    with pytest.raises(pytest.fail.Exception, match=pattern):
        try:
            call()
        except pytest.skip.Exception as exc:
            raise AssertionError(f'explicit verification was silently skipped: {exc}') from exc


def test_explicit_supervisor_candidate_failure_is_not_a_skip(monkeypatch):
    from tests.storage import test_storage_supervisor_tofudb as suite
    monkeypatch.setenv('TOFUDB_BIN', '/nonexistent/explicit-candidate')
    _fail_without_skip(suite._tofudb_binary, 'TOFUDB_BIN')


def test_explicit_differential_candidate_failure_is_not_a_skip(monkeypatch):
    from tests.tofudb import test_tofudb_differential as suite
    monkeypatch.setenv('TOFUDB_DIFFERENTIAL_BIN', '/nonexistent/explicit-candidate')
    _fail_without_skip(suite._resolve_tofudb_binary, 'TOFUDB_DIFFERENTIAL_BIN')


def test_failed_source_build_cannot_fall_back_to_stale_binary(tmp_path, monkeypatch):
    from tests.tofudb import test_tofudb_differential as suite
    from tests.support import tofudb_binary as binary_support
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    cargo = tmp_path / 'cargo'; cargo.write_text('synthetic cargo')
    stale = tmp_path / 'packages/tofu-db/target/debug/tofu-db'
    stale.parent.mkdir(parents=True); stale.write_text('stale executable'); stale.chmod(0o755)
    monkeypatch.setattr(suite, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(binary_support, '_CARGO_CANDIDATES', [cargo])
    # The resolver caches per process; clear it so this scenario's candidates
    # are actually consulted.
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    def failed_build(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr='synthetic compile failure')
    monkeypatch.setattr(suite.subprocess, 'run', failed_build)
    _fail_without_skip(suite._resolve_tofudb_binary, 'build failed')


def test_missing_implicit_supervisor_installation_remains_optional(monkeypatch):
    from tests.storage import test_storage_supervisor_tofudb as suite
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.delenv('TOFUDB_EXPECTED_SHA256', raising=False)
    def absent():
        raise RuntimeError('no native engine installed')
    monkeypatch.setattr(suite, '_resolve_tofudb_binary', absent)
    with pytest.raises(pytest.skip.Exception, match='no native engine'):
        suite._tofudb_binary()


@pytest.mark.parametrize('result', ['timeout', 'missing_output'])
def test_attempted_source_build_must_produce_verified_candidate(tmp_path, monkeypatch, result):
    from tests.tofudb import test_tofudb_differential as suite
    from tests.support import tofudb_binary as binary_support
    monkeypatch.delenv('TOFUDB_DIFFERENTIAL_BIN', raising=False)
    cargo = tmp_path / 'cargo'; cargo.write_text('synthetic cargo')
    monkeypatch.setattr(suite, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(binary_support, '_CARGO_CANDIDATES', [cargo])
    monkeypatch.setattr(binary_support, '_RESOLVED', None)
    monkeypatch.setattr(binary_support, '_RESOLUTION_ATTEMPTED', False)
    def build(*args, **kwargs):
        if result == 'timeout':
            raise subprocess.TimeoutExpired(args[0], 1)
        return subprocess.CompletedProcess(args[0], 0)
    monkeypatch.setattr(suite.subprocess, 'run', build)
    _fail_without_skip(suite._resolve_tofudb_binary, 'build')


def test_explicit_content_pin_failure_cannot_be_skipped(monkeypatch):
    from tests.storage import test_storage_supervisor_tofudb as suite
    monkeypatch.delenv('TOFUDB_BIN', raising=False)
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', '0' * 64)
    def mismatch():
        raise RuntimeError('tofu-db SHA-256 mismatch')
    monkeypatch.setattr(suite, '_resolve_tofudb_binary', mismatch)
    _fail_without_skip(suite._tofudb_binary, 'SHA-256 mismatch')
