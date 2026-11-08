"""Delivered working-tree bytes equal the checkpoint blob, including CR/CRLF.

Regression specs for the delivery patch transport: patches must travel as
bytes end to end (no text-mode capture), so carriage-return content in vLLM
progress-bar logs and CRLF files survives byte for byte.
"""
import hashlib
import subprocess

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from tests.storage.test_integration_delivery import (
    repo as repo_fixture, git, automatic_workspace, automatic_settle,
)

repo = repo_fixture
pytestmark = pytest.mark.unit


def checkpoint_blob(root, task_id, path):
    row = state.get_workspace(str(root), task_id, user_id=1)
    checkpoint = row['checkpoint_sha']
    assert checkpoint, row
    return git_bytes(root, 'show', f'{checkpoint}:{path}')


def git_bytes(root, *args):
    cp = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=20)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


def assert_delivered_bytes(root, task_id, path):
    """Publication succeeded and the working-tree file equals the blob exactly."""
    receipt = state.get_workspace(str(root), task_id, user_id=1)['origin']['delivery']
    assert receipt['state'] == 'delivered', receipt
    published = (root / path).read_bytes()
    expected = checkpoint_blob(root, task_id, path)
    assert hashlib.sha256(published).hexdigest() == hashlib.sha256(expected).hexdigest(), (
        f'published {len(published)} bytes (CR={published.count(bytes([13]))}), '
        f'checkpoint {len(expected)} bytes (CR={expected.count(bytes([13]))})')
    return published


PROGRESS_LOG = b''.join(
    b'progress %02d [##########] 100%%\r' % i for i in range(30))


def test_add_file_with_bare_carriage_returns_preserves_bytes(repo, monkeypatch):
    root, _work, _owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'serving.log').write_bytes(PROGRESS_LOG)
    automatic_settle(repo)
    assert_delivered_bytes(root, 'automatic', 'serving.log')
    assert (root / 'serving.log').read_bytes() == PROGRESS_LOG


def test_add_crlf_file_preserves_bytes(repo, monkeypatch):
    root, _work, _owner = repo
    work = automatic_workspace(repo, monkeypatch)
    expected = b''.join(b'row %02d\r\n' % i for i in range(12))
    (work / 'windows.txt').write_bytes(expected)
    automatic_settle(repo)
    published = assert_delivered_bytes(root, 'automatic', 'windows.txt')
    assert published == expected


def test_modify_existing_crlf_file_preserves_bytes(repo, monkeypatch):
    root, _work, _owner = repo
    baseline = b''.join(b'row %02d\r\n' % i for i in range(12))
    (root / 'windows.txt').write_bytes(baseline)
    git(root, 'add', 'windows.txt')
    git(root, 'commit', '-m', 'crlf baseline')
    work = automatic_workspace(repo, monkeypatch)
    # Baseline for the task must include the CRLF file; refresh the workspace.
    expected = baseline + b'appended 01\r\nappended 02\r\n'
    (work / 'windows.txt').write_bytes(expected)
    automatic_settle(repo)
    published = assert_delivered_bytes(root, 'automatic', 'windows.txt')
    assert published == expected


def test_modify_file_with_carriage_returns_in_middle_preserves_bytes(repo, monkeypatch):
    root, _work, _owner = repo
    original = b''.join(b'line %02d\n' % i for i in range(40))
    (root / 'app.log').write_bytes(original)
    git(root, 'add', 'app.log')
    git(root, 'commit', '-m', 'log baseline')
    work = automatic_workspace(repo, monkeypatch)
    edited = (b''.join(b'line %02d\n' % i for i in range(10))
              + b''.join(b'progress %02d [#####]\r' % i for i in range(20))
              + b''.join(b'line %02d\n' % i for i in range(10, 40)))
    (work / 'app.log').write_bytes(edited)
    automatic_settle(repo)
    published = assert_delivered_bytes(root, 'automatic', 'app.log')
    assert published == edited


def test_delivery_to_dirty_crlf_target_preserves_bytes(repo, monkeypatch):
    """Canonical tree holds uncommitted CR edits; delivery merges byte-exactly."""
    root, _work, _owner = repo
    (root / 'base.txt').write_bytes(b'base\n')
    work = automatic_workspace(repo, monkeypatch)
    expected = b''.join(b'progress %02d [#####]\r' % i for i in range(25))
    (work / 'serving.log').write_bytes(expected)
    # Concurrent canonical dirty content containing CRs on an unrelated path.
    (root / 'base.txt').write_bytes(b'canonical\r\nuser edit\r\n')
    git(root, 'add', 'base.txt')  # staged dirty bytes become part of the canonical source
    automatic_settle(repo)
    published = assert_delivered_bytes(root, 'automatic', 'serving.log')
    assert published == expected
    assert (root / 'base.txt').read_bytes() == b'canonical\r\nuser edit\r\n'
