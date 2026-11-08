"""Internal snapshots publish only to configured internal targets.

Owner decision 2026-11-06: Internal releases go to dev.internal.example.com and to a
private GitHub repository. Internal trees carry company endpoints and shared
configuration, so ``_handoff._snapshot_push`` (the only internal publisher)
admits a target through ``_publish._require_internal_push_target`` before it
writes anything and again immediately before ``git push``:

* an Open remote is refused before any network access, in any URL spelling;
* any other non-GitHub target must be a configured internal remote;
* a GitHub target must be reported by the API, at that moment, as private and
  owned by the user account the publish credential authenticates as; public,
  enterprise-internal, organization-owned or unknown visibility is refused.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')
pytestmark = pytest.mark.unit

import export_pkg._handoff as handoff  # noqa: E402
import export_pkg._publish as publish  # noqa: E402

PRIVATE_TARGET = 'https://github.com/release-owner/ToFu-internal.git'
PRIVATE_PATH = '/repos/release-owner/tofu-internal'


def _repository(**changes):
    repository = {'full_name': 'release-owner/ToFu-internal', 'private': True,
                  'visibility': 'private',
                  'owner': {'login': 'release-owner', 'type': 'User'}}
    repository.update(changes)
    return repository


@pytest.fixture
def github(monkeypatch):
    """Answer GitHub REST reads from a table; the network is never reached."""
    answers = {'/user': {'login': 'release-owner'}, PRIVATE_PATH: _repository()}
    calls = []

    def read(path):
        calls.append(path)
        answer = answers[path]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(publish, '_github_json', read)
    return answers, calls


@pytest.mark.parametrize('url', (
    'https://github.com/rangehow/ToFu.git',
    'https://github.com/NiuTrans/ToFu',
    'https://github.com/rangehow/tofu/',
    'git@github.com:NiuTrans/ToFu.git',
    'ssh://git@github.com/rangehow/ToFu.git',
))
def test_open_remote_is_refused_before_any_network_access(github, url):
    _, calls = github
    with pytest.raises(publish.ExportPublishSafetyError, match='Open remote'):
        publish._require_internal_push_target(url)
    assert calls == []


@pytest.mark.parametrize('repository', (
    _repository(private=False, visibility='public'),
    _repository(visibility='internal'),
    _repository(private=None),
    {key: value for key, value in _repository().items() if key not in ('private', 'visibility')},
), ids=('public', 'enterprise-internal', 'private-unknown', 'no-visibility-facts'))
def test_github_target_that_is_not_verified_private_is_refused(github, repository):
    answers, _ = github
    answers[PRIVATE_PATH] = repository
    with pytest.raises(publish.ExportPublishSafetyError, match='not private'):
        publish._require_internal_push_target(PRIVATE_TARGET)


@pytest.mark.parametrize('failure', (
    urllib.error.HTTPError(PRIVATE_TARGET, 404, 'Not Found', {}, None),
    urllib.error.URLError('network unreachable'),
    ValueError('malformed JSON'),
), ids=('not-found', 'unreachable', 'malformed'))
def test_unknown_visibility_is_refused(github, failure):
    answers, _ = github
    answers[PRIVATE_PATH] = failure
    with pytest.raises(publish.ExportPublishSafetyError, match='unknown'):
        publish._require_internal_push_target(PRIVATE_TARGET)


@pytest.mark.parametrize('owner', (
    {'login': 'release-owner', 'type': 'Organization'},
    {'login': 'someone-else', 'type': 'User'},
), ids=('organization', 'other-account'))
def test_private_repository_outside_the_publishing_account_is_refused(github, owner):
    answers, _ = github
    answers[PRIVATE_PATH] = _repository(owner=owner)
    with pytest.raises(publish.ExportPublishSafetyError, match='publishing account'):
        publish._require_internal_push_target(PRIVATE_TARGET)


def test_redirect_onto_an_open_remote_is_refused(github):
    answers, _ = github
    answers[PRIVATE_PATH] = _repository(full_name='rangehow/ToFu')
    with pytest.raises(publish.ExportPublishSafetyError, match='Open remote'):
        publish._require_internal_push_target(PRIVATE_TARGET)


def test_private_repository_of_the_publishing_account_is_admitted(github):
    _, calls = github
    publish._require_internal_push_target(PRIVATE_TARGET)
    assert calls == ['/user', PRIVATE_PATH]


def test_configured_internal_remote_is_admitted_without_network(github):
    _, calls = github
    for remote in publish._GIT_REPOS['internal']['remotes']:
        publish._require_internal_push_target(remote['url'])
    assert calls == []


@pytest.mark.parametrize('url', (
    'https://gitlab.com/someone/tofu.git',
    'ssh://git@git.internal.example.com/~someone/tofu.git',
    '/srv/git/tofu.git',
    'https://token-value@github.com/release-owner/ToFu-internal.git',
))
def test_unconfigured_or_credential_bearing_target_is_refused(github, url):
    _, calls = github
    with pytest.raises(publish.ExportPublishSafetyError):
        publish._require_internal_push_target(url)
    assert calls == []


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)
    assert completed.returncode == 0, (args, completed.stderr)
    return completed.stdout.strip()


@pytest.fixture
def bare_target(tmp_path, monkeypatch):
    """An empty bare repository configured as the only internal remote."""
    remote = tmp_path / 'target.git'
    _git(tmp_path, 'init', '--bare', '-q', str(remote))
    monkeypatch.setitem(publish._GIT_REPOS, 'internal', {
        'remotes': [{'name': 'origin', 'url': str(remote)}], 'branch': 'master'})
    return remote


def _snapshot(tmp_path: Path, *, committed: bool) -> Path:
    dest = tmp_path / 'snapshot'
    dest.mkdir()
    (dest / 'release.txt').write_text('verified internal tree\n')
    if committed:
        _git(dest, 'init', '-q', '-b', 'master')
        _git(dest, '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A')
        _git(dest, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'prepared')
    return dest


def test_committed_snapshot_reaches_a_branch_the_target_never_received(tmp_path, bare_target):
    dest = _snapshot(tmp_path, committed=True)
    prepared = _git(dest, 'rev-parse', 'HEAD')

    assert handoff._snapshot_push(dest, str(bare_target), 'master') is None

    assert _git(bare_target, 'rev-parse', 'refs/heads/master') == prepared


def test_target_is_admitted_again_immediately_before_the_push(tmp_path, bare_target, monkeypatch):
    dest = _snapshot(tmp_path, committed=False)
    verdicts = iter((None, publish.ExportPublishSafetyError('GitHub internal push target is not private')))

    def admit(url):
        verdict = next(verdicts)
        if verdict is not None:
            raise verdict

    monkeypatch.setattr(handoff, '_require_internal_push_target', admit)

    error = handoff._snapshot_push(dest, str(bare_target), 'master')

    assert error is not None and 'not private' in error
    assert _git(dest, 'log', '--format=%s', '-1').startswith('Export (internal)')
    assert _git(bare_target, 'for-each-ref') == ''


def test_open_remote_is_refused_before_the_snapshot_repository_changes(tmp_path, github):
    dest = _snapshot(tmp_path, committed=False)

    error = handoff._snapshot_push(dest, 'https://github.com/rangehow/ToFu.git', 'master')

    assert error is not None and 'Open remote' in error
    assert not (dest / '.git').exists()


def test_snapshot_repository_of_another_target_is_refused(tmp_path, bare_target):
    dest = _snapshot(tmp_path, committed=True)
    _git(dest, 'remote', 'add', 'origin', 'ssh://git@git.example.invalid/elsewhere.git')

    error = handoff._snapshot_push(dest, str(bare_target), 'master')

    assert error is not None and 'another target' in error
    assert _git(dest, 'remote', 'get-url', 'origin') == 'ssh://git@git.example.invalid/elsewhere.git'
    assert _git(bare_target, 'for-each-ref') == ''


@pytest.mark.parametrize('key', ('insteadOf', 'pushInsteadOf'))
def test_url_rewrite_away_from_the_admitted_target_is_refused(tmp_path, bare_target, key):
    dest = _snapshot(tmp_path, committed=True)
    elsewhere = tmp_path / 'elsewhere.git'
    _git(tmp_path, 'init', '--bare', '-q', str(elsewhere))
    _git(dest, 'config', f'url.{elsewhere}.{key}', str(bare_target))

    error = handoff._snapshot_push(dest, str(bare_target), 'master')

    assert error is not None and 'refused' in error
    assert _git(elsewhere, 'for-each-ref') == ''
    assert _git(bare_target, 'for-each-ref') == ''


def test_internal_handoff_push_uses_the_requested_snapshot_repository(tmp_path, monkeypatch):
    import export_pkg._orchestrate as orchestrate

    exported, pushed = [], []
    monkeypatch.setattr(orchestrate, 'export_project',
                        lambda mode, dest, **kwargs: exported.append(dest))
    monkeypatch.setattr(handoff, '_snapshot_push',
                        lambda dest, url, branch: pushed.append((dest, url, branch)))
    monkeypatch.setattr(handoff, 'ROOT', tmp_path / 'source')
    monkeypatch.setattr(handoff, '_dirty_tracked_files', lambda root: [])
    monkeypatch.setattr(handoff, '_write_state_tarball', lambda path, mode: path.write_bytes(b''))
    snapshot = tmp_path / 'private-mirror'

    handoff.run_handoff('internal', tmp_path / 'handoff', PRIVATE_TARGET, push=True,
                        export_dest=snapshot)

    assert exported == [snapshot]
    assert pushed == [(snapshot, PRIVATE_TARGET, 'master')]


def test_cli_dest_names_the_internal_snapshot_repository(tmp_path, monkeypatch):
    import export

    seen = []
    monkeypatch.setattr(export, 'run_handoff', lambda *args, **kwargs: seen.append(kwargs))
    monkeypatch.setattr(sys, 'argv', [
        'export.py', '--mode', 'internal', '--handoff', str(tmp_path / 'handoff'), '--push',
        '--git-url', PRIVATE_TARGET, '--dest', str(tmp_path / 'private-mirror')])

    export.main()

    assert [kwargs['export_dest'] for kwargs in seen] == [(tmp_path / 'private-mirror').resolve()]


@pytest.mark.parametrize('argv', (
    ('--mode', 'personal', '--git-url', PRIVATE_TARGET, '--push'),
    ('--mode', 'internal'),
), ids=('personal', 'without-push'))
def test_cli_dest_with_handoff_requires_an_internal_push(tmp_path, monkeypatch, argv):
    import export

    monkeypatch.setattr(export, 'run_handoff', lambda *args, **kwargs: pytest.fail('handoff ran'))
    monkeypatch.setattr(sys, 'argv', ['export.py', *argv, '--handoff', str(tmp_path / 'handoff'),
                                      '--dest', str(tmp_path / 'private-mirror')])

    with pytest.raises(SystemExit) as raised:
        export.main()

    assert raised.value.code == 2
