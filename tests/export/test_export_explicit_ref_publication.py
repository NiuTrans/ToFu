"""Publication sends only admitted refs, even with push.followTags enabled."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which('git') is None, reason='git not installed'),
]

_PRIVATE_TAG = 'local-private-annotation'
_RELEASE_TAG = 'v9.9.9'
_PRIVATE_MESSAGE = (
    'Synthetic private annotation, not admitted for publication.\n\n'
    '-----BEGIN PGP SIGNATURE-----\n'
    'synthetic-not-a-real-signature\n'
    '-----END PGP SIGNATURE-----\n'
)


@pytest.fixture(autouse=True)
def isolated_git_environment(tmp_path, monkeypatch):
    # Never inherit repository targeting, credentials, hooks, signing or URL
    # rewrites from the maintainer's environment. Only local file transports
    # are allowed, including in the real publication commands under test.
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_TERMINAL_PROMPT', '0')
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    monkeypatch.setenv('LC_ALL', 'C')


def _git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', *args], cwd=repo, capture_output=True, text=True, timeout=60,
    )


def _git(repo: Path, *args: str) -> str:
    result = _git_result(repo, *args)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or 'synthetic git failure')
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, 'init', '-q', '--template=')
    _git(path, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    _git(path, 'config', 'user.name', 'publication-test')
    _git(path, 'config', 'user.email', 'publication-test@example.invalid')
    return path


def _commit(repo: Path, content: str, message: str) -> str:
    (repo / 'README.md').write_text(content, encoding='utf-8')
    _git(repo, 'add', 'README.md')
    _git(repo, 'commit', '-qm', message)
    return _git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def publication_repo(tmp_path):
    repo = _repo(tmp_path / 'public')
    receiver = tmp_path / 'receiver.git'
    _git(tmp_path, 'init', '--bare', '-q', '--template=', str(receiver))
    _git(repo, 'remote', 'add', 'origin', str(receiver))
    old = _commit(repo, 'Old public source.\n', 'Synthetic public baseline')
    _git(repo, '-c', 'user.name=synthetic-private-tagger',
         '-c', 'user.email=private-tagger@example.invalid',
         'tag', '-a', _PRIVATE_TAG, old, '-m', _PRIVATE_MESSAGE)
    private_tag_oid = _git(repo, 'rev-parse', f'refs/tags/{_PRIVATE_TAG}')
    candidate = _commit(repo, 'Reviewed public source.\n', 'Synthetic reviewed release')
    local_head = _commit(repo, 'Later local source.\n', 'Synthetic later local work')
    _git(repo, 'config', 'push.followTags', 'true')

    # The unchecked annotation is on an OLD reachable commit, not merely HEAD.
    # Its synthetic tagger, message and signature bytes must never be sent.
    assert old != candidate
    assert _git(repo, 'merge-base', '--is-ancestor', old, candidate) == ''
    assert _git(repo, 'cat-file', '-t', private_tag_oid) == 'tag'
    assert '-----BEGIN PGP SIGNATURE-----' in _git(repo, 'cat-file', 'tag', private_tag_oid)
    assert _git(repo, 'config', '--get', 'push.followTags') == 'true'
    return repo, receiver, candidate, local_head, private_tag_oid


def _runner(repo: Path, calls: list, *, fail_first_push: bool = False):
    failed = False

    def run(cmd):
        nonlocal failed
        calls.append(cmd)
        if fail_first_push and cmd[1] == 'push' and not failed:
            failed = True
            raise RuntimeError('synthetic first push needs upstream retry')
        return _git(repo, *cmd[1:])

    return run


def _release_tag(repo: Path, candidate: str, annotated: bool = True) -> str:
    args = ['tag']
    if annotated:
        args.extend(['-a', '-m', 'Synthetic admitted release'])
    _git(repo, *args, _RELEASE_TAG, candidate)
    return _git(repo, 'rev-parse', f'refs/tags/{_RELEASE_TAG}')


def _assert_receiver_refs(receiver: Path, expected: set[str], private_tag_oid: str):
    refs = set(_git(receiver, 'for-each-ref', '--format=%(refname)').splitlines())
    assert f'refs/tags/{_PRIVATE_TAG}' not in refs, 'unchecked annotated tag was published'
    assert refs == expected
    assert _git_result(receiver, 'cat-file', '-e', private_tag_oid).returncode != 0, (
        'unchecked annotated tag object was transferred'
    )


def _seed_receiver(tmp_path: Path, receiver: Path) -> str:
    seed = _repo(tmp_path / 'seed')
    tip = _commit(seed, 'Remote-only source.\n', 'Synthetic remote history')
    _git(seed, 'push', '--no-follow-tags', str(receiver), 'main')
    return tip


@pytest.mark.parametrize('fixed_candidate', (False, True))
@pytest.mark.parametrize('remote_kind', ('named', 'url'))
def test_branch_publication_admits_branch_before_release_tag(
        publication_repo, fixed_candidate, remote_kind):
    from export_pkg._publish import _push_branch, _push_tag

    repo, receiver, candidate, local_head, private_tag_oid = publication_repo
    tag_oid = _release_tag(repo, candidate)
    target = 'origin' if remote_kind == 'named' else receiver.as_uri()
    run = _runner(repo, [])

    _push_branch(run, target, 'main', candidate=candidate if fixed_candidate else None)

    expected_tip = candidate if fixed_candidate else local_head
    assert _git(receiver, 'rev-parse', 'refs/heads/main') == expected_tip
    assert _git(repo, 'rev-parse', 'HEAD') == local_head
    _assert_receiver_refs(receiver, {'refs/heads/main'}, private_tag_oid)

    _push_tag(run, target, _RELEASE_TAG)

    assert _git(receiver, 'rev-parse', 'refs/heads/main') == expected_tip
    assert _git(receiver, 'rev-parse', f'refs/tags/{_RELEASE_TAG}') == tag_oid
    assert _git(receiver, 'rev-parse', f'refs/tags/{_RELEASE_TAG}^{{commit}}') == candidate
    _assert_receiver_refs(receiver, {'refs/heads/main', f'refs/tags/{_RELEASE_TAG}'}, private_tag_oid)


@pytest.mark.parametrize('annotated', (False, True))
@pytest.mark.parametrize('force', (False, True))
def test_explicit_release_tag_publication_sends_only_admitted_tag(publication_repo, annotated, force):
    from export_pkg._publish import _push_tag

    repo, receiver, candidate, local_head, private_tag_oid = publication_repo
    tag_oid = _release_tag(repo, candidate, annotated=annotated)

    # Start from an empty receiver so a preceding guarded branch push cannot
    # mask a tag-only publication leak.
    _push_tag(_runner(repo, []), 'origin', _RELEASE_TAG, force=force)

    assert _git(receiver, 'rev-parse', f'refs/tags/{_RELEASE_TAG}') == tag_oid
    assert _git(receiver, 'rev-parse', f'refs/tags/{_RELEASE_TAG}^{{commit}}') == candidate
    assert _git(repo, 'rev-parse', 'HEAD') == local_head
    _assert_receiver_refs(receiver, {f'refs/tags/{_RELEASE_TAG}'}, private_tag_oid)


def test_upstream_retry_sends_only_admitted_branch(publication_repo):
    from export_pkg._publish import _push_branch

    repo, receiver, _, local_head, private_tag_oid = publication_repo
    _push_branch(_runner(repo, [], fail_first_push=True), 'origin', 'main')

    assert _git(receiver, 'rev-parse', 'refs/heads/main') == local_head
    assert _git(repo, 'config', '--get', 'branch.main.remote') == 'origin'
    assert _git(repo, 'config', '--get', 'branch.main.merge') == 'refs/heads/main'
    _assert_receiver_refs(receiver, {'refs/heads/main'}, private_tag_oid)


@pytest.mark.parametrize('force', (False, True))
@pytest.mark.parametrize('remote_kind', ('named', 'url'))
def test_divergence_retry_sends_only_admitted_branch(publication_repo, tmp_path, force, remote_kind):
    from export_pkg._publish import _push_branch

    repo, receiver, _, local_head, private_tag_oid = publication_repo
    remote_tip = _seed_receiver(tmp_path, receiver)
    expected_tree = _git(repo, 'rev-parse', 'HEAD^{tree}')
    target = 'origin' if remote_kind == 'named' else receiver.as_uri()

    _push_branch(_runner(repo, []), target, 'main', force=force)

    pushed_tip = _git(receiver, 'rev-parse', 'refs/heads/main')
    assert pushed_tip == _git(repo, 'rev-parse', 'HEAD')
    assert _git(receiver, 'rev-parse', 'refs/heads/main^{tree}') == expected_tree
    remote_history = _git(receiver, 'log', '--format=%H', 'refs/heads/main').splitlines()
    if force:
        assert pushed_tip == local_head
        assert remote_tip not in remote_history
    else:
        assert remote_tip in remote_history
        assert _git(receiver, 'show', '-s', '--format=%P', pushed_tip).split() == [local_head, remote_tip]
    _assert_receiver_refs(receiver, {'refs/heads/main'}, private_tag_oid)


def test_fixed_candidate_force_is_rejected_before_any_git_command(publication_repo):
    from export_pkg._publish import ExportPublishSafetyError, _push_branch

    repo, receiver, candidate, local_head, private_tag_oid = publication_repo
    calls = []
    with pytest.raises(ExportPublishSafetyError, match='cannot force a fixed candidate'):
        _push_branch(_runner(repo, calls), 'origin', 'main', force=True, candidate=candidate)

    assert calls == []
    assert _git(repo, 'rev-parse', 'HEAD') == local_head
    _assert_receiver_refs(receiver, set(), private_tag_oid)


@pytest.mark.parametrize('remote_kind', ('named', 'url'))
def test_fixed_candidate_rejection_does_not_publish_other_refs(publication_repo, tmp_path, remote_kind):
    from export_pkg._publish import ExportPublishSafetyError, _push_branch

    repo, receiver, candidate, local_head, private_tag_oid = publication_repo
    remote_tip = _seed_receiver(tmp_path, receiver)
    target = 'origin' if remote_kind == 'named' else receiver.as_uri()
    calls = []
    with pytest.raises(ExportPublishSafetyError, match='reviewed candidate'):
        _push_branch(_runner(repo, calls), target, 'main', candidate=candidate)

    assert _git(receiver, 'rev-parse', 'refs/heads/main') == remote_tip
    assert _git(repo, 'rev-parse', 'HEAD') == local_head
    assert not any(cmd[1] in ('fetch', 'merge') or '--force' in cmd for cmd in calls)
    _assert_receiver_refs(receiver, {'refs/heads/main'}, private_tag_oid)
