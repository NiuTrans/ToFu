"""The updater follows the checkout's own upstream.

An internal clone's only branch is ``master``; every push is one release
snapshot that bumps ``VERSION``. Its update check compares against the
``VERSION`` on ``origin/master`` and its apply fast-forwards from there, while
a checkout of the official GitHub repository keeps the release-tags check.

Each test owns a bare remote, a publisher clone and the checkout under test.
Git runs without global/system configuration and may only use local
repositories, so nothing here can reach a network.
"""

from __future__ import annotations

import os
import subprocess

import pytest

import lib.self_update as su
from lib.self_update import _apply, _git, _status, _version

pytestmark = pytest.mark.unit

_IDENTITY = ('-c', 'user.name=Release', '-c', 'user.email=release@example.test')

_CHECK_PAYLOAD_KEYS = {
    'current', 'latest', 'tag', 'update_available', 'git_available',
    'update_method', 'dirty', 'blocking', 'runtime_changes', 'repo', 'error',
    'error_kind', 'error_detail',
}


def _git_out(cwd, *args) -> str:
    return subprocess.run(['git', '-C', str(cwd), *_IDENTITY, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _publish(publisher, version, branch='master') -> str:
    (publisher / 'VERSION').write_text(f'{version}\n', encoding='utf-8')
    _git_out(publisher, 'add', 'VERSION')
    _git_out(publisher, 'commit', '-q', '-m', f'Release {version}')
    _git_out(publisher, 'push', '-q', 'origin', f'HEAD:refs/heads/{branch}')
    return _git_out(publisher, 'rev-parse', 'HEAD')


class _TagsResponse:
    status_code = 200

    @staticmethod
    def json():
        return [{'name': 'v9.9.9'}, {'name': 'v0.1.0'}]


@pytest.fixture
def tags_api(monkeypatch):
    """GitHub tags API stand-in; the returned list records every request."""
    urls = []

    def fake_http_get(url, **_kwargs):
        urls.append(url)
        return _TagsResponse()

    monkeypatch.setattr(_version, 'http_get', fake_http_get)
    return urls


@pytest.fixture
def audits(monkeypatch):
    """Audit entries written by an apply, as ``(event, details)`` pairs."""
    records = []
    monkeypatch.setattr(_apply, 'audit_log',
                        lambda event, **details: records.append((event, details)))
    return records


@pytest.fixture
def updater(monkeypatch):
    """Isolate the updater; the returned callable points it at a checkout."""
    for name in ('TOFU_UPDATE_REPO', 'TOFU_UPDATE_REMOTE', 'TOFU_UPDATE_BRANCH'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    monkeypatch.setattr(_status, '_latest_release_cache',
                        {'at': 0.0, 'value': None, 'err': None})
    monkeypatch.setattr(_status, '_tree_status_cache', {'at': 0.0, 'value': None})
    monkeypatch.setattr(_version, '_failure_state', {'ts': 0.0, 'error': None})
    monkeypatch.setattr(su, 'current_version', lambda: '0.1.0')
    monkeypatch.setattr(_apply, 'current_version', lambda: '0.1.0')

    def point_at(checkout):
        monkeypatch.setattr(_git, '_ROOT', str(checkout))
        monkeypatch.setattr(_apply, '_ROOT', str(checkout))

    return point_at


@pytest.fixture
def internal_clone(tmp_path, updater):
    """``(publisher, checkout)`` around a bare remote whose only branch is master."""
    remote = tmp_path / 'tofu.git'
    _git_out(tmp_path, 'init', '-q', '--bare', str(remote))
    _git_out(remote, 'symbolic-ref', 'HEAD', 'refs/heads/master')
    publisher = tmp_path / 'publisher'
    _git_out(tmp_path, 'init', '-q', str(publisher))
    _git_out(publisher, 'symbolic-ref', 'HEAD', 'refs/heads/master')
    _git_out(publisher, 'remote', 'add', 'origin', str(remote))
    _publish(publisher, '0.1.0')
    checkout = tmp_path / 'checkout'
    _git_out(tmp_path, 'clone', '-q', str(remote), str(checkout))
    updater(checkout)
    return publisher, checkout


def test_check_reports_the_bumped_upstream_version(internal_clone, tags_api):
    publisher, _checkout = internal_clone
    released = _publish(publisher, '0.2.0')

    payload = su.check_for_update()

    assert set(payload) == _CHECK_PAYLOAD_KEYS
    assert payload['error'] is None, payload
    assert (payload['latest'], payload['update_available']) == ('0.2.0', True)
    assert payload['tag'] == released
    assert (payload['update_method'], payload['dirty']) == ('git', False)
    assert tags_api == [], 'an internal upstream must not consult GitHub tags'


def test_check_reports_no_update_when_the_upstream_version_is_equal(
        internal_clone, tags_api):
    payload = su.check_for_update()

    assert payload['error'] is None, payload
    assert (payload['latest'], payload['update_available']) == ('0.1.0', False)
    assert tags_api == []


def test_unreachable_upstream_keeps_the_failure_envelope(internal_clone, tmp_path):
    _publisher, checkout = internal_clone
    _git_out(checkout, 'remote', 'set-url', 'origin', str(tmp_path / 'gone.git'))

    payload = su.check_for_update()

    assert set(payload) == _CHECK_PAYLOAD_KEYS
    assert (payload['latest'], payload['update_available']) == (None, False)
    assert payload['error_kind'] == 'network' and payload['error']
    assert 'gone.git' in payload['error_detail']


def test_apply_fast_forwards_from_origin_master(internal_clone, audits):
    publisher, checkout = internal_clone
    released = _publish(publisher, '0.2.0')

    result = su._apply_via_git()

    assert result['ok'] is True, result
    assert (result['changed'], result['needs_restart']) == (True, True)
    assert (result['old_version'], result['new_version']) == ('0.1.0', '0.2.0')
    assert _git_out(checkout, 'rev-parse', 'HEAD') == released
    assert [(details['remote'], details['branch']) for _event, details in audits] == [
        ('origin', 'master')]


def test_explicit_branch_wins_for_check_and_apply(
        internal_clone, tags_api, audits, monkeypatch):
    publisher, checkout = internal_clone
    _publish(publisher, '0.2.0')
    stable = _publish(publisher, '0.3.0', branch='stable')
    monkeypatch.setenv('TOFU_UPDATE_BRANCH', 'stable')

    payload = su.check_for_update()
    result = su._apply_via_git()

    assert (payload['latest'], payload['tag']) == ('0.3.0', stable)
    assert result['ok'] is True, result
    assert _git_out(checkout, 'rev-parse', 'HEAD') == stable
    assert [(details['remote'], details['branch']) for _event, details in audits] == [
        ('origin', 'stable')]
    assert tags_api == []


def test_upstream_comes_from_the_current_branch_tracking_config(internal_clone):
    _publisher, checkout = internal_clone
    _git_out(checkout, 'remote', 'rename', 'origin', 'internal')

    assert su.resolve_update_upstream() == su.UpdateUpstream(
        remote='internal', branch='master')


@pytest.mark.parametrize('head', ['detached', 'untracked_branch'])
def test_without_an_upstream_the_updater_falls_back_to_origin_main(
        internal_clone, head):
    _publisher, checkout = internal_clone
    if head == 'detached':
        _git_out(checkout, 'checkout', '-q', '--detach')
    else:
        _git_out(checkout, 'checkout', '-q', '-b', 'local-only')

    assert su.resolve_update_upstream() == su.UpdateUpstream(
        remote='origin', branch='main')


def test_explicit_remote_and_branch_need_no_upstream(internal_clone, monkeypatch):
    _publisher, checkout = internal_clone
    _git_out(checkout, 'checkout', '-q', '--detach')
    monkeypatch.setenv('TOFU_UPDATE_REMOTE', 'mirror')
    monkeypatch.setenv('TOFU_UPDATE_BRANCH', 'release')

    assert su.resolve_update_upstream() == su.UpdateUpstream(
        remote='mirror', branch='release')


@pytest.mark.parametrize('url', [
    'https://github.com/{repo}.git',
    'https://github.com/{repo}',
    'ssh://git@github.com/{repo}.git',
    'git@github.com:{repo}.git',
    'git@github.com:{repo}',
])
def test_official_github_checkout_keeps_the_tags_check(tmp_path, updater, tags_api, url):
    checkout = tmp_path / 'checkout'
    _git_out(tmp_path, 'init', '-q', str(checkout))
    _git_out(checkout, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    _git_out(checkout, 'commit', '-q', '--allow-empty', '-m', 'install')
    _git_out(checkout, 'remote', 'add', 'origin', url.format(repo=su.UPDATE_REPO))
    _git_out(checkout, 'config', 'branch.main.remote', 'origin')
    _git_out(checkout, 'config', 'branch.main.merge', 'refs/heads/main')
    updater(checkout)

    payload = su.check_for_update()

    assert tags_api == [su._TAGS_URL]
    assert set(payload) == _CHECK_PAYLOAD_KEYS
    assert (payload['latest'], payload['tag'], payload['update_available']) == (
        '9.9.9', 'v9.9.9', True)


def test_explicit_update_repo_keeps_the_tags_check(internal_clone, tags_api, monkeypatch):
    monkeypatch.setenv('TOFU_UPDATE_REPO', su.UPDATE_REPO)

    payload = su.check_for_update()

    assert tags_api == [su._TAGS_URL]
    assert payload['latest'] == '9.9.9'


@pytest.mark.parametrize('url, official', [
    ('https://github.com/{repo}.git', True),
    ('http://github.com/{repo}/', True),
    ('https://user:token@github.com/{repo}.git', True),
    ('ssh://git@github.com:22/{repo}.git', True),
    ('git@github.com:{repo}.git', True),
    ('https://github.com/{upper}.git', True),
    ('ssh://git@git.example.test/~team/tofu.git', False),
    ('https://github.com/{repo}-fork.git', False),
    ('https://github.com/{repo}/extra.git', False),
    ('https://github.com.example.test/{repo}.git', False),
    ('git@notgithub.com:{repo}.git', False),
    ('/srv/mirrors/github.com/{repo}.git', False),
])
def test_official_repository_url_forms(url, official):
    repo = su.UPDATE_REPO
    assert _status._is_official_github_url(
        url.format(repo=repo, upper=repo.upper())) is official
