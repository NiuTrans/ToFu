"""Installer repair admission, source integrity and transactional publication."""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from bootstrap_pkg import runtime_checks, runtime_repair

pytestmark = pytest.mark.unit


def test_healthy_runtime_never_repairs(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_checks, 'check_javascript_runtime', lambda **kw: (True, 'healthy'))
    monkeypatch.setattr(runtime_repair, 'repair_javascript_runtime', lambda *a, **kw: pytest.fail('unexpected repair'))
    assert runtime_checks.ensure_javascript_runtime(root=tmp_path) == (True, 'healthy')


@pytest.mark.parametrize('detail', ['GLIBC_2.18 not found', "version `GLIBC_2.28' not found"])
def test_glibc_failure_enters_one_repair(monkeypatch, tmp_path, detail):
    calls = []
    monkeypatch.setattr(runtime_checks, 'check_javascript_runtime', lambda **kw: (False, detail))
    monkeypatch.setattr(runtime_repair, 'repair_javascript_runtime',
                        lambda message, **kw: (calls.append((message, kw)) or (True, 'repaired')))
    assert runtime_checks.ensure_javascript_runtime(root=tmp_path) == (True, 'repaired')
    assert len(calls) == 1 and calls[0][1]['root'] == tmp_path


@pytest.mark.parametrize('detail', ['timed out', 'WASM checksum mismatch', 'segmentation fault', 'missing wasmtime'])
def test_other_failures_do_not_download_or_build(monkeypatch, tmp_path, detail):
    monkeypatch.setattr(runtime_repair, '_build_library', lambda *a: pytest.fail('unexpected build'))
    assert runtime_repair.repair_javascript_runtime(detail, root=tmp_path) == (False, detail)


@pytest.mark.parametrize('outcome', ['success', 'failure', 'exception', 'cancel'])
def test_publication_restores_original_on_failed_verification(tmp_path, outcome):
    target, candidate = tmp_path / 'installed.so', tmp_path / 'candidate.so'
    target.write_bytes(b'original')
    candidate.write_bytes(b'compatible')
    def check(**kw):
        assert target.read_bytes() == b'compatible'
        if outcome == 'exception':
            raise OSError('probe spawn failed')
        if outcome == 'cancel':
            raise KeyboardInterrupt
        return outcome == 'success', 'probe result'
    if outcome in ('exception', 'cancel'):
        with pytest.raises(OSError if outcome == 'exception' else KeyboardInterrupt):
            runtime_repair._publish_library(candidate, target, root=tmp_path, check=check)
    else:
        healthy, detail = runtime_repair._publish_library(candidate, target, root=tmp_path, check=check)
        assert healthy is (outcome == 'success')
    assert target.read_bytes() == (b'compatible' if outcome == 'success' else b'original')
    assert not list(tmp_path.glob('.wasmtime-*'))


def test_source_hash_mismatch_never_extracts(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_repair, 'urlopen', lambda *a, **kw: io.BytesIO(b'untrusted'))
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        runtime_repair._download_source(tmp_path)
    assert not (tmp_path / 'wasmtime-49.0.0').exists()


def test_source_path_escape_is_rejected(monkeypatch, tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        member = tarfile.TarInfo('../escaped-file')
        member.size = 3
        archive.addfile(member, io.BytesIO(b'bad'))
    content = stream.getvalue()
    monkeypatch.setattr(runtime_repair, 'SOURCE_SHA256', hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(runtime_repair, 'urlopen', lambda *a, **kw: io.BytesIO(content))
    with pytest.raises(tarfile.FilterError):
        runtime_repair._download_source(tmp_path)
    assert not (tmp_path.parent / 'escaped-file').exists()


@pytest.mark.parametrize('probe_healthy', [True, False])
def test_built_library_must_pass_staged_execution_before_publication(monkeypatch, tmp_path, probe_healthy):
    package = tmp_path / 'wasmtime'
    native = package / 'linux-x86_64'
    native.mkdir(parents=True)
    target = native / '_libwasmtime.so'
    target.write_bytes(b'original')
    monkeypatch.setattr(runtime_repair.sys, 'platform', 'linux')
    monkeypatch.setattr(runtime_repair.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(runtime_repair.metadata, 'distribution', lambda name: SimpleNamespace(
        version=runtime_repair.WASMTIME_VERSION, locate_file=lambda name: package))
    def build(destination, emit, **kw):
        result = destination / 'library.so'
        result.write_bytes(b'compatible')
        return result
    monkeypatch.setattr(runtime_repair, '_build_library', build)
    calls = []
    def check(**kw):
        calls.append(kw)
        if kw.get('package_root'):
            assert (kw['package_root'] / 'wasmtime/linux-x86_64/_libwasmtime.so').read_bytes() == b'compatible'
            assert target.read_bytes() == b'original'
        return probe_healthy, 'synthetic check'
    monkeypatch.setattr(runtime_checks, 'check_javascript_runtime', check)
    healthy, detail = runtime_repair.repair_javascript_runtime('GLIBC_2.18 not found', root=tmp_path)
    assert healthy is probe_healthy
    assert len(calls) == (2 if probe_healthy else 1)
    assert target.read_bytes() == (b'compatible' if probe_healthy else b'original')
    assert not list(native.glob('.wasmtime-*'))


def test_build_uses_resource_budget_locked_dependencies_and_deadline(monkeypatch, tmp_path):
    import runtime_guards
    monkeypatch.setattr(runtime_repair.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda env: SimpleNamespace(
        effective_cpu_count=8, effective_memory_available_mb=4096))
    monkeypatch.delenv('CARGO_BUILD_JOBS', raising=False)
    monkeypatch.setattr(runtime_repair, '_download_source', lambda destination: destination / 'source')
    calls = []
    def run(command, **kw):
        calls.append((command, kw))
        return 0, 'built'
    monkeypatch.setattr(runtime_repair, 'run_command', run)
    library = runtime_repair._build_library(tmp_path, lambda line: None)
    command, options = calls[0]
    assert '--locked' in command and command[-1] == 'wasmtime-c-api'
    assert options['timeout'] == runtime_repair.BUILD_TIMEOUT
    assert options['env']['CARGO_BUILD_JOBS'] == '4'
    assert Path(options['env']['CARGO_TARGET_DIR']).is_relative_to(tmp_path)
    assert library.is_relative_to(tmp_path)


def test_repair_pin_matches_declared_runtime_dependency():
    import tomllib
    configuration = tomllib.loads((Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text())
    assert f'wasmtime=={runtime_repair.WASMTIME_VERSION}' in configuration['project']['dependencies']


def test_only_one_repair_can_own_environment(tmp_path):
    with runtime_repair._repair_lock(tmp_path):
        with pytest.raises(RuntimeError, match='Another Wasmtime repair'):
            with runtime_repair._repair_lock(tmp_path):
                pytest.fail('second writer acquired lock')
    with runtime_repair._repair_lock(tmp_path):
        pass


def test_build_uses_project_toolchain_without_exporting_project_secrets(monkeypatch, tmp_path):
    import runtime_guards
    (tmp_path / '.env').write_text('CARGO_HOME=/configured/cargo\nRUSTUP_HOME=/configured/rust\nCARGO_BUILD_JOBS=3\nLLM_API_KEY=private-project-key\n')
    monkeypatch.delenv('CARGO_HOME', raising=False)
    monkeypatch.delenv('RUSTUP_HOME', raising=False)
    monkeypatch.setenv('CARGO_BUILD_JOBS', '5')
    monkeypatch.setattr(runtime_repair.shutil, 'which', lambda name, **kw: '/tools/' + name)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda env: SimpleNamespace(
        effective_cpu_count=8, effective_memory_available_mb=8192))
    monkeypatch.setattr(runtime_repair, '_download_source', lambda destination: destination / 'source')
    captured = []
    monkeypatch.setattr(runtime_repair, 'run_command', lambda command, **kw: (captured.append(kw['env']) or (0, 'built')))
    runtime_repair._build_library(tmp_path, lambda line: None, root=tmp_path)
    assert captured[0]['CARGO_HOME'] == '/configured/cargo'
    assert captured[0]['RUSTUP_HOME'] == '/configured/rust'
    assert captured[0]['CARGO_BUILD_JOBS'] == '5'
    assert captured[0].get('LLM_API_KEY') != 'private-project-key'
