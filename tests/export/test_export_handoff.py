#!/usr/bin/env python3
"""tests/export/test_export_handoff.py — cross-cluster handoff artifacts.

WHY
---
A full internal export to a *cross-cluster* FUSE path (zw05 → sh02) is
latency-bound (~1 h for ~10k files; every create/stat is a WAN round
trip).  The handoff flow replaces the byte copy with git as transport:
``export.py --handoff DIR --git-url URL`` writes only two small
artifacts — ``bootstrap.sh`` (clone/pull + state restore + install) and
``tofu_state.tar.gz`` (gitignored user state: skills, memories, seeded
config) — which the remote host consumes locally.

Guards:
  * both artifacts are written, bootstrap is executable and carries the
    clone URL/branch/proxy but NO credential material;
  * the state tarball carries skills/memories and a config seeded
    exactly like the full export (internal = whitelisted shared keys,
    personal = full config + MCP tokens);
  * opensource mode is rejected (public transfer is GitHub, no state);
  * a dirty worktree only warns — committed state is the contract.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

import export_pkg._handoff as handoff  # noqa: E402
import export_pkg._internal as internal  # noqa: E402

pytestmark = pytest.mark.unit

_GIT_URL = 'https://dev.internal.example.com/example/tofu.git'


def _make_fake_project(root: Path):
    (root / 'data' / 'skills' / 'users' / '1' / 'skill-a').mkdir(parents=True)
    (root / 'data' / 'skills' / 'users' / '1' / 'skill-a' / 'SKILL.md').write_text(
        '# skill-a\n', encoding='utf-8')
    (root / 'data' / 'memories' / 'global').mkdir(parents=True)
    (root / 'data' / 'memories' / 'global' / 'mem.md').write_text('memo\n', encoding='utf-8')
    (root / '.tofu' / 'memories').mkdir(parents=True)
    (root / '.tofu' / 'memories' / 'proj.md').write_text('proj memo\n', encoding='utf-8')
    (root / '.tofu' / 'error_resolutions.json').write_text('{}', encoding='utf-8')
    (root / 'data' / 'config').mkdir(parents=True)
    (root / 'data' / 'config' / 'server_config.json').write_text(json.dumps({
        'providers': [
            {'id': 'default', 'name': 'Default', 'api_keys': ['shared-key']},
            {'id': 'openai', 'name': 'Personal', 'api_keys': ['personal-key']},
        ],
        'proxy_config': {'http_proxy': 'http://proxy.corp:3128'},
        'proxy_bypass_domains': ['corp.local'],
        'feishu': {'app_id': 'personal-feishu'},
    }), encoding='utf-8')
    (root / 'data' / 'config' / 'mcp_servers.json').write_text(
        '{"personal": true}', encoding='utf-8')


@pytest.fixture()
def fake_project(tmp_path, monkeypatch):
    proj = tmp_path / 'proj'
    proj.mkdir()
    _make_fake_project(proj)
    monkeypatch.setattr(handoff, 'ROOT', proj)
    monkeypatch.setattr(internal, 'ROOT', proj)
    monkeypatch.setattr(handoff, '_current_branch', lambda: 'main')
    monkeypatch.setattr(handoff, '_remote_branch_exists', lambda url, branch: True)
    monkeypatch.setattr(handoff, '_dirty_tracked_files', lambda root: [])
    return proj


def _tar_members(tar_path: Path) -> set[str]:
    with tarfile.open(tar_path, 'r:gz') as tf:
        return set(tf.getnames())


def _tar_json(tar_path: Path, name: str):
    with tarfile.open(tar_path, 'r:gz') as tf:
        f = tf.extractfile(name)
        assert f is not None, name
        return json.loads(io.TextIOWrapper(f, encoding='utf-8').read())


class TestHandoffArtifacts:

    def test_internal_writes_bootstrap_and_state(self, fake_project, tmp_path):
        out = tmp_path / 'handoff'
        handoff.run_handoff('internal', out, _GIT_URL, push=False)

        script = (out / 'bootstrap.sh').read_text(encoding='utf-8')
        assert os.stat(out / 'bootstrap.sh').st_mode & 0o111
        assert _GIT_URL in script
        # Internal always clones the release branch (master) — colleagues'
        # repo must hold the sanitized snapshot, never a raw dev branch.
        assert "BRANCH='master'" in script
        # Corp proxy baked for the remote git clone / install.
        assert 'export http_proxy="http://proxy.corp:3128"' in script
        # Token mechanism is env-driven — no credential material baked in.
        assert '${DEV_SANKUAI_TOKEN:-}' in script
        assert 'shared-key' not in script and 'personal-key' not in script

        members = _tar_members(out / 'tofu_state.tar.gz')
        assert 'data/skills/users/1/skill-a/SKILL.md' in members
        assert 'data/memories/global/mem.md' in members
        assert '.tofu/memories/proj.md' in members
        assert '.tofu/error_resolutions.json' in members

        # Internal mode: config seeded like the full export — whitelisted
        # keys only, providers filtered to the internal set, no personal
        # config files.
        cfg = _tar_json(out / 'tofu_state.tar.gz', 'data/config/server_config.json')
        assert 'feishu' not in cfg
        assert [p['id'] for p in cfg['providers']] == ['example-corp']
        assert 'data/config/mcp_servers.json' not in members

    def test_internal_bakes_corp_proxy_default_when_source_has_none(self, fake_project, tmp_path):
        # Exporter's shell often carries the proxy in env, not in
        # server_config.json — no-egress target hosts still need it baked.
        cfg_path = fake_project / 'data' / 'config' / 'server_config.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        del cfg['proxy_config']
        del cfg['proxy_bypass_domains']
        cfg_path.write_text(json.dumps(cfg), encoding='utf-8')

        out = tmp_path / 'handoff'
        handoff.run_handoff('internal', out, _GIT_URL, push=False)

        script = (out / 'bootstrap.sh').read_text(encoding='utf-8')
        assert f'export http_proxy="{internal._INTERNAL_CORP_PROXY}"' in script
        assert 'export TOFU_CONDA_MIRROR=' in script
        seeded = _tar_json(out / 'tofu_state.tar.gz', 'data/config/server_config.json')
        assert seeded['proxy_config']['http_proxy'] == internal._INTERNAL_CORP_PROXY
        assert '.internal.example.com' in seeded['proxy_bypass_domains']

    def test_internal_explicit_proxy_wins_over_default(self, fake_project, tmp_path):
        out = tmp_path / 'handoff'
        handoff.run_handoff('internal', out, _GIT_URL, push=False)
        script = (out / 'bootstrap.sh').read_text(encoding='utf-8')
        assert 'export http_proxy="http://proxy.corp:3128"' in script
        assert internal._INTERNAL_CORP_PROXY not in script
        seeded = _tar_json(out / 'tofu_state.tar.gz', 'data/config/server_config.json')
        assert seeded['proxy_config']['http_proxy'] == 'http://proxy.corp:3128'

    def test_personal_gets_no_corp_proxy_default(self, fake_project, tmp_path):
        cfg_path = fake_project / 'data' / 'config' / 'server_config.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        del cfg['proxy_config']
        del cfg['proxy_bypass_domains']
        cfg_path.write_text(json.dumps(cfg), encoding='utf-8')

        out = tmp_path / 'handoff'
        handoff.run_handoff('personal', out, _GIT_URL, push=False)
        script = (out / 'bootstrap.sh').read_text(encoding='utf-8')
        assert internal._INTERNAL_CORP_PROXY not in script
        assert 'export http_proxy' not in script

    def test_install_sh_proxy_injection_internal_default(self, fake_project, tmp_path):
        # _patch_install_sh_proxy: an internal export whose source config has
        # no proxy still bakes the corp default into the exported install.sh.
        cfg_path = fake_project / 'data' / 'config' / 'server_config.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        del cfg['proxy_config']
        del cfg['proxy_bypass_domains']
        cfg_path.write_text(json.dumps(cfg), encoding='utf-8')

        dest = tmp_path / 'dest'
        dest.mkdir()
        (dest / 'install.sh').write_text(
            '#!/bin/bash\nset -euo pipefail\necho hi\n', encoding='utf-8')
        internal._patch_install_sh_proxy(dest, 'internal')
        text = (dest / 'install.sh').read_text(encoding='utf-8')
        assert f'export http_proxy="{internal._INTERNAL_CORP_PROXY}"' in text

        # Personal mode with the same proxy-less source stays untouched.
        (dest / 'install.sh').write_text(
            '#!/bin/bash\nset -euo pipefail\necho hi\n', encoding='utf-8')
        internal._patch_install_sh_proxy(dest, 'personal')
        assert 'export http_proxy' not in (dest / 'install.sh').read_text(encoding='utf-8')

    def test_personal_carries_full_config_and_mcp_servers(self, fake_project, tmp_path):
        out = tmp_path / 'handoff'
        handoff.run_handoff('personal', out, _GIT_URL, push=False)

        members = _tar_members(out / 'tofu_state.tar.gz')
        cfg = _tar_json(out / 'tofu_state.tar.gz', 'data/config/server_config.json')
        assert cfg['feishu'] == {'app_id': 'personal-feishu'}
        assert {p['id'] for p in cfg['providers']} == {'example-corp', 'openai'}
        assert 'data/config/mcp_servers.json' in members

    def test_internal_defaults_to_release_repo(self, fake_project, tmp_path):
        out = tmp_path / 'handoff'
        handoff.run_handoff('internal', out, None, push=False)
        script = (out / 'bootstrap.sh').read_text(encoding='utf-8')
        assert 'ssh://git@git.internal.example.com/~your-username/tofu.git' in script
        assert "BRANCH='master'" in script

    def test_personal_requires_git_url(self, fake_project, tmp_path):
        with pytest.raises(SystemExit):
            handoff.run_handoff('personal', tmp_path / 'handoff', None)

    def test_opensource_rejected(self, fake_project, tmp_path):
        with pytest.raises(SystemExit):
            handoff.run_handoff('opensource', tmp_path / 'handoff', _GIT_URL)
        assert not (tmp_path / 'handoff').exists()

    def test_internal_push_failure_is_fatal(self, fake_project, tmp_path, monkeypatch):
        monkeypatch.setattr('export_pkg._orchestrate.export_project',
                            lambda *a, **k: None)
        monkeypatch.setattr(handoff, '_snapshot_push',
                            lambda dest, url, branch: 'auth denied')
        with pytest.raises(SystemExit):
            handoff.run_handoff('internal', tmp_path / 'handoff', push=True)
        # Nothing is written when the remote side would clone a stale ref.
        assert not (tmp_path / 'handoff').exists()

    def test_personal_push_failure_is_fatal(self, fake_project, tmp_path, monkeypatch):
        monkeypatch.setattr(handoff, '_push_head', lambda url, branch: 'auth denied')
        with pytest.raises(SystemExit):
            handoff.run_handoff('personal', tmp_path / 'handoff', _GIT_URL, push=True)
        assert not (tmp_path / 'handoff').exists()

    def test_snapshot_push_preserves_history(self, tmp_path):
        """Real-git check: snapshot-push lands the export tree as ONE commit
        on top of the existing remote history (fast-forward, never force)."""
        import subprocess

        def git(args, cwd):
            proc = subprocess.run(['git', *args], cwd=str(cwd),
                                  capture_output=True, text=True)
            assert proc.returncode == 0, (args, proc.stderr)
            return proc.stdout.strip()

        # Remote with an existing master history (a prior release).
        remote = tmp_path / 'remote.git'
        seed = tmp_path / 'seed'
        seed.mkdir()
        git(['init', '-b', 'master'], seed)
        git(['config', 'user.email', 't@t'], seed)
        git(['config', 'user.name', 't'], seed)
        (seed / 'old.txt').write_text('v1\n')
        git(['add', '-A'], seed)
        git(['commit', '-m', 'v1'], seed)
        git(['init', '--bare', str(remote)], tmp_path)
        git(['remote', 'add', 'origin', str(remote)], seed)
        git(['push', 'origin', 'master'], seed)

        dest = tmp_path / 'export'
        dest.mkdir()
        (dest / 'new.txt').write_text('v2\n')
        assert handoff._snapshot_push(dest, str(remote), 'master') is None

        log = git(['--git-dir', str(remote), 'log', '--format=%s', 'master'], tmp_path)
        commits = log.splitlines()
        assert len(commits) == 2 and commits[1] == 'v1'
        tree = git(['--git-dir', str(remote), 'ls-tree', '-r', '--name-only', 'master'], tmp_path)
        assert 'new.txt' in tree and 'old.txt' not in tree

        # Repeat push is a fast-forward on top (no force, history grows).
        (dest / 'new.txt').write_text('v3\n')
        assert handoff._snapshot_push(dest, str(remote), 'master') is None
        log = git(['--git-dir', str(remote), 'log', '--format=%s', 'master'], tmp_path)
        assert len(log.splitlines()) == 3

    def test_dry_run_writes_nothing(self, fake_project, tmp_path):
        out = tmp_path / 'handoff'
        handoff.run_handoff('internal', out, _GIT_URL, dry_run=True)
        assert not out.exists()

    def test_personal_worktree_rejected(self, fake_project, tmp_path):
        with pytest.raises(SystemExit):
            handoff.run_handoff('personal', tmp_path / 'handoff', _GIT_URL,
                                worktree=True)

    def test_internal_worktree_stages_untracked(self, fake_project, tmp_path,
                                                monkeypatch):
        """--worktree internal handoff: the release mirror must include the
        untracked source files the committed-source tar rule skips — a dirty
        tracked file importing one is the 2026-08-06 torn-snapshot incident.
        Gitignored junk must NOT ride along."""
        import subprocess

        proj = fake_project

        def git(args):
            proc = subprocess.run(['git', *args], cwd=str(proj),
                                  capture_output=True, text=True)
            assert proc.returncode == 0, (args, proc.stderr)

        git(['init', '-b', 'main'])
        git(['config', 'user.email', 't@t'])
        git(['config', 'user.name', 't'])
        (proj / 'lib').mkdir()
        (proj / 'lib' / 'tracked.py').write_text('OLD\n')
        (proj / '.gitignore').write_text('junk.txt\n')
        git(['add', '-A'])
        git(['commit', '-m', 'base'])
        (proj / 'lib' / 'tracked.py').write_text('NEW\n')          # dirty tracked
        (proj / 'lib' / 'untracked_mod.py').write_text('X = 1\n')  # untracked source
        (proj / 'junk.txt').write_text('junk\n')                   # gitignored

        export_dest = tmp_path / 'export-dest'
        monkeypatch.setattr(handoff, '_INTERNAL_EXPORT_DEST', export_dest)
        seen = {}

        def fake_export(mode, dest, **kwargs):
            seen.update(kwargs)
            # The tar copy ships WORKTREE bytes of tracked files only.
            (dest / 'lib').mkdir(parents=True, exist_ok=True)
            (dest / 'lib' / 'tracked.py').write_text('NEW\n')

        monkeypatch.setattr('export_pkg._orchestrate.export_project', fake_export)
        monkeypatch.setattr(handoff, '_snapshot_push',
                            lambda dest, url, branch: None)
        handoff.run_handoff('internal', tmp_path / 'handoff', push=True,
                            worktree=True)

        assert seen.get('worktree') is True
        assert (export_dest / 'lib' / 'untracked_mod.py').read_text() == 'X = 1\n'
        assert not (export_dest / 'junk.txt').exists()

    def test_internal_committed_handoff_stages_no_untracked(self, fake_project,
                                                            tmp_path, monkeypatch):
        """Without --worktree the committed-source contract is unchanged:
        export_project gets worktree=False and no untracked staging runs."""
        export_dest = tmp_path / 'export-dest'
        monkeypatch.setattr(handoff, '_INTERNAL_EXPORT_DEST', export_dest)
        seen = {}

        def fake_export(mode, dest, **kwargs):
            seen.update(kwargs)
            dest.mkdir(parents=True, exist_ok=True)

        def fail_stage(src, dest):
            raise AssertionError('untracked staging must not run without --worktree')

        monkeypatch.setattr('export_pkg._orchestrate.export_project', fake_export)
        monkeypatch.setattr(handoff, '_stage_worktree_untracked', fail_stage)
        monkeypatch.setattr(handoff, '_snapshot_push',
                            lambda dest, url, branch: None)
        handoff.run_handoff('internal', tmp_path / 'handoff', push=True)
        assert seen.get('worktree') is False


if __name__ == '__main__':
    unittest.main()
