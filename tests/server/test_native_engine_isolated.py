"""Isolated certification must never publish into a running deployment."""
import hashlib
from pathlib import Path

import pytest

from serverctl_pkg import native_engine as engines
from tests.server.test_native_engine_update import binary

pytestmark = pytest.mark.unit


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    crate = root / 'packages/tofu-db'
    (crate / 'src').mkdir(parents=True)
    (crate / 'Cargo.toml').write_text('[package]\nname="tofu-db"\nversion="0.1.0"\n')
    (crate / 'src/lib.rs').write_text('source')
    (root / '.env').write_text('TOFUDB_EXPECTED_SHA256=live-pin\n')
    return root


def test_isolated_candidate_preserves_configuration(source, tmp_path, monkeypatch):
    candidate = binary(tmp_path / 'candidate/tofu-db', engines.source_fingerprint(source))
    before = (source / '.env').read_bytes()
    probes = []
    monkeypatch.setattr(engines, '_validate_candidate', lambda *args: probes.append(args))
    monkeypatch.setattr(engines, '_build', lambda *a, **k: pytest.fail('explicit candidate must not build'))
    result = engines.prepare_isolated(source, tmp_path / 'isolated', candidate=candidate,
                                     environment={'TOFUDB_EXPECTED_SHA256': 'live-pin'})
    assert result.read_bytes() == candidate.read_bytes()
    assert probes[0][0] == candidate
    assert 'TOFUDB_EXPECTED_SHA256' not in probes[0][2]
    assert (source / '.env').read_bytes() == before
    assert not (source / 'packages/tofu-db/target').exists()
    assert result.with_name(result.name + '.sha256').is_file()


def test_explicit_stale_candidate_fails_without_build(source, tmp_path, monkeypatch):
    candidate = binary(tmp_path / 'candidate/tofu-db', '0' * 64)
    monkeypatch.setattr(engines, '_build', lambda *a, **k: pytest.fail('no replacement'))
    with pytest.raises(engines.EnginePreparationError, match='does not match'):
        engines.prepare_isolated(source, tmp_path / 'isolated', candidate=candidate, environment={})
    assert not (tmp_path / 'isolated/tofu-db').exists()


@pytest.mark.parametrize('failure', ['probe', 'source'])
def test_certification_failure_never_publishes(source, tmp_path, monkeypatch, failure):
    candidate = binary(tmp_path / 'candidate/tofu-db', engines.source_fingerprint(source))
    def probe(*args):
        if failure == 'probe':
            raise engines.EnginePreparationError('probe failed')
        (source / 'packages/tofu-db/src/lib.rs').write_text('changed')
    monkeypatch.setattr(engines, '_validate_candidate', probe)
    with pytest.raises(engines.EnginePreparationError):
        engines.prepare_isolated(source, tmp_path / 'isolated', candidate=candidate, environment={})
    assert not (tmp_path / 'isolated/tofu-db').exists()


def test_build_uses_private_root_and_cleans_it(source, tmp_path, monkeypatch):
    seen = []
    def build(root, staging, env, *, build_root):
        seen.append(build_root)
        assert build_root.is_relative_to(tmp_path / 'isolated')
        return binary(build_root / 'release/tofu-db', engines.source_fingerprint(root))
    monkeypatch.setattr(engines, '_build', build)
    monkeypatch.setattr(engines, '_validate_candidate', lambda *a: None)
    engines.prepare_isolated(source, tmp_path / 'isolated', environment={})
    assert not seen[0].exists()
    assert not (source / 'packages/tofu-db/target').exists()


def test_existing_or_checkout_output_refused(source, tmp_path):
    with pytest.raises(FileExistsError):
        engines.prepare_isolated(source, tmp_path, environment={})
    with pytest.raises(engines.EnginePreparationError, match='outside'):
        engines.prepare_isolated(source, source / 'new', environment={})


def test_pytest_explicit_candidate_is_certified_and_repinned(tmp_path, monkeypatch):
    from tests.conftest import _configure_explicit_test_engine
    candidate = tmp_path / 'tofu-db'
    candidate.write_bytes(b'candidate')
    seen = []
    monkeypatch.setattr(engines, 'verify_current', lambda *args: seen.append(args))
    env = {'TOFU_TEST_TOFUDB_BIN': str(candidate), 'TOFUDB_EXPECTED_SHA256': 'live-pin'}
    _configure_explicit_test_engine(env)
    assert seen[0][0] == candidate
    assert env['TOFUDB_BIN'] == str(candidate)
    assert env['TOFUDB_EXPECTED_SHA256'] == hashlib.sha256(b'candidate').hexdigest()


def test_pytest_explicit_candidate_refusal_is_fatal(tmp_path, monkeypatch):
    from tests.conftest import _configure_explicit_test_engine
    def refuse(*args):
        raise engines.EnginePreparationError('stale')
    monkeypatch.setattr(engines, 'verify_current', refuse)
    with pytest.raises(pytest.UsageError, match='Explicit test engine refused'):
        _configure_explicit_test_engine({'TOFU_TEST_TOFUDB_BIN': str(tmp_path / 'missing')})


def test_isolated_output_symlink_is_never_followed(source, tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    link = tmp_path / 'linked'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        engines.prepare_isolated(source, link, environment={})
    assert list(target.iterdir()) == []


def test_build_routes_snapshot_and_cargo_target_to_private_directory(source, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import runtime_guards

    private = tmp_path / 'private'
    private.mkdir()
    calls = []
    monkeypatch.setattr(engines.shutil, 'which', lambda name, **kw: '/toolchain/' + name)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda env: SimpleNamespace(
        effective_cpu_count=2, effective_memory_available_mb=2048))
    monkeypatch.setattr(engines, '_run', lambda command, **kw: calls.append(kw))
    result = engines._build(source, private, {}, build_root=private)
    assert result.is_relative_to(private)
    assert calls[0]['cwd'].is_relative_to(private)
    assert Path(calls[0]['environment']['CARGO_TARGET_DIR']).is_relative_to(private)
    assert not (source / 'packages/tofu-db/target').exists()
