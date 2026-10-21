"""Export tooling is import-safe, while authenticated publication fails closed."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')
pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent.parent


def test_export_import_does_not_resolve_github_token(tmp_path):
    """A clean checkout without env, vault, or legacy token must import."""
    probe = r'''
from pathlib import Path
import sys

import export_pkg._lists as lists
import lib.credentials_vault as vault

lists.ROOT = Path(sys.argv[1]) / 'checkout'
vault.bootstrap_from_legacy = lambda _path: None
vault.get_entry = lambda _name: None

import export
assert export._GH_TOKEN is None
print('OK')
'''
    env = dict(os.environ)
    env.pop('TOFU_GH_TOKEN', None)
    result = subprocess.run(
        [sys.executable, '-c', probe, str(tmp_path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'OK'


def test_authenticated_publish_refuses_missing_token(tmp_path, monkeypatch):
    import export
    import export_pkg._publish as publish

    monkeypatch.setattr(publish, '_verify_publish_tree', lambda _dest: None)
    monkeypatch.setitem(
        export._GIT_REPOS,
        'opensource',
        {
            'remotes': [{
                'name': 'origin',
                'url': 'https://github.com/example/project.git',
                'credential': 'github_token',
            }],
            'branch': 'main',
        },
    )
    monkeypatch.setattr(
        publish,
        '_load_gh_token',
        lambda: (_ for _ in ()).throw(SystemExit('token unavailable')),
    )
    monkeypatch.setattr(
        export.subprocess,
        'run',
        lambda *_args, **_kwargs: pytest.fail(
            'missing credentials must fail before git mutates the destination'),
    )

    with pytest.raises(SystemExit, match='token unavailable'):
        export._git_push(tmp_path, 'opensource')


def test_resolver_exposes_auth_url_and_keeps_url_clean(monkeypatch):
    """Credentials ride in ``auth_url`` (memory only); ``url`` stays clean so
    the remote-add step can never persist a token to .git/config."""
    import export_pkg._publish as publish

    monkeypatch.setattr(publish, '_load_gh_token', lambda: 'ghp_ResolverProbeToken000')
    resolved = publish._resolve_authenticated_remotes([
        {'name': 'origin',
         'url': 'https://github.com/example/project.git',
         'credential': 'github_token'},
        {'name': 'mirror', 'url': 'https://example.com/plain.git'},
    ])
    assert resolved[0]['url'] == 'https://github.com/example/project.git'
    assert resolved[0]['auth_url'] == (
        'https://ghp_ResolverProbeToken000@github.com/example/project.git')
    assert resolved[1] == {'name': 'mirror', 'url': 'https://example.com/plain.git'}


def test_redact_url_credentials():
    from export_pkg._publish import _redact_url_credentials

    assert _redact_url_credentials(
        'git push https://ghp_secretTOKEN123@github.com/o/r.git main\n'
        "fatal: unable to access 'https://ghp_secretTOKEN123@github.com/o/r.git'") == (
        'git push https://***@github.com/o/r.git main\n'
        "fatal: unable to access 'https://***@github.com/o/r.git'")
    assert _redact_url_credentials('https://github.com/o/r.git') == (
        'https://github.com/o/r.git')


def test_push_branch_url_target_never_sets_upstream():
    """``git push -u <url>`` writes branch.<name>.remote=<url> into .git/config
    — with a credentialed URL that is a token leak, so URL targets skip the
    upstream retry and classify the failure directly."""
    from export_pkg._publish import _push_branch

    calls = []

    def git_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ['git', 'push'] and '-u' in cmd:
            raise AssertionError('-u persists the credentialed URL into .git/config')
        if cmd[:2] == ['git', 'push'] and len(calls) == 1:
            raise RuntimeError('error: failed to push some refs — fetch first')

    target = 'https://ghp_x@example.com/r.git'
    _push_branch(git_run, target, 'main', force=False)
    pushes = [cmd for cmd in calls if cmd[:2] == ['git', 'push']]
    assert len(pushes) == 2
    assert all('-u' not in cmd and '--set-upstream' not in cmd for cmd in pushes)
    assert all(cmd[-2:] == [target, 'main'] for cmd in pushes)
    assert any(cmd[:2] == ['git', 'fetch'] and cmd[-2:] == [target, 'main']
               for cmd in calls)
    assert any(cmd[:2] == ['git', 'merge'] for cmd in calls)


def test_push_branch_named_remote_keeps_upstream_retry():
    """Named remotes keep the original -u fallback — only URL targets skip it."""
    from export_pkg._publish import _push_branch

    calls = []

    def git_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ['git', 'push'] and '-u' not in cmd:
            raise RuntimeError('error: failed to push some refs — fetch first')

    _push_branch(git_run, 'origin', 'main', force=False)
    pushes = [cmd for cmd in calls if cmd[:2] == ['git', 'push']]
    assert len(pushes) == 2
    assert '-u' not in pushes[0] and '-u' in pushes[1]
    assert all(cmd[-2:] == ['origin', 'main'] for cmd in pushes)
    assert not any(cmd[:2] in (['git', 'fetch'], ['git', 'merge']) for cmd in calls)


@pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')
def test_git_push_never_persists_credentials(tmp_path, monkeypatch, capsys):
    """End-to-end: a credentialed publish leaves .git/config credential-free,
    and a failing push leaks no token to the console."""
    import export
    import export_pkg._publish as publish

    token = 'ghp_ConfigHygieneProbe0000000000000000'
    monkeypatch.setattr(publish, '_verify_publish_tree', lambda _dest: None)
    monkeypatch.setattr(publish, '_load_gh_token', lambda: token)
    monkeypatch.setitem(
        export._GIT_REPOS,
        'opensource',
        {
            'remotes': [{
                'name': 'origin',
                # Push errors are injected below; no real network is used.
                'url': 'https://127.0.0.1:1/example/project.git',
                'credential': 'github_token',
            }],
            'branch': 'main',
        },
    )
    monkeypatch.setenv('GIT_TERMINAL_PROMPT', '0')
    for var in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                'all_proxy', 'ALL_PROXY'):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / 'README.md').write_text('safe\n', encoding='utf-8')

    real_run = publish.subprocess.run

    def fail_push(cmd, **kwargs):
        if cmd[:2] == ['git', 'push']:
            target = next(value for value in cmd if value.startswith('https://'))
            assert token in target
            return subprocess.CompletedProcess(cmd, 1, '',
                                               'synthetic authentication failed: ' + target)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(publish.subprocess, 'run', fail_push)
    with pytest.raises(RuntimeError, match='synthetic authentication failed') as error:
        export._git_push(tmp_path, 'opensource')
    assert token not in str(error.value)

    cfg = subprocess.run(
        ['git', '-C', str(tmp_path), 'config', '--local', '--list'],
        capture_output=True, text=True, check=True,
    ).stdout
    assert token not in cfg
    assert not re.search(r'https://[^/@\s]+@', cfg), cfg
    assert token not in capsys.readouterr().out
