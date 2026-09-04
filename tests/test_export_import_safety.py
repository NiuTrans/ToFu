"""Export tooling is import-safe, while authenticated publication fails closed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')
pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


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
