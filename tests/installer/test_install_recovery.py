"""Hermetic recovery contracts: real subprocesses/files, no downloads or services."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

import pytest

from bootstrap_pkg.process import OUTPUT_LIMIT, run_command
from scripts.install_frontend_dist import install_archive

# Built-graph asset paths exist only after `npm run build:frontend`;
# the audit dead-path scan must not treat them as missing source.
_AUDIT_SYNTHETIC_REPO_PATHS = {'static/vite/assets/admin.js', 'static/vite/assets/main.js', 'static/vite/assets/link'}

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_silent_command_deadline_and_stdin_are_bounded():
    started = time.monotonic()
    # The deadline only has to beat `sleep 30` while leaving the child enough
    # time to boot CPython from a FUSE checkout and print its (empty) stdin.
    rc, output = run_command([sys.executable, '-c',
        'import sys,time; print(repr(sys.stdin.read()),flush=True); time.sleep(30)'], timeout=2.5)
    assert rc == 124
    assert "''" in output
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name == 'nt', reason='POSIX session tree contract')
def test_descendant_holding_pipe_cannot_escape_deadline():
    started = time.monotonic()
    rc, _ = run_command([sys.executable, '-c',
        'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])'], timeout=.3)
    assert rc == 124
    assert time.monotonic() - started < 3


def test_flooded_output_retains_only_tail_and_redacts_credentials(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-provider-secret')
    rc, output = run_command([sys.executable, '-c',
        'print("x"*200000); print("https://user:fake-secret@example.test/simple?token=fake-token"); print("synthetic-provider-secret"); print("last-error")'])
    assert rc == 0
    assert len(output) <= OUTPUT_LIMIT
    assert 'fake-secret' not in output and 'fake-token' not in output
    assert 'synthetic-provider-secret' not in output
    assert output.endswith('last-error\n')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX session cleanup')
def test_output_callback_failure_reaps_tree_even_if_direct_child_exits(monkeypatch):
    import signal
    from types import SimpleNamespace
    from bootstrap_pkg import process
    signals = []
    child = SimpleNamespace(pid=98765, stdout=io.BytesIO(b'ready\n'), returncode=0,
                            wait=lambda **kwargs: 0)
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    def failed_sink(line):
        raise RuntimeError('sink closed')
    with pytest.raises(RuntimeError, match='sink closed'):
        run_command(['synthetic-child'], emit=failed_sink)
    assert signals == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]
    assert child.stdout.closed


@pytest.mark.parametrize('message,expected', [
    ('Connection reset by peer', 2), ('certificate verification failed', 1),
    ('No space left on device', 1), ('No matching distribution found', 1),
])
def test_retry_only_recognized_transient_failures(tmp_path, message, expected):
    counter = tmp_path / 'attempts'
    code = ('from pathlib import Path; import sys; p=Path(sys.argv[1]); '
            'p.write_text(p.read_text()+"x" if p.exists() else "x"); '
            'print(sys.argv[2]); sys.exit(1)')
    result = subprocess.run([sys.executable, str(ROOT / 'bootstrap_pkg/process.py'),
        '--timeout', '5', '--retry-network', '--', sys.executable, '-c', code,
        str(counter), message], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert len(counter.read_text()) == expected


@pytest.mark.skipif(os.name == 'nt', reason='Bash-only installer bootstrap')
def test_pre_python_watchdog_bounds_descendants_without_host_timeout():
    source = (ROOT / 'install.sh').read_text()
    helper = source[source.index('_install_run_shell() ('):source.index('# Once Python exists')]
    started = time.monotonic()
    command = helper + '\n_install_run_shell .3 "$@"\n'
    result = subprocess.run(['bash', '-c', command, 'watchdog', sys.executable, '-c',
        'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); time.sleep(30)'],
        capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert time.monotonic() - started < 4


def test_failed_conda_solver_never_deletes_existing_environment(tmp_path):
    source = (ROOT / 'install.sh').read_text()
    start = source.index('_install_main_deps() {')
    end = source.index('ok "Python dependencies installed"', start)
    calls = tmp_path / 'calls'
    script = '''
set -eu
warn() { printf '%s\\n' "$*"; }
fail() { printf '%s\\n' "$*"; exit 1; }
_install_run() { printf '%s\\n' "$*" >> "$CALLS"; return 1; }
conda() { printf 'UNEXPECTED:%s\\n' "$*" >> "$CALLS"; return 1; }
CONDA_BIN=fake-conda
ENV_NAME=existing
CONDA_ENV_PREFIX=staging
CONDA_PKGS=(quart)
_FORCE_REINSTALL=""
_CONDA_CAN_REBUILD=0
''' + source[start:end]
    result = subprocess.run(['bash', '-c', script], env=dict(os.environ, CALLS=str(calls)),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'existing environment preserved' in result.stdout
    assert 'install -p staging' in calls.read_text()
    assert 'remove' not in calls.read_text() and 'UNEXPECTED' not in calls.read_text()


def test_required_install_helpers_are_not_gitignored():
    helpers = ['scripts/install_frontend_dist.py', 'bootstrap_pkg/process.py']
    assert all((ROOT / helper).is_file() for helper in helpers)
    result = subprocess.run(['git', 'check-ignore', '--no-index', '-v', *helpers], cwd=ROOT,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode in (0, 1), result.stderr
    assert all(line.split('\t')[0].split(':', 2)[-1].startswith('!')
               for line in result.stdout.splitlines()), result.stdout


def test_frontend_install_helper_survives_public_export_policy():
    export = pytest.importorskip('export', reason='private exporter is not shipped in public source')
    assert 'scripts/install_frontend_dist.py' in export._OPENSOURCE_KEEP_FILES
    for mode in ('personal', 'internal', 'opensource'):
        assert export._should_exclude('bootstrap_pkg/process.py', 'process.py', mode) is None


def _archive(tmp_path, files, *, symlink=False):
    archive = tmp_path / 'frontend-dist-test.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        for name, value in files.items():
            entry = tarfile.TarInfo(name)
            if symlink:
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/tmp'
                bundle.addfile(entry)
            else:
                payload = value.encode()
                entry.size = len(payload)
                bundle.addfile(entry, io.BytesIO(payload))
    checksum = tmp_path / (archive.name + '.sha256')
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
    return archive, checksum


def _project(tmp_path):
    project = tmp_path / 'project'
    current = project / 'static' / 'vite'
    current.mkdir(parents=True)
    (current / 'manifest.json').write_text('previous')
    return project


def test_frontend_bad_checksum_keeps_current_graph(tmp_path):
    project = _project(tmp_path)
    archive, checksum = _archive(tmp_path, {'static/vite/manifest.json': '{}'})
    checksum.write_text('0' * 64 + '  ' + archive.name)
    with pytest.raises(ValueError, match='checksum mismatch'):
        install_archive(project, archive, checksum)
    assert (project / 'static/vite/manifest.json').read_text() == 'previous'


@pytest.mark.parametrize('name,symlink', [
    ('../../escaped', False), ('static/vite/../../.env', False),
    ('server.py', False), ('static/vite/assets/link', True),
])
def test_unsafe_frontend_archive_never_touches_checkout(tmp_path, name, symlink):
    project = _project(tmp_path)
    archive, checksum = _archive(tmp_path, {name: 'bad'}, symlink=symlink)
    with pytest.raises(ValueError, match='Unsafe'):
        install_archive(project, archive, checksum)
    assert (project / 'static/vite/manifest.json').read_text() == 'previous'
    assert list((project / 'static').iterdir()) == [project / 'static/vite']


def test_invalid_frontend_graph_keeps_previous_and_cleans_stage(tmp_path):
    project = _project(tmp_path)
    archive, checksum = _archive(tmp_path, {'static/vite/manifest.json': '{}'})
    def reject(candidate):
        assert (candidate / 'manifest.json').is_file()
        raise ValueError('missing chunk')
    with pytest.raises(ValueError, match='missing chunk'):
        install_archive(project, archive, checksum, validate=reject)
    assert (project / 'static/vite/manifest.json').read_text() == 'previous'
    assert list((project / 'static').iterdir()) == [project / 'static/vite']


def test_valid_frontend_published_after_validation(tmp_path):
    project = _project(tmp_path)
    archive, checksum = _archive(tmp_path, {'static/vite/manifest.json': 'new',
                                          'static/vite/assets/main.js': 'hello'})
    def validate(candidate):
        assert (project / 'static/vite/manifest.json').read_text() == 'previous'
        assert (candidate / 'assets/main.js').read_text() == 'hello'
    install_archive(project, archive, checksum, validate=validate)
    assert (project / 'static/vite/manifest.json').read_text() == 'new'
    assert list((project / 'static').iterdir()) == [project / 'static/vite']


@pytest.mark.parametrize('stale', [False, True])
def test_staged_frontend_uses_canonical_graph_and_source_validation(tmp_path, monkeypatch, stale):
    from lib import vite_assets
    project = _project(tmp_path)
    locales = project / 'frontend/src/i18n/locales'
    locales.mkdir(parents=True)
    for language in ('zh', 'en'):
        (locales / f'{language}.json').write_text('{}')
    monkeypatch.setattr(vite_assets, 'BASE_DIR', str(project))
    monkeypatch.setattr(vite_assets, 'VITE_AUTHORING_CONFIG_PATHS', ())
    monkeypatch.setattr(vite_assets, 'VITE_AUTHORING_SUFFIXES', frozenset({'.json'}))
    monkeypatch.setattr(vite_assets, 'I18N_LOCALE_PATHS', tuple(str(locales / f'{lang}.json') for lang in ('zh', 'en')))
    monkeypatch.setattr(vite_assets, '_build_ids', {})
    monkeypatch.setattr(vite_assets, '_build_id_manifest_key', None)
    manifest = {value: {'isEntry': True, 'file': f'assets/{name}.js'}
                for name, value in vite_assets.VITE_ENTRIES.items()}
    manifest[vite_assets.VITE_ENTRY].update({
        vite_assets.I18N_CATALOG_DIGEST_FIELD: vite_assets._source_i18n_catalog_digest(),
        vite_assets.VITE_AUTHORING_DIGEST_FIELD: '0' * 64 if stale else vite_assets._source_vite_authoring_digest(),
    })
    files = {'static/vite/manifest.json': json.dumps(manifest),
             'static/vite/assets/main.js': '// main', 'static/vite/assets/admin.js': '// admin'}
    archive, checksum = _archive(tmp_path, files)
    if stale:
        with pytest.raises(vite_assets.ViteAssetError, match='stale'):
            install_archive(project, archive, checksum)
        assert (project / 'static/vite/manifest.json').read_text() == 'previous'
    else:
        install_archive(project, archive, checksum)
        assert json.loads((project / 'static/vite/manifest.json').read_text()) == manifest
    assert list((project / 'static').iterdir()) == [project / 'static/vite']


@pytest.mark.parametrize('budget', ['MAX_ARCHIVE_BYTES', 'MAX_EXTRACT_BYTES', 'MAX_FILES'])
def test_frontend_resource_budget_rejects_before_publication(tmp_path, monkeypatch, budget):
    from scripts import install_frontend_dist
    project = _project(tmp_path)
    archive, checksum = _archive(tmp_path, {'static/vite/manifest.json': '{}'})
    monkeypatch.setattr(install_frontend_dist, budget, 0)
    with pytest.raises(ValueError):
        install_archive(project, archive, checksum)
    assert (project / 'static/vite/manifest.json').read_text() == 'previous'


def test_frontend_publication_failure_restores_previous(tmp_path, monkeypatch):
    project = _project(tmp_path)
    archive, checksum = _archive(tmp_path, {'static/vite/manifest.json': 'new'})
    original = os.replace
    def fail_new(source, destination):
        if Path(source).name == 'vite' and Path(source).parent.name == 'static' and Path(source) != project / 'static/vite':
            raise OSError('injected publication failure')
        return original(source, destination)
    monkeypatch.setattr(os, 'replace', fail_new)
    with pytest.raises(OSError, match='injected'):
        install_archive(project, archive, checksum, validate=lambda _: None)
    assert (project / 'static/vite/manifest.json').read_text() == 'previous'
