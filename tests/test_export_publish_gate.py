"""Public-export publication gate: only code/templates may reach Git."""

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


def test_clean_code_and_documented_placeholders_pass(tmp_path):
    from export import _publish_tree_findings, _verify_publish_tree

    (tmp_path / 'lib').mkdir()
    (tmp_path / 'lib' / 'feature.py').write_text('VALUE = 1\n', encoding='utf-8')
    (tmp_path / 'data' / 'config').mkdir(parents=True)
    (tmp_path / 'data' / '.gitkeep').touch()
    (tmp_path / 'data' / 'config' / '.gitkeep').touch()
    (tmp_path / 'uploads' / 'images').mkdir(parents=True)
    (tmp_path / 'uploads' / 'images' / '.gitkeep').touch()

    forbidden, oversized, total = _publish_tree_findings(tmp_path)
    assert forbidden == []
    assert oversized == []
    assert total == len('VALUE = 1\n')
    _verify_publish_tree(tmp_path)


@pytest.mark.parametrize(
    'rel',
    [
        'data/config/server_config.json',
        'logs/app.log',
        'uploads/images/private.png',
        '.env',
        '.env.production',
        'config/credentials_vault.json',
        'config/mcp_servers.json',
        'certs/service.pem',
        'cache/history.sqlite3',
    ],
)
def test_runtime_data_and_real_configuration_are_blocked(tmp_path, rel):
    from export import ExportPublishSafetyError, _verify_publish_tree

    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('private\n', encoding='utf-8')

    with pytest.raises(ExportPublishSafetyError, match='forbidden data/config paths'):
        _verify_publish_tree(tmp_path)


def test_opensource_skeleton_keeps_configuration_as_template(tmp_path, monkeypatch):
    import export
    import export_pkg._export_core as export_core

    monkeypatch.setattr(export_core, '_bundle_internal_mcp_repos', lambda *args: None)
    monkeypatch.setattr(export_core, '_bundle_tofu_search_wheel', lambda *args: None)
    monkeypatch.setattr(export_core, '_portablize_bundled_mcp_config', lambda *args: None)
    monkeypatch.setattr(export_core, '_patch_install_sh_proxy', lambda *args: None)

    export._create_skeleton(tmp_path, 'opensource')

    assert not (tmp_path / '.env').exists()
    assert (tmp_path / '.env.example').is_file()
    export._verify_publish_tree(tmp_path)


def test_fifty_mib_file_is_blocked_without_allocating_payload(tmp_path):
    from export import (
        ExportPublishSafetyError,
        _PUBLISH_MAX_FILE_BYTES,
        _publish_tree_findings,
        _verify_publish_tree,
    )

    target = tmp_path / 'static' / 'huge.bin'
    target.parent.mkdir()
    with target.open('wb') as fh:
        fh.truncate(_PUBLISH_MAX_FILE_BYTES)

    _, oversized, _ = _publish_tree_findings(tmp_path)
    assert oversized == [('static/huge.bin', _PUBLISH_MAX_FILE_BYTES)]
    with pytest.raises(ExportPublishSafetyError, match='at or above 50 MiB'):
        _verify_publish_tree(tmp_path)


def test_gitignored_runtime_data_is_not_a_publish_candidate(tmp_path):
    import subprocess

    from export import _publish_tree_findings

    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / '.gitignore').write_text('/data/\n.env\n', encoding='utf-8')
    (tmp_path / 'README.md').write_text('safe\n', encoding='utf-8')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'private.db').write_text('private\n', encoding='utf-8')
    (tmp_path / '.env').write_text('TOKEN=private\n', encoding='utf-8')

    forbidden, oversized, total = _publish_tree_findings(tmp_path)
    assert forbidden == []
    assert oversized == []
    assert total == len('/data/\n.env\n') + len('safe\n')


def test_tracked_runtime_data_is_blocked_even_when_ignored_later(tmp_path):
    import subprocess

    from export import ExportPublishSafetyError, _verify_publish_tree

    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'private.db').write_text('private\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-f', 'data/private.db'], cwd=tmp_path, check=True)
    (tmp_path / '.gitignore').write_text('/data/\n', encoding='utf-8')

    with pytest.raises(ExportPublishSafetyError, match='data/private.db'):
        _verify_publish_tree(tmp_path)


def test_git_metadata_is_not_counted_or_scanned(tmp_path):
    import subprocess

    from export import _publish_tree_findings

    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / '.git' / 'objects' / 'large.db').write_text('x', encoding='utf-8')
    (tmp_path / 'README.md').write_text('safe\n', encoding='utf-8')

    forbidden, oversized, total = _publish_tree_findings(tmp_path)
    assert forbidden == []
    assert oversized == []
    assert total == len('safe\n')


def test_secret_verification_is_fail_closed(tmp_path, monkeypatch):
    import export

    (tmp_path / 'README.md').write_text('safe\n', encoding='utf-8')

    def unreadable(*args, **kwargs):
        raise OSError('synthetic candidate enumeration failure')

    import export_pkg._publish as publish

    monkeypatch.setattr(publish, '_publish_candidate_paths', unreadable)

    with pytest.raises(export.ExportPublishSafetyError,
                       match='synthetic candidate enumeration failure'):
        export._verify_opensource(tmp_path)


def test_secret_verification_blocks_a_detected_leak(tmp_path, monkeypatch):
    import export

    secret = next(iter(export._SECRETS))
    (tmp_path / 'config.txt').write_text(secret + '\n', encoding='utf-8')

    with pytest.raises(export.ExportPublishSafetyError, match='1 leak'):
        export._verify_opensource(tmp_path)


def test_secret_verification_blocks_generic_token_format(tmp_path):
    import export

    token = 'ghp_' + 'A' * 36
    (tmp_path / 'config.txt').write_text(token + '\n', encoding='utf-8')

    with pytest.raises(export.ExportPublishSafetyError, match='1 leak'):
        export._verify_opensource(tmp_path)


def test_private_key_placeholder_is_allowed_but_material_is_blocked(tmp_path):
    import export

    target = tmp_path / 'config.md'
    target.write_text(
        'private_key_pem: "-----BEGIN RSA PRIVATE KEY-----..."\n',
        encoding='utf-8',
    )
    export._verify_opensource(tmp_path)

    target.write_text(
        '-----BEGIN RSA PRIVATE KEY-----\n' + 'A' * 64 + '\n',
        encoding='utf-8',
    )
    with pytest.raises(export.ExportPublishSafetyError, match='1 leak'):
        export._verify_opensource(tmp_path)


def test_git_push_runs_gate_before_git_add(tmp_path, monkeypatch):
    import export

    calls = []

    def reject(_dest):
        calls.append('gate')
        raise export.ExportPublishSafetyError('blocked')

    def must_not_run(*args, **kwargs):
        calls.append('subprocess')
        raise AssertionError('git must not run after a failed publication gate')

    import export_pkg._publish as publish

    monkeypatch.setattr(publish, '_verify_publish_tree', reject)
    monkeypatch.setattr(export.subprocess, 'run', must_not_run)

    with pytest.raises(export.ExportPublishSafetyError, match='blocked'):
        export._git_push(tmp_path, 'opensource')
    assert calls == ['gate']


def test_git_push_propagates_staged_gate_failure(tmp_path, monkeypatch):
    import export

    import export_pkg._publish as publish

    monkeypatch.setattr(publish, '_verify_publish_tree', lambda dest: None)
    monkeypatch.setattr(
        publish, '_verify_staged_publish_snapshot',
        lambda dest: (_ for _ in ()).throw(export.ExportPublishSafetyError('blocked index')),
    )
    monkeypatch.setitem(
        export._GIT_REPOS, 'opensource',
        {'remotes': [{'name': 'origin', 'url': str(tmp_path / 'remote.git')}],
         'branch': 'main'},
    )
    (tmp_path / 'README.md').write_text('safe\n', encoding='utf-8')

    with pytest.raises(export.ExportPublishSafetyError, match='blocked index'):
        export._git_push(tmp_path, 'opensource')


def test_git_push_blocks_commit_tree_race_before_push(tmp_path, monkeypatch):
    import subprocess

    import export

    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'README.md').write_text('safe\n', encoding='utf-8')

    import export_pkg._publish as publish

    monkeypatch.setattr(publish, '_verify_publish_tree', lambda dest: None)
    monkeypatch.setattr(publish, '_verify_staged_publish_snapshot', lambda dest: '0' * 40)
    monkeypatch.setitem(
        export._GIT_REPOS, 'opensource',
        {'remotes': [{'name': 'origin', 'url': str(remote)}], 'branch': 'main'},
    )

    with pytest.raises(export.ExportPublishSafetyError,
                       match='Git index changed after publication verification'):
        export._git_push(repo, 'opensource')
    assert subprocess.run(
        ['git', '--git-dir', str(remote), 'show-ref'],
        capture_output=True, text=True,
    ).stdout == ''


def test_staged_snapshot_blocks_tracked_runtime_data(tmp_path):
    import subprocess

    from export import ExportPublishSafetyError, _verify_staged_publish_snapshot

    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'private.db').write_text('private\n', encoding='utf-8')
    subprocess.run(['git', 'add', '-f', 'data/private.db'], cwd=tmp_path, check=True)

    with pytest.raises(ExportPublishSafetyError, match='data/private.db'):
        _verify_staged_publish_snapshot(tmp_path)


def test_staged_snapshot_blocks_symlink(tmp_path):
    import os
    import subprocess

    from export import ExportPublishSafetyError, _verify_staged_publish_snapshot

    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'target.txt').write_text('safe\n', encoding='utf-8')
    os.symlink('target.txt', tmp_path / 'linked.txt')
    subprocess.run(['git', 'add', 'target.txt', 'linked.txt'], cwd=tmp_path, check=True)

    with pytest.raises(ExportPublishSafetyError, match='unsupported Git entries'):
        _verify_staged_publish_snapshot(tmp_path)


def test_gate_is_wired_after_export_transforms_and_before_push():
    import inspect

    import export

    source = inspect.getsource(export.export_project)
    anchor = source.index('# Post-export tasks: lint (opensource only), verify, push.')
    start = source.index("        if mode == 'opensource':", anchor)
    end = source.index('        return', start)
    window = source[start:end]

    assert window.index('_restore_opensource_kept_files(dest)') < window.index(
        '_verify_publish_tree(dest)')
    assert window.index('_verify_opensource(dest)') < window.index(
        '_verify_publish_tree(dest)')
    assert window.index('_verify_publish_tree(dest)') < window.index(
        '_git_push(dest, mode')

    push_window = inspect.getsource(export._git_push)
    assert push_window.index("_run(['git', 'add', '-A'])") < push_window.index(
        '_verify_staged_publish_snapshot(dest)')
    assert push_window.index('_verify_staged_publish_snapshot(dest)') < push_window.index(
        "_run(['git', 'commit', '-m', commit_msg])")
    assert push_window.index("_run(['git', 'commit', '-m', commit_msg])") < push_window.index(
        "_run(['git', 'rev-parse', 'HEAD^{tree}'])")
    assert push_window.index("_run(['git', 'rev-parse', 'HEAD^{tree}'])") < push_window.index(
        '_push_branch(_run, rname, branch')
