"""Immediate workspace delivery is independent of Git commits/release gates."""
from pathlib import Path
import subprocess

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery, task_delta_delivery
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit


def finish(root, task='automatic', outcome='completed'):
    return delivery.settle_task_workspace(str(root), task, user_id=1, outcome=outcome)


def test_completion_returns_confirmed_workspace_delivery_without_worker_or_gates(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.py').write_text('answer = 42\n')
    head = git(root, 'rev-parse', 'HEAD')
    index = (root / '.git/index').read_bytes()
    calls = []
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **k: pytest.fail('release gate blocks workspace delivery'))
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: calls.append('worker'))
    original = ic._git
    def observe(checkout, args, **kwargs):
        assert args[:2] != ['worktree', 'add'], 'delivery materialized a checkout'
        return original(checkout, args, **kwargs)
    monkeypatch.setattr(ic, '_git', observe)
    result = finish(root)
    assert result['state'] == 'delivered', result
    assert result['publicationMode'] == 'working_tree'
    assert (root / 'new.py').read_text() == 'answer = 42\n'
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index
    assert calls == []


def test_borrowed_dirty_and_staged_context_accepts_task_delta(repo, monkeypatch):
    root, _, _ = repo
    (root / 'base.txt').write_text('staged user work\n')
    git(root, 'add', 'base.txt')
    (root / 'base.txt').write_text('unstaged user work\n')
    (root / 'user.txt').write_text('untracked input\n')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('unstaged user work\ntask addition\n')
    (work / 'user.txt').write_text('untracked input\ntask addition\n')
    head, index = git(root, 'rev-parse', 'HEAD'), (root / '.git/index').read_bytes()
    assert finish(root)['state'] == 'delivered'
    assert (root / 'base.txt').read_text() == 'unstaged user work\ntask addition\n'
    assert (root / 'user.txt').read_text() == 'untracked input\ntask addition\n'
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index


def test_compatible_same_file_changes_merge_without_committing_peer(repo, monkeypatch):
    root, _, _ = repo
    original = ''.join(f'line {n}\n' for n in range(20))
    (root / 'base.txt').write_text(original)
    git(root, 'add', 'base.txt'); git(root, 'commit', '-m', 'multiline input')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text(original.replace('line 2\n', 'task line\n'))
    concurrent = original.replace('line 14\n', 'peer line\n')
    (root / 'base.txt').write_text(concurrent)
    git(root, 'add', 'base.txt')
    index, head = (root / '.git/index').read_bytes(), git(root, 'rev-parse', 'HEAD')
    assert finish(root)['state'] == 'delivered'
    assert (root / 'base.txt').read_text() == concurrent.replace('line 2\n', 'task line\n')
    assert (root / '.git/index').read_bytes() == index
    assert git(root, 'rev-parse', 'HEAD') == head


def test_conflict_preserves_whole_workspace_and_checkpoint(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task version\n')
    (work / 'new.bin').write_bytes(b'task\x00binary')
    (root / 'base.txt').write_text('peer version\n')
    index = (root / '.git/index').read_bytes()
    result = finish(root)
    assert result['state'] == 'blocked'
    assert result['retry']['conflictPaths'] == ['base.txt']
    assert (root / 'base.txt').read_text() == 'peer version\n'
    assert not (root / 'new.bin').exists()
    assert (root / '.git/index').read_bytes() == index
    assert git(root, 'show', result['checkpointSha'] + ':base.txt') == 'task version'


def test_two_tasks_merge_at_completion_and_preserve_binary_delete_modes(repo, monkeypatch):
    root, _, _ = repo
    first = automatic_workspace(repo, monkeypatch, 'first')
    second = automatic_workspace(repo, monkeypatch, 'second')
    (first / 'asset.bin').write_bytes(bytes(range(256)))
    (first / 'base.txt').unlink()
    (second / 'tool.sh').write_text('#!/bin/sh\nexit 0\n')
    (second / 'tool.sh').chmod(0o755)
    head = git(root, 'rev-parse', 'HEAD')
    assert finish(root, 'first')['state'] == 'delivered'
    assert finish(root, 'second')['state'] == 'delivered'
    assert (root / 'asset.bin').read_bytes() == bytes(range(256))
    assert not (root / 'base.txt').exists()
    assert (root / 'tool.sh').stat().st_mode & 0o111
    assert git(root, 'rev-parse', 'HEAD') == head


def test_unborn_workspace_delivery_requires_no_initial_commit(repo, monkeypatch):
    _, legacy, owner = repo
    root = legacy.parent / 'unborn'
    root.mkdir(); git(root, 'init', '-b', 'main')
    work = automatic_workspace((root, legacy, owner), monkeypatch)
    (work / 'first.txt').write_text('first output\n')
    assert finish(root)['state'] == 'delivered'
    assert (root / 'first.txt').read_text() == 'first output\n'
    assert subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'],
                          capture_output=True).returncode != 0
    assert not (root / '.git/index').exists()


@pytest.mark.parametrize('movement', ['commit', 'branch', 'unrelated'])
def test_history_and_unrelated_file_movement_do_not_delay_merge(repo, monkeypatch, movement):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task output\n')
    original = task_delta_delivery._merge_workspace_delta
    def merge(*args):
        target = original(*args)
        if movement == 'commit':
            git(root, 'commit', '--allow-empty', '-m', 'concurrent history')
        elif movement == 'branch':
            git(root, 'update-ref', 'refs/heads/other', git(root, 'rev-parse', 'HEAD'))
            git(root, 'symbolic-ref', 'HEAD', 'refs/heads/other')
        else:
            (root / 'peer.txt').write_text('peer output\n')
        return target
    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', merge)
    assert finish(root)['state'] == 'delivered'
    assert (root / 'task.txt').read_text() == 'task output\n'


@pytest.mark.parametrize('fault', ['intent', 'ack', 'cancel', 'file'])
def test_publication_rechecks_authority_and_recovers_lost_ack(repo, monkeypatch, fault):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('base\nappend once\n')
    original = delivery._save
    injected = []
    def save(row, receipt, **updates):
        if not injected:
            if fault == 'ack' and updates.get('state') == 'delivered':
                injected.append(True); raise RuntimeError('lost ACK')
            if updates.get('state') == 'pending' and updates.get('targetSha'):
                if fault == 'intent':
                    injected.append(True); raise RuntimeError('intent unavailable')
                result = original(row, receipt, **updates)
                if fault == 'cancel':
                    injected.append(True)
                    original(row, result, state='preserved', taskOutcome='cancelled', reason='cancelled during publication')
                elif fault == 'file':
                    injected.append(True); (root / 'base.txt').write_text('peer after intent\n')
                return result
        return original(row, receipt, **updates)
    monkeypatch.setattr(delivery, '_save', save)
    result = finish(root)
    if fault == 'ack':
        assert (root / 'base.txt').read_text() == 'base\nappend once\n'
        (root / 'unrelated.txt').write_text('later output\n')
        assert delivery.resume_pending_workspace_deliveries()
        result = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
        assert result['state'] == 'delivered'
        assert (root / 'base.txt').read_text().count('append once') == 1
    else:
        assert result['state'] != 'delivered'
        expected = 'peer after intent\n' if fault == 'file' else 'base\n'
        assert (root / 'base.txt').read_text() == expected


def test_user_commit_can_clean_workspace_after_delivery(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('completed task\n')
    assert finish(root)['state'] == 'delivered'
    assert git(root, 'status', '--porcelain') != ''
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'save completed workspace')
    assert git(root, 'status', '--porcelain') == ''
    assert (root / 'base.txt').read_text() == 'completed task\n'


def test_workspace_delivery_resource_measurement(repo, monkeypatch):
    import json
    import statistics
    import time
    root, _, _ = repo
    durations = []
    for number in range(10):
        task = f'measured-{number}'
        work = automatic_workspace(repo, monkeypatch, task)
        (work / f'{task}.txt').write_text('owned output\n')
        before = len(list(Path('/proc/self/fd').iterdir()))
        started = time.perf_counter()
        result = finish(root, task)
        durations.append((time.perf_counter() - started) * 1000)
        assert result['state'] == 'delivered', result
        assert len(list(Path('/proc/self/fd').iterdir())) == before
        assert not work.exists()
    print('workspace-delivery-measurement=' + json.dumps({
        'samples': len(durations), 'medianMs': round(statistics.median(durations), 3),
        'maxMs': round(max(durations), 3), 'fdDelta': 0, 'retainedClones': 0}))


def test_legacy_committed_lost_ack_keeps_ancestry_recovery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('historical delivered output\n')
    result = finish(root)
    git(root, 'add', 'base.txt'); git(root, 'commit', '-m', 'historical committed delivery')
    target = git(root, 'rev-parse', 'HEAD')
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, result, state='pending', phase='commit_adoption',
                   targetSha=target, publicationMode='committed')
    git(root, 'commit', '--allow-empty', '-m', 'later history')
    (root / 'base.txt').write_text('later user edits\n')
    assert delivery.resume_pending_workspace_deliveries()
    result = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    assert result['state'] == 'delivered'
    assert result['publicationMode'] == 'committed'
    assert (root / 'base.txt').read_text() == 'later user edits\n'
