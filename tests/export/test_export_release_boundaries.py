"""Release exports keep immutable source and fail closed at publication boundaries."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')
pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'scripts/helper.mjs', 'scripts/private_only.py',
    'packages/sample/scripts/runtime.ts', 'lib/frozen.ts',
}


@pytest.fixture(autouse=True)
def isolated_export_state(monkeypatch):
    from export_pkg import _state

    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    monkeypatch.delenv('TOFU_EXPORT_SOURCE_REF', raising=False)
    monkeypatch.setenv('GIT_TERMINAL_PROMPT', '0')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)


def _git(repo: Path, *args: str, env=None) -> str:
    result = subprocess.run(
        ['git', '-c', 'user.name=export-test', '-c', 'user.email=export-test@example.invalid',
         *args], cwd=repo, env=env, capture_output=True, text=True, check=True, timeout=60,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, 'init', '-q', '-b', 'main')
    _git(path, 'config', 'user.name', 'export-test')
    _git(path, 'config', 'user.email', 'export-test@example.invalid')
    return path


def _commit(repo: Path) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'synthetic source')
    return _git(repo, 'rev-parse', 'HEAD')


def _local_publish(tmp_path, monkeypatch):
    import export_pkg._publish as publish

    repo = _repo(tmp_path / 'public')
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', '-q', str(remote))
    (repo / 'README.md').write_text('Public source fixture.\n', encoding='utf-8')
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [{'name': 'origin', 'url': str(remote)}],
    })
    return publish, repo, remote


def _runner(repo: Path, calls: list):
    def run(cmd):
        calls.append(cmd)
        result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout or 'synthetic git failure')
        return result.stdout.strip()
    return run


def test_opensource_tar_manifest_excludes_private_root_scripts_and_restores_only_allowlist(
        tmp_path, monkeypatch):
    import export_pkg._export_core as core
    import export_pkg._pipeline as pipeline
    from export_pkg import _state
    from export_pkg._excludes import _should_exclude
    from export_pkg._tar import _stream_tar_copy

    source = _repo(tmp_path / 'source')
    files = {
        '.gitignore': '/scripts/*\n',
        'README.md': 'Public fixture.\n',
        'scripts/helper.mjs': 'export const kept = true;\n',
        'scripts/private_only.py': 'PRIVATE = True\n',
        'packages/sample/scripts/runtime.ts': 'export const nested = true;\n',
    }
    for rel, content in files.items():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    _git(source, 'add', '-f', *files)
    _git(source, 'commit', '-qm', 'tracked private and public fixture')
    monkeypatch.setattr(pipeline, 'ROOT', source)
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES', {'scripts/helper.mjs'})
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', _git(source, 'rev-parse', 'HEAD'))
    dest = tmp_path / 'exported'
    dest.mkdir()
    excludes, _ = pipeline._build_tar_excludes_for_mode('opensource', dest)
    archive = tmp_path / 'manifest.tar'
    subprocess.run(
        ['tar', 'cf', str(archive), *excludes, '-C', str(source), '.'],
        check=True, capture_output=True, timeout=60,
    )
    with tarfile.open(archive) as tf:
        manifest = {member.name.removeprefix('./') for member in tf if member.isfile()}
    print('actual tar manifest:', ', '.join(sorted(manifest)))
    assert manifest == {
        rel for rel in files if _should_exclude(rel, Path(rel).name, 'opensource') is None
    }
    assert 'scripts/private_only.py' not in manifest
    assert 'scripts/helper.mjs' not in manifest
    assert 'packages/sample/scripts/runtime.ts' in manifest

    _stream_tar_copy(source, dest, excludes)
    core._restore_export_kept_files(dest, 'opensource')
    shipped_scripts = {path.relative_to(dest).as_posix()
                       for path in (dest / 'scripts').rglob('*') if path.is_file()}
    assert shipped_scripts == {'scripts/helper.mjs'}
    assert (dest / 'scripts/helper.mjs').read_text() == files['scripts/helper.mjs']
    assert '!/scripts/helper.mjs' in (dest / '.gitignore').read_text()


@pytest.mark.parametrize('mode', ('internal', 'opensource'))
def test_retained_helper_uses_pinned_sha_not_live_head_or_dirty_bytes(tmp_path, monkeypatch, mode):
    import export_pkg._export_core as core
    from export_pkg import _state

    source = _repo(tmp_path / 'source')
    helper = source / 'scripts/helper.mjs'
    helper.parent.mkdir()
    helper.write_text('export const source = "frozen";\n', encoding='utf-8')
    helper.chmod(0o755)
    pinned = _commit(source)
    helper.write_text('export const source = "later-head";\n', encoding='utf-8')
    _commit(source)
    helper.write_text('export const source = "dirty-live";\n', encoding='utf-8')
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES', {'scripts/helper.mjs'})
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', pinned)
    dest = tmp_path / 'exported'
    dest.mkdir()

    core._restore_export_kept_files(dest, mode)

    restored = dest / 'scripts/helper.mjs'
    assert restored.read_text() == 'export const source = "frozen";\n'
    assert restored.stat().st_mode & 0o111
    assert helper.read_text() == 'export const source = "dirty-live";\n'


@pytest.mark.parametrize('missing', ('blob', 'commit'))
def test_retained_helper_with_given_sha_fails_closed_instead_of_reading_live(
        tmp_path, monkeypatch, missing):
    import export_pkg._export_core as core
    from export_pkg import _state
    from export_pkg._verify import ExportIntegrityError

    source = _repo(tmp_path / 'source')
    (source / 'README.md').write_text('Public fixture.\n')
    pinned = _commit(source)
    (source / 'scripts').mkdir()
    (source / 'scripts/helper.mjs').write_text('export const dirty = true;\n')
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES', {'scripts/helper.mjs'})
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', pinned if missing == 'blob' else 'f' * 40)
    dest = tmp_path / 'exported'
    dest.mkdir()

    with pytest.raises(ExportIntegrityError, match='retained source'):
        core._restore_export_kept_files(dest, 'opensource')
    assert not (dest / 'scripts/helper.mjs').exists()


def test_frozen_export_does_not_apply_live_untracked_excludes(tmp_path, monkeypatch):
    import export_pkg._pipeline as pipeline

    source = _repo(tmp_path / 'source')
    (source / 'lib').mkdir()
    (source / 'lib/core.py').write_text('VALUE = 1\n')
    _commit(source)
    (source / 'lib/frozen.ts').write_text('export const frozen = true;\n')
    env = {**os.environ, 'GIT_INDEX_FILE': str(tmp_path / 'frozen.index')}
    _git(source, 'read-tree', 'HEAD', env=env)
    _git(source, 'add', 'lib/frozen.ts', env=env)
    tree = _git(source, 'write-tree', env=env)
    pinned = _git(source, 'commit-tree', tree, '-p', 'HEAD', '-m', 'frozen candidate', env=env)
    monkeypatch.setenv('TOFU_EXPORT_SOURCE_REF', pinned)
    monkeypatch.setattr(pipeline, 'ROOT', source)
    dest = tmp_path / 'exported'

    pipeline._export_via_tar_with_sanitize('opensource', dest)

    assert (dest / 'lib/frozen.ts').read_text() == 'export const frozen = true;\n'
    assert 'lib/frozen.ts' in _git(source, 'ls-files', '--others', '--exclude-standard')


@pytest.mark.parametrize('name', (
    'source.ts', 'source.tsx', 'source.rs', 'source.mjs', 'source.cjs', 'source.svg',
    'source.sql', 'source.lock', 'source.swift', 'source.kt', 'source.future', 'launcher',
))
def test_utf8_source_sanitization_is_not_limited_by_filename(tmp_path, name):
    from export_pkg._excludes import _is_text_file
    from export_pkg._sanitize import _post_copy_sanitize

    account = 'hadoop' + '-aipnlp'
    target = tmp_path / name
    target.write_text('// λ account=' + account + '\n', encoding='utf-8')
    assert _is_text_file(str(target))
    result = _post_copy_sanitize(tmp_path, 'opensource')
    assert result['sanitized'] == 1
    assert account not in target.read_text(encoding='utf-8')


@pytest.mark.parametrize('name', ('source.ts', 'image.svg', 'artifact.blob', 'launcher'))
def test_secret_gate_scans_all_candidate_bytes_even_when_not_utf8_text(tmp_path, name):
    import export_pkg._publish as publish

    (tmp_path / name).write_bytes(b'\xff\x00' + b'ghp_' + b'A' * 36 + b'\x00\xfe')
    with pytest.raises(publish.ExportPublishSafetyError, match='1 leak'):
        publish._verify_opensource(tmp_path)


def test_blob_secret_gate_reports_label_not_secret_bytes(capsys):
    import export_pkg._publish as publish

    token = b'ghp_' + b'A' * 36
    with pytest.raises(publish.ExportPublishSafetyError, match='1 leak') as error:
        publish._verify_opensource_blob(b'\xff\n' + token, 'synthetic blob')
    output = capsys.readouterr().out
    assert 'synthetic blob:2' in output
    assert token.decode() not in output
    assert token.decode() not in str(error.value)
    publish._verify_opensource_blob(b'\xff\x00safe bytes\xfe', 'safe blob')


@pytest.mark.parametrize('attribute_source', ('tracked', 'info'))
def test_staged_export_ignore_cannot_hide_real_secret_blob(tmp_path, attribute_source):
    import export_pkg._publish as publish

    repo = _repo(tmp_path / 'public')
    (repo / 'hidden.ts').write_bytes(b'ghp_' + b'A' * 36 + b'\n')
    attrs = repo / '.gitattributes' if attribute_source == 'tracked' else repo / '.git/info/attributes'
    attrs.write_text('hidden.ts export-ignore\n')
    _git(repo, 'add', '-A')
    (repo / 'hidden.ts').write_text('export const nowSafe = true;\n')

    with pytest.raises(publish.ExportPublishSafetyError, match='1 leak'):
        publish._verify_staged_publish_snapshot(repo)


def test_staged_snapshot_retains_raw_export_ignore_and_export_subst_blobs(tmp_path, monkeypatch):
    import export_pkg._publish as publish

    repo = _repo(tmp_path / 'public')
    expected = {
        '.gitattributes': b'hidden.ts export-ignore\nsubst.ts export-subst\n',
        'hidden.ts': b'export const hidden = true;\r\n',
        'subst.ts': b'// $Format:%H$\r\nexport const raw = true;\n',
    }
    for name, content in expected.items():
        (repo / name).write_bytes(content)
    _git(repo, 'add', '-A')
    observed = {}

    def inspect_snapshot(snapshot):
        observed.update({path.relative_to(snapshot).as_posix(): path.read_bytes()
                         for path in snapshot.rglob('*') if path.is_file()})

    monkeypatch.setattr(publish, '_verify_opensource', inspect_snapshot)
    verified = publish._verify_staged_publish_snapshot(repo)
    assert observed == expected
    assert verified == _git(repo, 'write-tree')


@pytest.mark.parametrize('raw_path', (
    b'../escape.ts', b'/absolute.ts', b'.git/config', b'pkg\\escape.ts',
    b'pkg//escape.ts', b'pkg/./escape.ts', b'pkg/line\nbreak.ts', b'pkg/\xff.ts',
))
def test_staged_snapshot_rejects_unsafe_index_paths(tmp_path, monkeypatch, raw_path):
    import export_pkg._publish as publish

    repo = _repo(tmp_path / 'public')
    (repo / 'README.md').write_text('Public fixture.\n')
    _git(repo, 'add', '-A')
    oid = _git(repo, 'rev-parse', ':README.md').encode()
    real_run = publish.subprocess.run

    def unsafe_index(cmd, **kwargs):
        if cmd[:4] == ['git', 'ls-files', '-s', '-z']:
            return subprocess.CompletedProcess(cmd, 0, b'100644 ' + oid + b' 0\t' + raw_path + b'\0', b'')
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(publish.subprocess, 'run', unsafe_index)
    with pytest.raises(publish.ExportPublishSafetyError, match='unsafe Git paths'):
        publish._verify_staged_publish_snapshot(repo)


@pytest.mark.parametrize('failed_command', ('commit', 'push', 'tag'))
def test_git_push_propagates_actual_command_failures(tmp_path, monkeypatch, failed_command):
    publish, repo, remote = _local_publish(tmp_path, monkeypatch)
    real_run = publish.subprocess.run
    monkeypatch.setattr(publish, '_read_version', lambda: '9.9.9')

    def fail_command(cmd, **kwargs):
        if cmd[:2] == ['git', failed_command]:
            return subprocess.CompletedProcess(cmd, 42, '', 'synthetic ' + failed_command + ' blocked')
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(publish.subprocess, 'run', fail_command)
    with pytest.raises(RuntimeError, match='synthetic ' + failed_command + ' blocked'):
        publish._git_push(repo, 'opensource', is_release=failed_command == 'tag')
    assert _git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads') == ''


def test_unchanged_export_retries_push_after_previous_push_failure(tmp_path, monkeypatch):
    publish, repo, remote = _local_publish(tmp_path, monkeypatch)

    def fail_push(*args, **kwargs):
        raise RuntimeError('synthetic first push failed')

    with monkeypatch.context() as failure:
        failure.setattr(publish, '_push_branch', fail_push)
        with contextlib.suppress(RuntimeError):
            publish._git_push(repo, 'opensource')
    committed = _git(repo, 'rev-parse', 'HEAD')
    assert _git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads') == ''

    publish._git_push(repo, 'opensource')

    assert _git(repo, 'rev-parse', 'HEAD') == committed
    assert _git(remote, 'rev-parse', 'refs/heads/main') == committed


@pytest.mark.parametrize('annotated', (False, True))
def test_remote_tag_same_peeled_commit_is_idempotent(tmp_path, annotated):
    import export_pkg._publish as publish

    repo = _repo(tmp_path / 'public')
    (repo / 'README.md').write_text('Public fixture.\n')
    _commit(repo)
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', '-q', str(remote))
    if annotated:
        _git(repo, 'tag', '-a', 'v9.9.9', '-m', 'synthetic release')
    else:
        _git(repo, 'tag', 'v9.9.9')
    _git(repo, 'push', str(remote), 'refs/tags/v9.9.9')
    before = _git(remote, 'rev-parse', 'refs/tags/v9.9.9')
    calls = []

    publish._push_tag(_runner(repo, calls), str(remote), 'v9.9.9')

    assert not any(cmd[1] == 'push' for cmd in calls)
    assert _git(remote, 'rev-parse', 'refs/tags/v9.9.9') == before


@pytest.mark.parametrize('force', (False, True))
def test_published_tag_conflict_is_fatal_and_never_moves_even_with_force(tmp_path, force):
    import export_pkg._publish as publish

    repo = _repo(tmp_path / 'public')
    (repo / 'README.md').write_text('First public fixture.\n')
    old_commit = _commit(repo)
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', '-q', str(remote))
    _git(repo, 'push', str(remote), 'HEAD:refs/tags/v9.9.9')
    (repo / 'README.md').write_text('Second public fixture.\n')
    _commit(repo)
    _git(repo, 'tag', '-a', 'v9.9.9', '-m', 'synthetic conflicting release')
    calls = []

    with pytest.raises(publish.ExportPublishSafetyError, match='published tag'):
        publish._push_tag(_runner(repo, calls), str(remote), 'v9.9.9', force=force)
    assert not any('--force' in cmd for cmd in calls)
    assert _git(remote, 'rev-parse', 'refs/tags/v9.9.9^{commit}') == old_commit


@pytest.mark.parametrize('failed_command', ('ls-remote', 'push'))
def test_tag_probe_and_push_errors_are_fatal(failed_command):
    import export_pkg._publish as publish

    def run(cmd):
        if cmd[1] == failed_command:
            raise RuntimeError('synthetic tag ' + failed_command + ' failed')
        return '' if cmd[1] == 'ls-remote' else 'a' * 40

    with pytest.raises(RuntimeError, match='synthetic tag ' + failed_command + ' failed'):
        publish._push_tag(run, 'origin', 'v9.9.9')


def test_git_push_does_not_delete_or_retarget_an_existing_local_release_tag(tmp_path, monkeypatch):
    publish, repo, remote = _local_publish(tmp_path, monkeypatch)
    old_commit = _commit(repo)
    _git(repo, 'tag', '-a', 'v9.9.9', '-m', 'synthetic previous release')
    tag_object = _git(repo, 'rev-parse', 'refs/tags/v9.9.9')
    (repo / 'README.md').write_text('Changed public fixture.\n')
    monkeypatch.setattr(publish, '_read_version', lambda: '9.9.9')

    with pytest.raises(publish.ExportPublishSafetyError, match='existing local release tag'):
        publish._git_push(repo, 'opensource', is_release=True, force=True)
    assert _git(repo, 'rev-parse', 'refs/tags/v9.9.9') == tag_object
    assert _git(repo, 'rev-parse', 'refs/tags/v9.9.9^{commit}') == old_commit
    assert _git(remote, 'for-each-ref', '--format=%(refname)') == ''


@pytest.mark.parametrize('mode, push, worktree', (
    ('internal', True, False), ('internal', True, True), ('internal', False, False),
    ('personal', True, False), ('personal', False, False),
))
def test_handoff_dry_run_only_previews_before_any_export_push_or_probe(
        tmp_path, monkeypatch, capsys, mode, push, worktree):
    import export_pkg._handoff as handoff
    import export_pkg._orchestrate as orchestrate

    source = tmp_path / 'source'
    source.mkdir()
    out = tmp_path / 'handoff'
    export_dest = tmp_path / 'exported'
    calls = []
    monkeypatch.setattr(handoff, 'ROOT', source)
    monkeypatch.setattr(handoff, '_INTERNAL_EXPORT_DEST', export_dest)
    monkeypatch.setattr(handoff, '_current_branch', lambda: 'main')
    monkeypatch.setattr(handoff, '_dirty_tracked_files', lambda root: [])
    monkeypatch.setattr(orchestrate, 'export_project', lambda *a, **k: calls.append('export'))
    monkeypatch.setattr(handoff, '_snapshot_push', lambda *a: calls.append('snapshot-push'))
    monkeypatch.setattr(handoff, '_push_head', lambda *a: calls.append('push-head'))
    monkeypatch.setattr(handoff, '_remote_branch_exists', lambda *a: calls.append('probe'))
    monkeypatch.setattr(handoff, '_stage_worktree_untracked', lambda *a: calls.append('stage'))
    monkeypatch.setattr(handoff, '_write_state_tarball', lambda *a: calls.append('tarball'))

    handoff.run_handoff(mode, out, str(tmp_path / 'remote.git'),
                        push=push, dry_run=True, worktree=worktree)

    assert calls == []
    assert not out.exists()
    assert not export_dest.exists()
    assert 'dry run' in capsys.readouterr().out


def test_snapshot_sanitization_version_comes_from_the_same_frozen_source(tmp_path, monkeypatch):
    import export_pkg._pipeline as pipeline
    from export_pkg import _state

    source = _repo(tmp_path / 'source')
    (source / 'VERSION').write_text('1.2.3\n')
    _commit(source)
    (source / 'VERSION').write_text('9.9.9\n')
    monkeypatch.setattr(pipeline, 'ROOT', source)
    monkeypatch.setattr(_state, '_EXPORT_VERSION', '9.9.9')
    seen = []

    def sanitize(dest, mode):
        seen.append(_state._EXPORT_VERSION)
        return {'sanitized': 0}

    monkeypatch.setattr(pipeline, '_post_copy_sanitize', sanitize)
    pipeline._export_via_tar_with_sanitize('opensource', tmp_path / 'exported')
    assert seen == ['1.2.3']
    assert (source / 'VERSION').read_text() == '9.9.9\n'


def test_mutable_export_clears_previous_snapshot_pin(tmp_path, monkeypatch):
    import export_pkg._pipeline as pipeline
    from export_pkg import _state

    source = _repo(tmp_path / 'source')
    (source / 'README.md').write_text('Public fixture.\n')
    _commit(source)
    monkeypatch.setattr(pipeline, 'ROOT', source)
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', 'a' * 40)
    pipeline._export_via_tar_with_sanitize('internal', tmp_path / 'exported', worktree=True)
    assert _state._EXPORT_SOURCE_SHA is None
