"""Executable public-history publication contract using private synthetic repos."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    is_opensource_build(), reason='The private publication toolchain is not shipped')]


def git(repo: Path, *args: str, env=None) -> str:
    result = subprocess.run(['git', *args], cwd=repo, capture_output=True,
                            text=True, check=True, env=env)
    return result.stdout.strip()


def init(repo: Path) -> Path:
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Public Contributor')
    git(repo, 'config', 'user.email', 'contributor@example.org')
    return repo


def commit(repo: Path, name: str, files: dict[str, str], *, date=None) -> str:
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(repo, 'add', '-A')
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    git(repo, 'commit', '-qm', name, env=env)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture()
def repositories(tmp_path):
    source = init(tmp_path / 'source')
    base = commit(source, 'baseline', {'README.md': 'baseline\n', 'VERSION': '1.0.0\n'})
    public = init(tmp_path / 'public')
    original = commit(public, 'Previous public release', {
        'README.md': 'baseline\n', 'VERSION': '1.0.0\n'})
    return source, public, base, original


def candidate(public: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        target = public / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(public, 'add', '-A')
    return git(public, 'write-tree')


def sync(source, public, tip, tree, *, base=None):
    from export_pkg._history import synchronize_public_history
    return synchronize_public_history(source, public, tip, tree, history_base=base)


def test_preserves_separate_side_commits_merge_and_author_dates(repositories):
    source, public, base, original = repositories
    git(source, 'checkout', '-qb', 'feature')
    side = commit(source, 'feat: side work\n\nWhy the side change matters.',
                  {'side.py': 'SIDE = 1\n'}, date='2025-01-03T09:00:00+00:00')
    git(source, 'checkout', '-q', 'main')
    main = commit(source, 'fix: main work', {'main.py': 'MAIN = 1\n'},
                  date='2025-01-01T09:00:00+00:00')
    git(source, 'merge', '--no-ff', '-qm', 'Merge tested feature', 'feature')
    tip = git(source, 'rev-parse', 'HEAD')
    tree = candidate(public, {'side.py': 'SIDE = 1\n', 'main.py': 'MAIN = 1\n'})
    result = sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD^{tree}') == tree
    assert git(public, 'merge-base', '--is-ancestor', original, 'HEAD') == ''
    assert git(public, 'show', '-s', '--format=%B', result.mapping[side]).startswith(
        'feat: side work\n\nWhy the side change matters.')
    assert git(public, 'show', '-s', '--format=%aI', result.mapping[side]) == \
        git(source, 'show', '-s', '--format=%aI', side)
    assert git(public, 'show', '-s', '--format=%an <%ae>', result.mapping[side]) == \
        'Public Contributor <contributor@example.org>'
    parents = git(public, 'show', '-s', '--format=%P', result.mapping[tip]).split()
    assert parents == [result.mapping[main], result.mapping[side]]
    assert git(public, 'show', result.mapping[side] + ':side.py') == 'SIDE = 1'
    assert git(public, 'show', result.mapping[main] + ':main.py') == 'MAIN = 1'
    assert not (public / '.git/objects/info/alternates').exists()
    assert subprocess.run(['git', 'cat-file', '-e', tip], cwd=public,
                          capture_output=True).returncode != 0


def test_incremental_cursor_deduplicates_and_ignores_forged_trailer(repositories):
    source, public, base, _ = repositories
    first = commit(source, 'first\n\nSource-Commit: ' + '0' * 40,
                   {'one.py': 'ONE = 1\n'})
    tree = candidate(public, {'one.py': 'ONE = 1\n'})
    result = sync(source, public, first, tree, base=base)
    rerun = sync(source, public, first, tree)
    assert rerun.public_tip == result.public_tip
    second = commit(source, 'second', {'two.py': 'TWO = 2\n'})
    tree2 = candidate(public, {'two.py': 'TWO = 2\n'})
    continued = sync(source, public, second, tree2)
    assert continued.mapping[first] == result.mapping[first]
    assert git(public, 'show', '-s', '--format=%P', continued.mapping[second]) == result.public_tip
    assert git(public, 'log', '--format=%s').splitlines().count('first') == 1
    cursor = json.loads((public / '.git/tofu-export-history.json').read_text())
    assert cursor['source_tip'] == second
    assert cursor['public_tip'] == continued.public_tip
    assert 'Source-Commit:' not in (public / 'README.md').read_text()


def test_internal_only_commit_is_folded_without_publishing_private_file(repositories):
    source, public, base, _ = repositories
    changed = commit(source, 'public fix', {'safe.py': 'SAFE = 1\n'})
    tip = commit(source, 'Private service configuration', {'bundles/private.json': '{}\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    result = sync(source, public, tip, tree, base=base)
    assert result.mapping[tip] == result.mapping[changed]
    assert result.skipped >= 1
    assert 'bundles/private.json' not in git(public, 'ls-tree', '-r', '--name-only', 'HEAD')
    assert 'Private service configuration' not in git(public, 'log', '--format=%s')


@pytest.mark.parametrize('where', ['ancestor', 'message', 'hidden-attribute', 'binary'])
def test_secret_in_any_new_history_object_blocks_before_ref_advancement(repositories, where):
    source, public, base, original = repositories
    token = 'ghp_' + 'A' * 36
    files = {'bad.ts': 'const key = "' + token + '";\n'}
    message = 'unsafe intermediate'
    if where == 'message':
        files = {'safe.py': 'SAFE = 1\n'}
        message += '\n\n' + token
    elif where == 'hidden-attribute':
        files['.gitattributes'] = 'bad.ts export-ignore\n'
    elif where == 'binary':
        files = {'blob.unknown': '\x00' + token}
    commit(source, message, files)
    for path in files:
        (source / path).unlink()
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Remove intermediate content')
    tip = git(source, 'rev-parse', 'HEAD')
    tree = candidate(public, {})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original
    assert not (public / '.git/tofu-export-history.json').exists()


def test_missing_initial_baseline_and_cursor_tampering_fail_closed(repositories):
    source, public, base, original = repositories
    tip = commit(source, 'safe', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError, match='history-base'):
        sync(source, public, tip, tree)
    assert git(public, 'rev-parse', 'HEAD') == original
    result = sync(source, public, tip, tree, base=base)
    cursor = public / '.git/tofu-export-history.json'
    document = json.loads(cursor.read_text())
    document['public_tip'] = original
    cursor.write_text(json.dumps(document))
    with pytest.raises(ExportPublishSafetyError, match='cursor'):
        sync(source, public, tip, tree)
    assert git(public, 'rev-parse', 'HEAD') == result.public_tip


def test_known_internal_metadata_and_typescript_are_sanitized(repositories):
    source, public, base, _ = repositories
    tip = commit(source, 'fix: internal endpoint\n\nSee https://api.openai.com/help', {
        'safe.ts': 'const url = "https://api.openai.com/v1";\n'})
    tree = candidate(public, {'safe.ts': 'const url = "https://your-llm-gateway.example.com/v1";\n'})
    result = sync(source, public, tip, tree, base=base)
    assert 'internal.example.com' not in git(public, 'show', result.mapping[tip] + ':safe.ts')
    assert 'internal.example.com' not in git(public, 'show', '-s', '--format=%B', result.mapping[tip])


def test_internal_tail_after_reconcile_does_not_create_false_source_commit(repositories):
    source, public, base, _ = repositories
    first = commit(source, 'public change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n', 'generated.js': 'GENERATED = 1;\n'})
    initial = sync(source, public, first, tree, base=base)
    assert git(public, 'show', '-s', '--format=%s', initial.mapping[first]) == 'public change'
    tip = commit(source, 'Private-only tail', {'bundles/private.json': '{}\n'})
    continued = sync(source, public, tip, tree)
    assert continued.public_tip == initial.public_tip
    assert 'Private-only tail' not in git(public, 'log', '--format=%s')
    next_tip = commit(source, 'next public change', {'next.py': 'NEXT = 1\n'})
    next_tree = candidate(public, {'next.py': 'NEXT = 1\n'})
    final = sync(source, public, next_tip, next_tree)
    assert git(public, 'merge-base', '--is-ancestor', initial.public_tip, final.public_tip) == ''
    assert git(public, 'show', '-s', '--format=%s', final.mapping[next_tip]) == 'next public change'


def test_raw_private_clone_is_not_a_public_anchor(repositories, tmp_path):
    source, _public, base, _ = repositories
    private = commit(source, 'private original', {'export_pkg/private.txt': 'private markers\n'})
    clone = tmp_path / 'raw-clone'
    subprocess.run(['git', 'clone', '-q', '--no-local', str(source), str(clone)], check=True)
    tip = commit(source, 'public change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(clone, {'safe.py': 'SAFE = 1\n'})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError, match='private|source'):
        sync(source, clone, tip, tree, base=base)
    assert git(clone, 'rev-parse', 'HEAD') == private


@pytest.mark.parametrize('archive_kind', ['deep', 'duplicate', 'private-member'])
def test_archive_members_are_fully_verified_before_history_publication(repositories, archive_kind):
    import io
    import warnings
    import zipfile

    def zip_bytes(members):
        stream = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in members:
                    archive.writestr(name, data)
        return stream.getvalue()

    source, public, base, original = repositories
    if archive_kind == 'deep':
        value = ('ghp_' + 'A' * 36).encode()
        for _ in range(5):
            value = zip_bytes([('member.zip', value)])
    elif archive_kind == 'duplicate':
        value = zip_bytes([('safe.txt', b'one'), ('safe.txt', b'two')])
    else:
        value = zip_bytes([('.env', b'PRIVATE=value')])
    (source / 'asset.zip').write_bytes(value)
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'archive change')
    tip = git(source, 'rev-parse', 'HEAD')
    tree = candidate(public, {})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original


def test_cursor_settlement_failure_retries_without_duplicate_commits(repositories, monkeypatch):
    from export_pkg import _history as history
    source, public, base, original = repositories
    tip = commit(source, 'source change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    original_write = history._write_state

    def crash_settlement(path, value):
        if path.name == 'tofu-export-history.json':
            raise OSError('synthetic lost settlement acknowledgement')
        return original_write(path, value)

    monkeypatch.setattr(history, '_write_state', crash_settlement)
    with pytest.raises(OSError, match='settlement'):
        sync(source, public, tip, tree, base=base)
    published = git(public, 'rev-parse', 'HEAD')
    assert published != original
    count = git(public, 'rev-list', '--count', 'HEAD')
    monkeypatch.setattr(history, '_write_state', original_write)
    retry = sync(source, public, tip, tree, base=base)
    assert retry.public_tip == published
    assert git(public, 'rev-list', '--count', 'HEAD') == count
    assert not (public / '.git/tofu-export-history.pending.json').exists()


def test_unowned_lock_file_does_not_block_after_process_exit(repositories):
    source, public, base, _ = repositories
    tip = commit(source, 'source change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    (public / '.git/tofu-export-history.lock').touch()
    result = sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD^{tree}') == tree
    assert result.created >= 1


def bare(repo: Path) -> Path:
    repo.mkdir()
    subprocess.run(['git', 'init', '--bare', '-q', str(repo)], check=True)
    return repo


def test_history_publication_partial_remote_failure_retries_exact_ref(repositories, tmp_path, monkeypatch):
    from export_pkg import _publish as publish
    source, public, base, _ = repositories
    remote1, remote2 = bare(tmp_path / 'remote1'), bare(tmp_path / 'remote2')
    for remote in (remote1, remote2):
        git(public, 'push', str(remote), 'main')
    tip = commit(source, 'feature with review detail\n\nReason for this change.', {'safe.py': 'SAFE = 1\n'})
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [
            {'name': 'one', 'url': str(remote1)}, {'name': 'two', 'url': str(remote2)}]})
    original_push = publish._push_branch
    failed = False

    def push(run, remote, branch, **kwargs):
        nonlocal failed
        if remote == 'two' and not failed:
            failed = True
            raise RuntimeError('synthetic second remote failure')
        return original_push(run, remote, branch, **kwargs)

    monkeypatch.setattr(publish, '_push_branch', push)

    def builder(destination, tree, message, *, published_tip=None):
        from export_pkg._history import synchronize_public_history
        return synchronize_public_history(source, destination, tip, tree,
                                          history_base=base, published_tip=published_tip).public_tip

    with pytest.raises(RuntimeError, match='second remote'):
        publish._git_push(public, 'opensource', history_builder=builder)
    published = git(public, 'rev-parse', 'HEAD')
    assert git(remote1, 'rev-parse', 'refs/heads/main') == published
    assert git(remote2, 'rev-parse', 'refs/heads/main') != published
    count = git(public, 'rev-list', '--count', 'HEAD')
    publish._git_push(public, 'opensource', history_builder=builder)
    assert git(remote1, 'rev-parse', 'refs/heads/main') == published
    assert git(remote2, 'rev-parse', 'refs/heads/main') == published
    assert git(public, 'rev-list', '--count', 'HEAD') == count


def test_history_release_tag_uses_verified_version_not_live_source(repositories, tmp_path, monkeypatch):
    from export_pkg import _publish as publish
    source, public, base, _ = repositories
    remote = bare(tmp_path / 'remote')
    git(public, 'push', str(remote), 'main')
    tip = commit(source, 'prepare release', {'safe.py': 'SAFE = 1\n'})
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [{'name': 'origin', 'url': str(remote)}]})
    monkeypatch.setattr(publish, '_read_version', lambda: '9.9.9')

    def builder(destination, tree, message, *, published_tip=None):
        from export_pkg._history import synchronize_public_history
        return synchronize_public_history(source, destination, tip, tree,
                                          history_base=base, published_tip=published_tip).public_tip

    publish._git_push(public, 'opensource', is_release=True, history_builder=builder)
    assert git(remote, 'rev-parse', 'refs/tags/v1.0.0^{commit}') == git(public, 'rev-parse', 'HEAD')
    assert git(public, 'tag', '--list', 'v9.9.9') == ''


def test_foreign_remote_advance_does_not_ours_merge_or_push_another_candidate(repositories, tmp_path, monkeypatch):
    from export_pkg import _publish as publish
    from export_pkg._publish import ExportPublishSafetyError
    source, public, base, original = repositories
    remote = bare(tmp_path / 'remote')
    git(public, 'push', str(remote), 'main')
    foreign = init(tmp_path / 'foreign')
    foreign_tip = commit(foreign, 'foreign changes', {'foreign.py': 'FOREIGN = 1\n'})
    git(foreign, 'push', '--force', str(remote), 'main')
    tip = commit(source, 'safe source change', {'safe.py': 'SAFE = 1\n'})
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [{'name': 'origin', 'url': str(remote)}]})

    def builder(destination, tree, message, *, published_tip=None):
        from export_pkg._history import synchronize_public_history
        return synchronize_public_history(source, destination, tip, tree,
                                          history_base=base, published_tip=published_tip).public_tip

    with pytest.raises(ExportPublishSafetyError, match='advance|candidate|ancestor|diverg'):
        publish._git_push(public, 'opensource', history_builder=builder)
    assert git(remote, 'rev-parse', 'refs/heads/main') == foreign_tip
    assert git(public, 'merge-base', '--is-ancestor', original, 'HEAD') == ''
    assert 'Preserve ' not in git(public, 'log', '--format=%s')


def test_unrelated_source_root_keeps_no_implicit_fast_import_parent(repositories):
    source, public, base, _ = repositories
    git(source, 'checkout', '--orphan', 'foreign')
    git(source, 'rm', '-rf', '.')
    root = commit(source, 'independent public root', {'VERSION': '1.0.0\n', 'foreign.py': 'FOREIGN = 1\n'})
    git(source, 'checkout', 'main')
    git(source, 'merge', '--allow-unrelated-histories', '--no-ff', '-qm', 'Merge independent source root', 'foreign')
    tip = git(source, 'rev-parse', 'HEAD')
    tree = candidate(public, {'foreign.py': 'FOREIGN = 1\n'})
    result = sync(source, public, tip, tree, base=base)
    assert git(public, 'show', '-s', '--format=%P', result.mapping[root]) == ''
    assert git(public, 'show', '-s', '--format=%P', result.mapping[tip]).split() == [
        result.mapping[base], result.mapping[root]]


@pytest.mark.parametrize('private_path', ['.env.production', 'notes/live.sqlite', 'fixtures/server_config.json'])
def test_forbidden_private_path_cannot_enter_a_removed_ancestor(repositories, private_path):
    source, public, base, original = repositories
    commit(source, 'private path', {private_path: 'synthetic value\n'})
    (source / private_path).unlink()
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'remove private path')
    tree = candidate(public, {})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError, match='private|forbidden'):
        sync(source, public, git(source, 'rev-parse', 'HEAD'), tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original


def test_unverified_non_zip_compression_is_rejected(repositories):
    import gzip
    source, public, base, original = repositories
    (source / 'asset.tar.gz').write_bytes(gzip.compress(('ghp_' + 'A' * 36).encode()))
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'compressed payload')
    tip = git(source, 'rev-parse', 'HEAD')
    tree = candidate(public, {})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError, match='archive|compression'):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original


def test_replace_object_cannot_hide_the_real_staged_secret(repositories):
    from export_pkg import _publish as publish
    from export_pkg._publish import ExportPublishSafetyError
    _source, public, _base, original = repositories
    (public / 'secret.txt').write_text('ghp_' + 'A' * 36 + '\n')
    git(public, 'add', '-A')
    unsafe = git(public, 'rev-parse', ':secret.txt')
    clean = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], cwd=public,
                                    input=b'safe replacement\n').decode().strip()
    git(public, 'replace', unsafe, clean)
    with pytest.raises(ExportPublishSafetyError, match='replace|secret|leak'):
        publish._verify_staged_publish_snapshot(public)
    assert git(public, 'rev-parse', 'HEAD') == original


def test_first_history_anchor_accepts_declared_runtime_placeholders(repositories):
    source, public, base, _ = repositories
    commit(public, 'Public placeholders', {'data/.gitkeep': '', 'logs/.gitkeep': ''})
    tip = commit(source, 'public change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    result = sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD^{tree}') == tree
    assert result.created > 0


@pytest.mark.parametrize('header', ['gpgsig', 'mergetag', 'extension-header'])
def test_unpublished_anchor_scans_exact_commit_headers(repositories, header):
    source, public, base, _ = repositories
    raw = subprocess.check_output(['git', 'cat-file', 'commit', 'HEAD'], cwd=public)
    secret_header = header.encode() + b' synthetic\n ' + ('ghp_' + 'A' * 36).encode()
    raw = raw.replace(b'\n\n', b'\n' + secret_header + b'\n\n', 1)
    unsafe = subprocess.check_output(['git', 'hash-object', '-w', '-t', 'commit', '--stdin'],
                                     cwd=public, input=raw).decode().strip()
    git(public, 'update-ref', 'refs/heads/main', unsafe)
    tip = commit(source, 'safe change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError, match='safety|leak|metadata'):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == unsafe


def test_separate_git_directory_cannot_share_source_object_database(repositories):
    import shutil
    source, public, base, original = repositories
    public_objects = public / '.git/objects'
    saved = public / '.git/saved-test-objects'
    public_objects.rename(saved)
    for entry in saved.rglob('*'):
        target = source / '.git/objects' / entry.relative_to(saved)
        if entry.is_dir():
            target.mkdir(exist_ok=True)
        elif not target.exists():
            shutil.copy2(entry, target)
    public_objects.symlink_to(source / '.git/objects', target_is_directory=True)
    tip = commit(source, 'safe change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    from export_pkg._publish import ExportPublishSafetyError
    with pytest.raises(ExportPublishSafetyError, match='object|independent|symbolic|shared'):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original


@pytest.mark.parametrize('domain', [
    'your-llm-gateway.example.com', 'GIT.internal.example.com', 'API.internal.example.com', 'internal.example.com', 'internal.example.com',
])
def test_publication_domain_checks_and_scrub_are_case_insensitive(domain, tmp_path):
    from export_pkg._publish import ExportPublishSafetyError, _verify_opensource_blob
    from export_pkg._sanitize import _collect_opensource_candidates, _sanitize_source_opensource
    text = 'contributor@' + domain + ' https://' + domain + '/v1\n'
    with pytest.raises(ExportPublishSafetyError):
        _verify_opensource_blob(text.encode(), 'synthetic metadata')
    clean = _sanitize_source_opensource(text, 'commit-metadata.txt')
    assert domain.lower() not in clean.lower()
    _verify_opensource_blob(clean.encode(), 'sanitized metadata')
    (tmp_path / 'mixed.ts').write_text(text)
    assert 'mixed.ts' in _collect_opensource_candidates(tmp_path)


def test_source_contributors_and_message_scrub_uppercase_internal_domains(repositories):
    source, public, base, _ = repositories
    git(source, 'config', 'user.email', 'contributor@GIT.internal.example.com')
    tip = commit(source, 'safe change\n\nSee https://api.openai.com/help', {
        'safe.ts': 'const url = "https://api.openai.com/v1";\n'})
    tree = candidate(public, {'safe.ts': 'const url = "https://api.openai.com/v1";\n'})
    result = sync(source, public, tip, tree, base=base)
    raw = subprocess.check_output(['git', 'cat-file', 'commit', result.mapping[tip]], cwd=public)
    assert b'internal.example.com' not in raw.lower()
    assert 'internal.example.com' not in git(public, 'show', result.mapping[tip] + ':safe.ts').lower()


@pytest.mark.parametrize('budget_name,limit', [
    ('_HISTORY_MAX_IMPORTED_BYTES', 100), ('_HISTORY_MAX_MEMBERS', 4),
    ('_HISTORY_MAX_METADATA_BYTES', 50),
])
def test_history_cumulative_budgets_fail_before_ref_advancement(repositories, monkeypatch, budget_name, limit):
    from export_pkg import _history as history
    from export_pkg._publish import ExportPublishSafetyError
    source, public, base, original = repositories
    for letter in 'ABC':
        tip = commit(source, 'safe change ' + letter, {'asset.txt': letter * 70 + '\n'})
    tree = candidate(public, {'asset.txt': 'C' * 70 + '\n'})
    monkeypatch.setattr(history, budget_name, limit, raising=False)
    with pytest.raises(ExportPublishSafetyError, match='budget|limit'):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original
    assert not (public / '.git/tofu-export-history.json').exists()


def test_only_all_acknowledging_remotes_can_admit_previous_public_ancestry(repositories, tmp_path, monkeypatch):
    from export_pkg import _publish as publish
    source, public, base, original = repositories
    remote1, remote2 = bare(tmp_path / 'remote1'), bare(tmp_path / 'remote2')
    # Synthetic previously published policy violation: it must not excuse any
    # new source disclosure, nor be trusted when another target lacks its tip.
    unsafe_anchor = commit(public, 'Existing public snapshot', {'legacy.ts': 'https://GIT.internal.example.com/old\n'})
    git(public, 'push', str(remote1), 'main')
    tip = commit(source, 'safe source change', {'safe.py': 'SAFE = 1\n'})
    (public / 'legacy.ts').unlink()
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [
            {'name': 'one', 'url': str(remote1)}, {'name': 'two', 'url': str(remote2)}]})
    acknowledgements = []

    def builder(destination, tree, message, *, published_tip=None):
        from export_pkg._history import synchronize_public_history
        acknowledgements.append(published_tip)
        return synchronize_public_history(source, destination, tip, tree, message,
                                          history_base=base, published_tip=published_tip).public_tip

    with pytest.raises(publish.ExportPublishSafetyError):
        publish._git_push(public, 'opensource', history_builder=builder)
    assert acknowledgements == [None]
    assert git(public, 'rev-parse', 'HEAD') == unsafe_anchor
    assert git(remote1, 'rev-parse', 'refs/heads/main') == unsafe_anchor
    git(public, 'push', str(remote2), 'main')
    publish._git_push(public, 'opensource', history_builder=builder)
    assert acknowledgements[-1] == unsafe_anchor
    for remote in (remote1, remote2):
        assert git(remote, 'rev-parse', 'refs/heads/main') == git(public, 'rev-parse', 'HEAD')
    assert git(public, 'merge-base', '--is-ancestor', original, 'HEAD') == ''
    assert git(public, 'show', 'HEAD:safe.py') == 'SAFE = 1'


def test_acknowledged_legacy_anchor_never_waives_new_history_scan(repositories, tmp_path, monkeypatch):
    from export_pkg import _publish as publish
    source, public, base, original = repositories
    remote = bare(tmp_path / 'remote')
    git(public, 'push', str(remote), 'main')
    commit(source, 'intermediate secret', {'unsafe.txt': 'ghp_' + 'A' * 36 + '\n'})
    (source / 'unsafe.txt').unlink()
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'remove intermediate secret')
    tip = git(source, 'rev-parse', 'HEAD')
    candidate(public, {})
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [{'name': 'origin', 'url': str(remote)}]})

    def builder(destination, tree, message, *, published_tip=None):
        from export_pkg._history import synchronize_public_history
        assert published_tip == original
        return synchronize_public_history(source, destination, tip, tree, message,
                                          history_base=base, published_tip=published_tip).public_tip

    with pytest.raises(publish.ExportPublishSafetyError):
        publish._git_push(public, 'opensource', history_builder=builder)
    assert git(public, 'rev-parse', 'HEAD') == original
    assert git(remote, 'rev-parse', 'refs/heads/main') == original


@pytest.mark.parametrize('operation', ['read', 'write'])
def test_batch_pipe_deadline_is_enforced_and_process_is_joined(monkeypatch, operation):
    import sys
    from export_pkg import _history as history
    from export_pkg._publish import ExportPublishSafetyError
    monkeypatch.setattr(history, '_HISTORY_IO_SECONDS', 5)
    process = subprocess.Popen([sys.executable, '-c',
                                'import sys; sys.stdin.read(1); sys.stdin.read()' if operation == 'read'
                                else 'import os,sys,threading; os.read(0,1); '
                                     'sys.stdout.write("READY\\n"); sys.stdout.flush(); '
                                     'threading.Event().wait()'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    try:
        pipe = history._HistoryPipe(process, history._HistoryBudget())
        pipe.write(b'x')
        if operation == 'write':
            assert pipe.line() == b'READY\n'
        monkeypatch.setattr(history, '_HISTORY_IO_SECONDS', 0.05)
        with pytest.raises(ExportPublishSafetyError, match='deadline'):
            if operation == 'read':
                pipe.line()
            else:
                pipe.write(b'x' * (1024 * 1024))
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
    assert process.poll() is not None


def test_multiple_archives_share_one_unpacked_byte_budget(repositories, monkeypatch):
    import io
    import zipfile
    from export_pkg import _history as history
    from export_pkg._publish import ExportPublishSafetyError
    source, public, base, original = repositories
    for letter in 'AB':
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('safe.txt', letter * 70)
        (source / ('asset-' + letter + '.zip')).write_bytes(stream.getvalue())
        git(source, 'add', '-A')
        git(source, 'commit', '-qm', 'safe archive ' + letter)
    monkeypatch.setattr(history, '_HISTORY_MAX_UNPACKED_BYTES', 100)
    with pytest.raises(ExportPublishSafetyError, match='unpacked.*budget'):
        sync(source, public, git(source, 'rev-parse', 'HEAD'), candidate(public, {}), base=base)
    assert git(public, 'rev-parse', 'HEAD') == original


def test_batch_total_deadline_is_enforced_before_git_or_ref_change(repositories, monkeypatch):
    from export_pkg import _history as history
    from export_pkg._publish import ExportPublishSafetyError
    source, public, base, original = repositories
    tip = commit(source, 'safe change', {'safe.py': 'SAFE = 1\n'})
    tree = candidate(public, {'safe.py': 'SAFE = 1\n'})
    monkeypatch.setattr(history, '_HISTORY_MAX_SECONDS', 0)
    with pytest.raises(ExportPublishSafetyError, match='deadline'):
        sync(source, public, tip, tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original


@pytest.mark.parametrize('explicit_url', [False, True])
@pytest.mark.parametrize('scope', ['local', 'included', 'environment', 'pushurl', 'legacy_file'])
def test_history_rejects_distinct_actual_push_endpoints_before_builder(
        repositories, tmp_path, monkeypatch, explicit_url, scope):
    from export_pkg import _publish as publish
    source, public, base, _ = repositories
    acknowledged = bare(tmp_path / 'acknowledged')
    unacknowledged = bare(tmp_path / 'unacknowledged')
    anchor = commit(public, 'Prior public snapshot', {'legacy.ts': 'https://GIT.internal.example.com/old\n'})
    git(public, 'push', str(acknowledged), 'main')
    tip = commit(source, 'safe source change', {'safe.py': 'SAFE = 1\n'})
    (public / 'legacy.ts').unlink()
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    config_key = 'url.' + str(unacknowledged) + '.pushInsteadOf'
    if scope == 'local':
        git(public, 'config', config_key, str(acknowledged))
    elif scope == 'included':
        included = tmp_path / 'included.gitconfig'
        subprocess.run(['git', 'config', '--file', str(included), config_key,
                        str(acknowledged)], check=True)
        git(public, 'config', 'include.path', str(included))
    elif scope == 'environment':
        monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0', config_key)
        monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(acknowledged))
    elif scope == 'legacy_file':
        git(public, 'config', config_key, str(acknowledged))
        legacy_file = tmp_path / 'legacy-empty.gitconfig'
        legacy_file.write_text('')
        monkeypatch.setenv('GIT_CONFIG', str(legacy_file))
    else:
        git(public, 'remote', 'add', 'origin', str(acknowledged))
        git(public, 'config', 'remote.origin.pushurl', str(unacknowledged))
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {
        'branch': 'main', 'remotes': [{'name': 'origin', 'url': str(acknowledged)}]})
    if explicit_url:
        monkeypatch.setattr(publish, '_resolve_authenticated_remotes', lambda remotes: [
            {**row, 'auth_url': str(acknowledged)} for row in remotes])
    built = []

    def builder(destination, tree, message, *, published_tip=None):
        from export_pkg._history import synchronize_public_history
        built.append(published_tip)
        return synchronize_public_history(source, destination, tip, tree, message,
                                          history_base=base, published_tip=published_tip).public_tip

    with pytest.raises(publish.ExportPublishSafetyError, match='endpoint|push.*URL|rewrite'):
        publish._git_push(public, 'opensource', history_builder=builder, is_release=True)
    assert not built
    assert git(public, 'rev-parse', 'HEAD') == anchor
    assert git(acknowledged, 'rev-parse', 'refs/heads/main') == anchor
    assert subprocess.run(['git', 'rev-parse', '--verify', 'refs/heads/main'],
                          cwd=unacknowledged, capture_output=True).returncode != 0
    assert subprocess.run(['git', 'rev-parse', '--verify', 'refs/tags/v1.0.0'],
                          cwd=unacknowledged, capture_output=True).returncode != 0


@pytest.mark.parametrize('member_name', [
    'nested/.GIT/config', 'nested\\file.txt', 'nested:name.txt', './alias.txt',
    'nested//alias.txt', 'tail.', 'tail ', 'nested/alias\x7f.txt',
    'nested/.git./config', 'nested/.git /config', 'docs///',
])
def test_removed_history_archive_refuses_nonportable_member_names(repositories, member_name):
    import io
    import zipfile
    from export_pkg._publish import ExportPublishSafetyError

    source, public, base, original = repositories
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, b'safe content')
    asset = source / 'asset.zip'
    asset.write_bytes(stream.getvalue())
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Intermediate archived content')
    asset.unlink()
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Remove archived content')
    tree = candidate(public, {})

    with pytest.raises(ExportPublishSafetyError, match='path'):
        sync(source, public, git(source, 'rev-parse', 'HEAD'), tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original
    assert not (public / '.git/tofu-export-history.json').exists()
    assert not (public / '.git/tofu-export-history.pending.json').exists()


@pytest.mark.parametrize('filename', ['safe\\name.txt', 'safe:name.txt', 'tail.', 'tail '])
def test_removed_history_source_refuses_nonportable_paths(repositories, filename):
    from export_pkg._publish import ExportPublishSafetyError

    source, public, base, original = repositories
    commit(source, 'Intermediate named content', {filename: 'safe content\n'})
    (source / filename).unlink()
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Remove named content')
    tree = candidate(public, {})

    with pytest.raises(ExportPublishSafetyError, match='path'):
        sync(source, public, git(source, 'rev-parse', 'HEAD'), tree, base=base)
    assert git(public, 'rev-parse', 'HEAD') == original
    assert not (public / '.git/tofu-export-history.json').exists()
    assert not (public / '.git/tofu-export-history.pending.json').exists()


@pytest.mark.parametrize('member_kind', ['compressed-directory-data', 'directory-symlink', 'file-special'])
def test_removed_history_archive_refuses_directory_payloads_and_special_types(repositories, member_kind):
    import io
    import zipfile
    from export_pkg._publish import ExportPublishSafetyError

    source, public, base, original = repositories
    directory = member_kind != 'file-special'
    member = zipfile.ZipInfo('docs/' if directory else 'safe.txt')
    member.create_system = 3
    member.external_attr = {'compressed-directory-data': 0o40755,
                            'directory-symlink': 0o120777,
                            'file-special': 0o10644}[member_kind] << 16
    value = b'ghp_' + b'A' * 36 if member_kind == 'compressed-directory-data' else b''
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(member, value, compress_type=zipfile.ZIP_DEFLATED)
    assert not value or value not in stream.getvalue()
    asset = source / 'asset.zip'
    asset.write_bytes(stream.getvalue())
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Intermediate archive metadata')
    asset.unlink()
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Remove archived metadata')

    with pytest.raises(ExportPublishSafetyError):
        sync(source, public, git(source, 'rev-parse', 'HEAD'), candidate(public, {}), base=base)
    assert git(public, 'rev-parse', 'HEAD') == original
    assert not (public / '.git/tofu-export-history.json').exists()
    assert not (public / '.git/tofu-export-history.pending.json').exists()


def test_history_accepts_empty_plain_archive_directories(repositories):
    import io
    import zipfile

    source, public, base, _ = repositories
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        directory = zipfile.ZipInfo('docs/')
        directory.create_system = 3
        directory.external_attr = (0o40755 << 16) | 0x10
        archive.writestr(directory, b'')
        archive.writestr('docs/readme.txt', b'Public archive content.\n')
    (source / 'asset.zip').write_bytes(stream.getvalue())
    git(source, 'add', '-A')
    git(source, 'commit', '-qm', 'Plain portable archive')
    tip = git(source, 'rev-parse', 'HEAD')
    result = sync(source, public, tip, candidate(public, {}), base=base)
    assert subprocess.check_output(['git', 'show', result.mapping[tip] + ':asset.zip'],
                                   cwd=public) == stream.getvalue()


@pytest.mark.parametrize('resume_kind', ['same-source', 'new-source', 'pending'])
@pytest.mark.parametrize('receiver_change', ['added', 'instead-of'])
def test_history_resume_rechecks_legacy_ancestry_at_every_receiving_endpoint(
        repositories, tmp_path, monkeypatch, resume_kind, receiver_change):
    from export_pkg import _history as history, _publish as publish

    source, public, base, _ = repositories
    remote1, remote2, empty = (bare(tmp_path / name) for name in ('one', 'two', 'empty'))
    legacy = commit(public, 'Existing public snapshot', {'legacy.ts': 'https://GIT.internal.example.com/old\n'})
    for remote in (remote1, remote2):
        git(public, 'push', str(remote), 'main')
    tip = commit(source, 'Safe incremental change', {'safe.py': 'SAFE = 1\n'})
    (public / 'legacy.ts').unlink()
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    remotes = [{'name': 'one', 'url': str(remote1)}, {'name': 'two', 'url': str(remote2)}]
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {'branch': 'main', 'remotes': remotes})

    def builder(destination, tree, message, *, published_tip=None):
        return history.synchronize_public_history(source, destination, tip, tree, message,
            history_base=base, published_tip=published_tip).public_tip

    write_state = history._write_state
    if resume_kind == 'pending':
        def fail_cursor(path, value):
            if path.name == 'tofu-export-history.json':
                raise OSError('synthetic lost cursor acknowledgement')
            return write_state(path, value)
        monkeypatch.setattr(history, '_write_state', fail_cursor)
        with pytest.raises(OSError, match='cursor acknowledgement'):
            publish._git_push(public, 'opensource', history_builder=builder)
        monkeypatch.setattr(history, '_write_state', write_state)
    else:
        publish._git_push(public, 'opensource', history_builder=builder)
    head = git(public, 'rev-parse', 'HEAD')
    cursor, pending = (public / '.git' / ('tofu-export-history' + suffix + '.json')
                       for suffix in ('', '.pending'))
    before = {path: path.read_bytes() if path.exists() else None for path in (cursor, pending)}
    remote_before = [git(remote, 'rev-parse', 'refs/heads/main') for remote in (remote1, remote2)]
    assert git(public, 'merge-base', legacy, head) == legacy
    if resume_kind == 'new-source':
        tip = commit(source, 'Next incremental change', {'next.py': 'NEXT = 1\n'})
        candidate(public, {'next.py': 'NEXT = 1\n'})
    if receiver_change == 'added':
        remotes.append({'name': 'new', 'url': str(empty)})
    else:
        git(public, 'config', 'url.' + str(empty) + '.insteadOf', str(remote2))

    with pytest.raises(publish.ExportPublishSafetyError):
        publish._git_push(public, 'opensource', history_builder=builder, is_release=True)
    assert git(public, 'rev-parse', 'HEAD') == head
    assert {path: path.read_bytes() if path.exists() else None for path in (cursor, pending)} == before
    assert [git(remote, 'rev-parse', 'refs/heads/main') for remote in (remote1, remote2)] == remote_before
    assert subprocess.run(['git', 'show-ref'], cwd=empty, capture_output=True).returncode == 1


def test_history_partial_retry_keeps_previously_acknowledged_legacy_frontier(
        repositories, tmp_path, monkeypatch):
    from export_pkg import _history as history, _publish as publish

    source, public, base, _ = repositories
    remotes = [bare(tmp_path / name) for name in ('one', 'two')]
    legacy = commit(public, 'Existing public snapshot', {'legacy.ts': 'https://GIT.internal.example.com/old\n'})
    for remote in remotes:
        git(public, 'push', str(remote), 'main')
    tip = commit(source, 'Safe incremental change', {'safe.py': 'SAFE = 1\n'})
    (public / 'legacy.ts').unlink()
    candidate(public, {'safe.py': 'SAFE = 1\n'})
    monkeypatch.setitem(publish._GIT_REPOS, 'opensource', {'branch': 'main', 'remotes': [
        {'name': name, 'url': str(remote)} for name, remote in zip(('one', 'two'), remotes)]})
    push_branch = publish._push_branch
    def fail_second(run, remote, branch, **kwargs):
        if remote == 'two':
            raise RuntimeError('synthetic second receiver unavailable')
        return push_branch(run, remote, branch, **kwargs)
    monkeypatch.setattr(publish, '_push_branch', fail_second)
    frontiers = []
    def builder(destination, tree, message, *, published_tip=None):
        frontiers.append(published_tip)
        return history.synchronize_public_history(source, destination, tip, tree, message,
            history_base=base, published_tip=published_tip).public_tip
    with pytest.raises(RuntimeError, match='second receiver'):
        publish._git_push(public, 'opensource', history_builder=builder)
    head = git(public, 'rev-parse', 'HEAD')
    count = git(public, 'rev-list', '--count', head)
    monkeypatch.setattr(publish, '_push_branch', push_branch)
    publish._git_push(public, 'opensource', history_builder=builder)
    assert frontiers == [legacy, legacy]
    assert git(public, 'rev-list', '--count', 'HEAD') == count
    assert all(git(remote, 'rev-parse', 'refs/heads/main') == head for remote in remotes)
