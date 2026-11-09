"""The Internal-only built-in 大象 robot (owner decision 2026-11-08, docs/CHANNELS.md C6).

An Internal export packages the robot application from this host's owner-only
file at the channels contract's ``builtinCredentialFile``, identically for
every Internal target (company Dev platform and the private GitHub copy). The
source repository ignores that path, and the Open publication gate reads the
values from the same local file and refuses any Open tree, history blob,
archive member or commit text that holds either. Every value here is
synthetic; no source file names the real ones.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Synthetic negative-test anchors for the secret-scan checks, not repo files.
_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/x.md', 'lib/x.py'}

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

import export_pkg._history as history  # noqa: E402
import export_pkg._internal as internal  # noqa: E402
import export_pkg._publish as publish  # noqa: E402

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
PACKAGED = 'lib/channels/dx/robot_builtin.json'
CLIENT_ID = 'f00dfeed42'  # synthetic, shaped like an application client_id
SECRET = '0123456789abcdef0123456789abcdef'  # synthetic, shaped like a client secret


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    (root / 'data' / 'config').mkdir(parents=True)
    (root / 'contracts').mkdir()
    shutil.copy(REPO / 'contracts' / 'channels_v1.schema.json', root / 'contracts')
    monkeypatch.setattr(internal, 'ROOT', root)
    monkeypatch.setattr(publish, 'ROOT', root)
    return root


def write_values(root: Path) -> Path:
    path = root / 'data' / 'config' / 'dx_robot_builtin.json'
    path.write_text(json.dumps({'clientId': CLIENT_ID, 'secret': SECRET}), encoding='utf-8')
    path.chmod(0o600)
    return path


def test_the_contract_names_the_packaged_path():
    contract = json.loads((REPO / 'contracts' / 'channels_v1.schema.json').read_text(encoding='utf-8'))
    assert contract['x-dxRobot']['builtinCredentialFile'] == PACKAGED


def test_this_repository_can_never_commit_the_packaged_file():
    ignored = subprocess.run(['git', '-C', str(REPO), 'check-ignore', '-q', PACKAGED], check=False)
    assert ignored.returncode == 0


def test_an_internal_export_commits_the_builtin_robot(project, tmp_path):
    write_values(project)
    dest = tmp_path / 'dest'
    dest.mkdir()
    shutil.copy(REPO / '.gitignore', dest / '.gitignore')
    internal._package_builtin_robot(dest, 'internal')
    packaged = dest / PACKAGED
    assert json.loads(packaged.read_text(encoding='utf-8')) == {'clientId': CLIENT_ID, 'secret': SECRET}
    assert packaged.stat().st_mode & 0o777 == 0o600
    subprocess.run(['git', 'init', '-q', str(dest)], check=True)
    ignored = subprocess.run(['git', '-C', str(dest), 'check-ignore', '-q', PACKAGED], check=False)
    assert ignored.returncode == 1, 'the Internal tree stages the file for its snapshot push'
    internal._package_builtin_robot(dest, 'internal')
    assert (dest / '.gitignore').read_text(encoding='utf-8').count('BEGIN tofu internal built-in robot') == 1


def test_open_and_personal_exports_never_package_it(project, tmp_path):
    write_values(project)
    for mode in ('opensource', 'personal'):
        internal._package_builtin_robot(tmp_path / mode, mode)
        assert not (tmp_path / mode).exists()


def test_without_the_local_file_nothing_is_packaged(project, tmp_path):
    internal._package_builtin_robot(tmp_path / 'dest', 'internal')
    assert not (tmp_path / 'dest').exists()


def test_a_malformed_local_file_refuses_the_internal_export(project, tmp_path):
    (project / 'data' / 'config' / 'dx_robot_builtin.json').write_text('{"clientId": "x"}', encoding='utf-8')
    with pytest.raises(RuntimeError):
        internal._package_builtin_robot(tmp_path / 'dest', 'internal')


@pytest.mark.parametrize('content', [
    b'robot = {"clientId": "f00dfeed42"}\n',
    b'secret: 0123456789abcdef0123456789abcdef\n',
    b'https://example.invalid/app?appkey=f00dfeed42&x=1',
])
def test_the_open_gate_refuses_either_value_and_names_only_the_label(project, content, capsys):
    write_values(project)
    with pytest.raises(publish.ExportPublishSafetyError):
        publish._verify_opensource_blob(content, 'docs/x.md')
    printed = capsys.readouterr().out
    assert 'docs/x.md' in printed and CLIENT_ID not in printed and SECRET not in printed


def test_the_open_gate_passes_clean_bytes_and_longer_hex_runs(project):
    write_values(project)
    publish._verify_opensource_blob(b'commit 9f00dfeed42ab and nothing else', 'docs/x.md')
    publish._verify_opensource_blob(b'clean text', 'docs/x.md')


def test_the_open_tree_scan_refuses_the_values(project, tmp_path):
    write_values(project)
    tree = tmp_path / 'open'
    (tree / 'docs').mkdir(parents=True)
    (tree / 'docs' / 'a.md').write_text(f'leak {SECRET}\n', encoding='utf-8')
    with pytest.raises(publish.ExportPublishSafetyError):
        publish._verify_opensource(tree)


def test_open_history_blobs_and_archive_members_are_refused(project):
    write_values(project)
    with pytest.raises(publish.ExportPublishSafetyError):
        history._check_blob(f'id={CLIENT_ID}\n'.encode(), 'lib/x.py', history._HistoryBudget())
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, 'w') as archive:
        archive.writestr('inner/config.json', json.dumps({'secret': SECRET}))
    with pytest.raises(publish.ExportPublishSafetyError):
        history._check_blob(packed.getvalue(), 'vendor/pkg.whl', history._HistoryBudget())


def test_an_unreadable_local_file_fails_the_open_gate_closed(project):
    (project / 'data' / 'config' / 'dx_robot_builtin.json').write_text('not json', encoding='utf-8')
    with pytest.raises(publish.ExportPublishSafetyError):
        publish._verify_opensource_blob(b'clean text', 'docs/x.md')


def test_without_the_local_file_the_open_gate_is_unchanged(project):
    publish._verify_opensource_blob(f'{CLIENT_ID} {SECRET}'.encode(), 'docs/x.md')
