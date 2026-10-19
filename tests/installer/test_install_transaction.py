"""Failures before commit preserve the selected interpreter and configuration."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from bootstrap_pkg import install_artifacts as artifacts
from bootstrap_pkg import install_transaction as transaction

pytestmark = pytest.mark.unit


def wheel(path, version='0.10.4'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'tofu_search-{version}.dist-info/METADATA',
                         f'Name: tofu-search\nVersion: {version}\n')
    return path


def test_required_artifact_absence_is_a_consistency_error(tmp_path):
    (tmp_path / 'requirements.txt').write_text('tofu-search>=0.10.4\n')
    (tmp_path / '.tofu_env.json').write_text('old environment')
    with pytest.raises(ValueError, match='RELEASE CONSISTENCY ERROR'):
        artifacts.prepare_search_wheel(tmp_path, 'unused-python')
    assert (tmp_path / '.tofu_env.json').read_text() == 'old environment'


def test_bundled_artifact_is_cached_exactly_and_corruption_fails_closed(tmp_path):
    (tmp_path / 'requirements.txt').write_text('tofu-search>=0.10.4\n')
    original = wheel(tmp_path / 'vendor/tofu_search-0.10.4-py3-none-any.whl')
    cached = artifacts.prepare_search_wheel(tmp_path, 'unused-python')
    assert cached.read_bytes() == original.read_bytes()
    receipt = json.loads((cached.parent / 'manifest.json').read_text())
    assert receipt['version'] == '0.10.4'
    assert receipt['sha256'] == hashlib.sha256(cached.read_bytes()).hexdigest()
    assert artifacts.prepare_search_wheel(tmp_path, 'unused-python') == cached
    cached.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='hash mismatch'):
        artifacts.prepare_search_wheel(tmp_path, 'unused-python')


def candidate(root):
    prefix = root / '.tofu-envs/new'
    prefix.mkdir(parents=True)
    python = prefix / 'python'
    python.write_text('interpreter')
    (root / '.env').write_text('TOFU_PERSONAL_STORAGE_BACKEND=tofudb\n')
    (root / '.tofu_env.json').write_text('old environment')
    (root / '.tofu-env.pending').write_text('TOFU_PERSONAL_STORAGE_BACKEND=tofudb\nPORT=15000\n')
    (root / '.tofu_env.pending.json').write_text(json.dumps({'env_prefix': str(prefix), 'python': str(python)}))
    return prefix


def test_commit_changes_pointer_only_after_candidate_exists(tmp_path):
    prefix = candidate(tmp_path)
    transaction.commit(tmp_path)
    assert json.loads((tmp_path / '.tofu_env.json').read_text())['env_prefix'] == str(prefix)
    assert 'tofudb' in (tmp_path / '.env').read_text()
    assert not (tmp_path / '.tofu_env.pending.json').exists()


def test_pointer_publication_failure_restores_old_configuration(tmp_path, monkeypatch):
    candidate(tmp_path)
    previous = (tmp_path / '.env').read_bytes()
    replace = transaction.os.replace
    def fail_pointer(source, destination):
        if Path(destination).name == '.tofu_env.json':
            raise OSError('fault injection')
        return replace(source, destination)
    monkeypatch.setattr(transaction.os, 'replace', fail_pointer)
    with pytest.raises(OSError, match='fault injection'):
        transaction.commit(tmp_path)
    assert (tmp_path / '.tofu_env.json').read_text() == 'old environment'
    assert (tmp_path / '.env').read_bytes() == previous


def test_missing_candidate_never_changes_live_configuration(tmp_path):
    prefix = candidate(tmp_path)
    (prefix / 'python').unlink()
    with pytest.raises(ValueError, match='missing'):
        transaction.commit(tmp_path)
    assert (tmp_path / '.tofu_env.json').read_text() == 'old environment'
    assert 'PORT' not in (tmp_path / '.env').read_text()


def test_wheel_prefetch_locks_downloaded_artifacts_without_installing(tmp_path, monkeypatch):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text('tofu-search==0.10.4\n')
    house = tmp_path / 'wheelhouse'
    calls = []
    from bootstrap_pkg import python_wheels
    monkeypatch.setattr(python_wheels, 'identity', lambda python: {
        'python': 'cpython-312', 'platform': 'linux', 'machine': 'x86_64',
        'tags': ['py3-none-any']})
    monkeypatch.setattr(artifacts.shutil, 'which', lambda name: None)
    def run(command, **kwargs):
        calls.append(command)
        if '--dry-run' in command:
            (Path(command[command.index('--report') + 1])).write_text(json.dumps({'install': [{
                'metadata': {'name': 'tofu-search', 'version': '0.10.4'},
                'download_info': {'url': 'https://example.invalid/search.whl',
                                  'archive_info': {'hashes': {'sha256': 'a' * 64}}}}]}))
        else:
            assert '--require-hashes' in command and '--no-deps' in command
            wheel(Path(command[command.index('--wheel-dir') + 1]) / 'tofu_search-0.10.4-py3-none-any.whl')
    monkeypatch.setattr(artifacts.subprocess, 'run', run)
    lock = artifacts.prepare_python_wheels('python', requirements, house)
    assert 'tofu-search==0.10.4 --hash=sha256:' in lock.read_text()
    assert len(calls) == 2 and '--dry-run' in calls[0]


def test_release_manifest_requires_exact_version_and_hash(tmp_path):
    (tmp_path / 'requirements.txt').write_text('tofu-search>=0.10.4\n')
    artifact = wheel(tmp_path / 'vendor/tofu_search-0.10.4-py3-none-any.whl')
    manifest = {'schema': 1, 'artifacts': {'tofu_search': {
        'filename': artifact.name, 'version': '0.10.4', 'sha256': 'a' * 64}}}
    (artifact.parent / 'install-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='hash/version mismatch'):
        artifacts.prepare_search_wheel(tmp_path, 'unused-python')
    manifest['artifacts']['tofu_search']['sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (artifact.parent / 'install-manifest.json').write_text(json.dumps(manifest))
    assert artifacts.prepare_search_wheel(tmp_path, 'unused-python').name == artifact.name
