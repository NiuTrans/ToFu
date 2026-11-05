"""Changed HEAD establishes pending code without materializing a text patch."""

import subprocess

import pytest

from lib import boot_identity
from lib.self_update import _git


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('dirty', [False, True])
def test_changed_head_only_needs_the_current_dirty_verdict(monkeypatch, dirty):
    monkeypatch.setattr(boot_identity, '_FINGERPRINT_CACHE', {
        'head': '1' * 12, 'dirty': True, 'digest': 'loaded-source',
    })
    monkeypatch.setattr(_git, '_head_sha', lambda: '2' * 40)

    def run(args, timeout=30):
        # This probe can answer whether tracked edits exist. Rendering a
        # patch is unnecessary once the source commit differs from boot.
        assert '--quiet' in args
        return subprocess.CompletedProcess(args, int(dirty), '', '')

    monkeypatch.setattr(_git, '_run_git', run)
    assert boot_identity.pending_code_changes() == {
        'pending': True,
        'loaded_head': '1' * 12,
        'current_head': '2' * 12,
        'dirty': dirty,
    }


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'missing'])
def test_changed_head_does_not_claim_a_clean_tree_when_probe_fails(
        monkeypatch, failure):
    monkeypatch.setattr(boot_identity, '_FINGERPRINT_CACHE', {
        'head': '1' * 12, 'dirty': False, 'digest': 'loaded-source',
    })
    monkeypatch.setattr(_git, '_head_sha', lambda: '2' * 40)

    def run(args, timeout=30):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, timeout)
        if failure == 'missing':
            raise FileNotFoundError('git')
        return subprocess.CompletedProcess(args, 128, '', 'unavailable')

    monkeypatch.setattr(_git, '_run_git', run)
    result = boot_identity.pending_code_changes()
    # HEAD still proves a newer commit exists; dirty is independently unknown.
    assert result['pending'] is True
    assert result['dirty'] is None
    assert result['current_head'] == '2' * 12


def test_same_short_head_still_compares_full_source_fingerprints(monkeypatch):
    """A short SHA match cannot establish source equality, including collisions."""
    monkeypatch.setattr(boot_identity, '_FINGERPRINT_CACHE', {
        'head': '1' * 12, 'dirty': True, 'digest': 'loaded-source',
    })
    monkeypatch.setattr(_git, '_head_sha', lambda: '1' * 12 + '2' * 28)
    monkeypatch.setattr(_git, '_run_git', lambda args, timeout=30:
                        subprocess.CompletedProcess(args, 0, '+edited', ''))
    result = boot_identity.pending_code_changes()
    assert result['pending'] is True
    assert result['dirty'] is True


def test_matching_observed_head_is_fingerprinted_without_a_second_read(monkeypatch):
    head = 'a' * 40
    commands = []

    def run(args, timeout=30):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(_git, '_run_git', run)
    monkeypatch.setattr(_git, '_head_sha', lambda: pytest.fail('HEAD was already observed'))
    monkeypatch.setattr(boot_identity, '_FINGERPRINT_CACHE',
                        boot_identity._compute_code_fingerprint(head))
    commands.clear()
    assert boot_identity.pending_code_changes(current_head=head) == {
        'pending': False,
        'loaded_head': head[:12],
        'current_head': head[:12],
        'dirty': False,
    }
    assert commands == [['diff', 'HEAD']]


def test_unresolved_observed_head_is_read_again(monkeypatch):
    head = 'b' * 40
    reads = []
    monkeypatch.setattr(_git, '_run_git', lambda args, timeout=30:
                        subprocess.CompletedProcess(args, 0, '', ''))
    monkeypatch.setattr(_git, '_head_sha', lambda: reads.append(head) or head)
    monkeypatch.setattr(boot_identity, '_FINGERPRINT_CACHE', {
        'head': head[:12], 'dirty': False, 'digest': 'loaded-source',
    })
    result = boot_identity.pending_code_changes(current_head=None)
    assert reads == [head]
    assert result['current_head'] == head[:12]
