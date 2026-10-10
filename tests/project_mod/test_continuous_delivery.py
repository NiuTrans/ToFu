"""Continuous delivery preserves concurrent bytes and retries only changed inputs."""
from pathlib import Path
import threading
import json

import jsonschema
import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from lib.project_mod.delivery_retry import conflicted_paths
from lib.project_mod.workspace_lease import workspace_write_lease
from tests.storage.test_integration_delivery import (
    repo as repo_fixture, git, automatic_workspace, automatic_settle,
)

repo = repo_fixture
pytestmark = pytest.mark.unit


def receipt(root):
    return state.get_workspace(str(root), 'automatic', user_id=1)['origin']['delivery']


def setup_conflict(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task version\n')
    (work / 'new.txt').write_text('publish together\n')
    (root / 'base.txt').write_text('canonical version\n')
    return root, work


def spy_merges(monkeypatch):
    calls = []
    original = ic._git

    def spy(root, args, **kwargs):
        if args[:2] == ['apply', '--3way']:
            calls.append(tuple(args))
        return original(root, args, **kwargs)

    monkeypatch.setattr(ic, '_git', spy)
    return calls


def test_same_file_compatible_changes_three_way_merge_preserves_staged_index(repo, monkeypatch):
    root, _, _ = repo
    original = ''.join(f'line {n}\n' for n in range(20))
    (root / 'base.txt').write_text(original)
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'multiline baseline')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text(original.replace('line 2\n', 'task line\n'))
    concurrent = original.replace('line 4\n', 'concurrent line\n')
    (root / 'base.txt').write_text(concurrent)
    git(root, 'add', 'base.txt')
    before_index = git(root, 'write-tree')
    before_head = git(root, 'rev-parse', 'HEAD')
    assert automatic_settle(repo)['state'] == 'delivered'
    assert (root / 'base.txt').read_text() == concurrent.replace('line 2\n', 'task line\n')
    assert git(root, 'write-tree') == before_index
    assert git(root, 'rev-parse', 'HEAD') == before_head


def test_conflict_evidence_is_durable_bounded_and_no_partial_publication(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    result = automatic_settle(repo)
    assert result['state'] == 'blocked'
    evidence = result['retry']
    schema = json.loads((Path(__file__).resolve().parents[2] /
                                     'contracts/workspace_delivery_retry_v1.schema.json').read_text())
    jsonschema.validate(evidence, schema)
    assert evidence['conflictPaths'] == ['base.txt']
    assert evidence['conflictPathCount'] == 1
    assert git(root, 'show', evidence['baselineSha'] + ':base.txt') == 'base'
    assert git(root, 'show', evidence['checkpointSha'] + ':base.txt') == 'task version'
    assert git(root, 'show', evidence['sourceSha'] + ':base.txt') == 'canonical version'
    assert git(root, 'rev-parse', 'refs/tofu/delivery-inputs/u1/' + ic._safe_task('automatic')) == evidence['sourceSha']
    assert not (root / 'new.txt').exists()
    assert (work / 'new.txt').exists()


def test_restart_and_unrelated_edits_do_not_repeat_conflict_merge(repo, monkeypatch):
    root, _ = setup_conflict(repo, monkeypatch)
    calls = spy_merges(monkeypatch)
    first = automatic_settle(repo)
    assert len(calls) == 1
    delivery._RECOVERY_CURSOR = 0  # Simulate a fresh worker, with no memory cache.
    assert not delivery.resume_pending_workspace_deliveries()
    (root / 'unrelated.txt').write_text('other task\n')
    assert not delivery.resume_pending_workspace_deliveries()
    assert len(calls) == 1
    assert receipt(root) == first
    # A relevant change retries immediately and publishes the entire delta.
    (root / 'base.txt').write_text('base\n')
    assert delivery.resume_pending_workspace_deliveries()
    assert len(calls) == 2
    assert receipt(root)['state'] == 'delivered'
    assert receipt(root)['retry'] is None
    assert (root / 'new.txt').read_text() == 'publish together\n'
    assert (root / 'unrelated.txt').read_text() == 'other task\n'


def test_gate_failure_waits_for_combined_tree_or_verification_change(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('candidate\n')
    calls = []

    def gate(*args, **kwargs):
        calls.append(args[2])
        return len(calls) > 1, 'injected semantic failure'

    monkeypatch.setattr(ic, '_gate_commands', gate)
    first = automatic_settle(repo)
    assert first['state'] == 'failed'
    assert first['retry']['condition'] == 'validation_inputs_changed'
    assert not delivery.resume_pending_workspace_deliveries()
    assert len(calls) == 1
    assert not (root / 'new.txt').exists()
    # Gate configuration is itself an input; no checkpoint mutation required.
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'python -m unittest')
    assert delivery.resume_pending_workspace_deliveries()
    assert len(calls) == 2
    assert receipt(root)['state'] == 'delivered'


def test_failed_owner_does_not_get_publication_by_retry_evidence(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    automatic_settle(repo)
    delivery.settle_task_workspace(str(root), 'automatic', user_id=1, outcome='cancelled')
    (root / 'base.txt').write_text('base\n')
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'preserved'
    assert not (root / 'new.txt').exists()


def test_conflict_path_parser_preserves_literal_tabs_and_newlines():
    assert conflicted_paths('100644 abc 1\tfile\tname\n.txt\0'
                            '100644 def 2\tfile\tname\n.txt\0') == ['file\tname\n.txt']


class AliveWorker:
    def is_alive(self):
        return True


@pytest.mark.parametrize('armed,stopping,self_worker,expected', [
    (True, False, False, True), (False, False, False, False),
    (True, True, False, False), (True, False, True, False),
])
def test_release_hint_only_wakes_existing_authorized_worker(monkeypatch, armed, stopping,
                                                           self_worker, expected):
    worker = threading.current_thread() if self_worker else AliveWorker()
    monkeypatch.setattr(ic, '_WORKER', worker)
    monkeypatch.setattr(ic, '_WORKER_AUTHORITY_ARMED', armed)
    monkeypatch.setattr(ic, '_WORKER_STOP_REQUESTED', stopping)
    before = ic._worker_generation()
    assert ic.notify_workspace_write_released() is expected
    assert ic._worker_generation() == before + int(expected)


def test_release_wake_runs_after_exclusion_and_registry_release(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    observed = []

    def wake():
        # A competing thread can acquire; callback never runs under admission.
        def writer():
            with workspace_write_lease(root, timeout=0):
                observed.append('acquired')
        thread = threading.Thread(target=writer)
        # Avoid observing the probe writer itself recursively.
        monkeypatch.setattr(ic, 'notify_workspace_write_released', lambda: False)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

    monkeypatch.setattr(ic, 'notify_workspace_write_released', wake)
    with workspace_write_lease(root):
        pass
    assert observed == ['acquired']


def test_explicit_retry_routes_to_delivery_and_repaired_checkpoint_not_candidate(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    assert automatic_settle(repo)['state'] == 'blocked'
    # Agent/human repair remains in the registered source; the canonical
    # change is preserved in the proposed resolution before it can publish.
    (work / 'base.txt').write_text('canonical version\nresolved task addition\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    result = ic.retry_workspace(str(root), 'automatic', user_id=1)
    assert result['state'] == 'pending'
    assert not ic.process_ready_once()
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'base.txt').read_text() == 'canonical version\nresolved task addition\n'
    assert (root / 'new.txt').read_text() == 'publish together\n'
    with pytest.raises(ic.IntegrationError, match='unpublished completed'):
        ic.retry_workspace(str(root), 'automatic', user_id=1)


def test_retry_cannot_upgrade_revoked_outcome_or_cross_owner(repo, monkeypatch):
    root, _ = setup_conflict(repo, monkeypatch)
    automatic_settle(repo)
    with pytest.raises(Exception):
        ic.retry_workspace(str(root), 'automatic', user_id=2)
    delivery.settle_task_workspace(str(root), 'automatic', user_id=1, outcome='cancelled')
    with pytest.raises(ic.IntegrationError, match='unpublished completed'):
        ic.retry_workspace(str(root), 'automatic', user_id=1)


def test_unchanged_poll_reuses_pinned_commit_without_git_object_growth(repo, monkeypatch):
    root, _ = setup_conflict(repo, monkeypatch)
    result = automatic_settle(repo)
    before = git(root, 'count-objects', '-v')
    # Force a later Git author/committer timestamp to catch timestamp-only
    # snapshot churn even when a fast test finishes in the original second.
    monkeypatch.setenv('GIT_AUTHOR_DATE', '2030-01-01T00:00:00Z')
    monkeypatch.setenv('GIT_COMMITTER_DATE', '2030-01-01T00:00:00Z')
    for _ in range(3):
        assert not delivery.resume_pending_workspace_deliveries()
    assert receipt(root) == result
    assert git(root, 'count-objects', '-v') == before


def test_repair_preserves_unrelated_canonical_wip_and_original_source(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    (root / 'unrelated.txt').write_text('user wip\n')
    git(root, 'add', 'unrelated.txt')
    index = git(root, 'write-tree')
    automatic_settle(repo)
    (work / 'base.txt').write_text('canonical version\nresolved task addition\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    ic.retry_workspace(str(root), 'automatic', user_id=1)
    row = state.get_workspace(str(root), 'automatic', user_id=1)
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         'contracts/workspace_delivery_repair_v1.schema.json').read_text())
    jsonschema.validate(row['origin']['delivery']['repair'], schema)
    assert git(root, 'show', row['origin']['delivery']['repair']['checkpointSha'] +
               ':unrelated.txt') == 'user wip'
    assert not (work / 'unrelated.txt').exists()
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'unrelated.txt').read_text() == 'user wip\n'
    assert git(root, 'write-tree') == index


def test_repair_cannot_expand_original_delta_paths(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    first = automatic_settle(repo)
    (work / 'other.txt').write_text('outside repair scope\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    with pytest.raises(ic.IntegrationError, match='outside the original task delta'):
        ic.retry_workspace(str(root), 'automatic', user_id=1)
    assert receipt(root) == first
    assert not (root / 'other.txt').exists()


def test_repair_preparation_does_not_hold_repository_or_writer_admission(repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    root, work = setup_conflict(repo, monkeypatch)
    automatic_settle(repo)
    (work / 'base.txt').write_text('canonical version\nresolved task addition\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    original = ic._alternate_index_checkpoint
    entered, release = threading.Event(), threading.Event()

    def pause(checkout, *args, **kwargs):
        if Path(checkout).name == 'proposal':
            entered.set()
            assert release.wait(15)
        return original(checkout, *args, **kwargs)

    monkeypatch.setattr(ic, '_alternate_index_checkpoint', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retry = pool.submit(ic.retry_workspace, str(root), 'automatic', user_id=1)
        try:
            assert entered.wait(15)
            def writer():
                with workspace_write_lease(root, timeout=0), ic._repo_lock(str(root)):
                    (root / 'other.txt').write_text('concurrent admission\n')
            pool.submit(writer).result(timeout=5)
        finally:
            release.set()
        assert retry.result(timeout=20)['state'] == 'pending'
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'other.txt').read_text() == 'concurrent admission\n'


def test_repair_preparation_rechecks_revocation_before_admission(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    automatic_settle(repo)
    (work / 'base.txt').write_text('canonical version\nresolved task addition\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    original = ic._alternate_index_checkpoint

    def revoke(checkout, *args, **kwargs):
        result = original(checkout, *args, **kwargs)
        if Path(checkout).name == 'proposal':
            delivery.settle_task_workspace(str(root), 'automatic', user_id=1, outcome='cancelled')
        return result

    monkeypatch.setattr(ic, '_alternate_index_checkpoint', revoke)
    with pytest.raises(ic.IntegrationError, match='Repair authority changed'):
        ic.retry_workspace(str(root), 'automatic', user_id=1)
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'preserved'
    assert not (root / 'new.txt').exists()


def test_repair_pin_rolls_back_on_durable_receipt_failure(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    automatic_settle(repo)
    (work / 'base.txt').write_text('canonical version\nresolved task addition\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    first = ic.retry_workspace(str(root), 'automatic', user_id=1)
    ref = 'refs/tofu/delivery-repairs/u1/' + ic._safe_task('automatic')
    previous = git(root, 'rev-parse', ref)
    (work / 'base.txt').write_text('canonical version\nupdated resolution\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    original = delivery._save

    def fail(row, value, **updates):
        if updates.get('state') == 'pending':
            raise RuntimeError('injected durable admission failure')
        return original(row, value, **updates)

    monkeypatch.setattr(delivery, '_save', fail)
    with pytest.raises(RuntimeError, match='durable admission failure'):
        ic.retry_workspace(str(root), 'automatic', user_id=1)
    assert git(root, 'rev-parse', ref) == previous
    assert receipt(root) == first['delivery']


def test_lost_repair_publication_receipt_recovers_without_reapplying(repo, monkeypatch):
    root, work = setup_conflict(repo, monkeypatch)
    automatic_settle(repo)
    (work / 'base.txt').write_text('canonical version\nappend once\n')
    ic.checkpoint_workspace(str(root), 'automatic', user_id=1)
    ic.retry_workspace(str(root), 'automatic', user_id=1)
    original = delivery._save
    lost = []

    def fail(row, value, **updates):
        if updates.get('state') == 'delivered' and not lost:
            lost.append(True)
            raise RuntimeError('lost repair publication acknowledgement')
        return original(row, value, **updates)

    monkeypatch.setattr(delivery, '_save', fail)
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'blocked'
    (root / 'unrelated.txt').write_text('after publication\n')
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'base.txt').read_text() == 'canonical version\nappend once\n'
    assert (root / 'unrelated.txt').read_text() == 'after publication\n'


def test_conflict_path_evidence_is_bounded_by_encoded_bytes_and_reports_truncation():
    from lib.project_mod.delivery_retry import retry_evidence, MAX_CONFLICT_PATH_BYTES
    paths = [f'{n}-' + '界' * 100 for n in range(100)]
    evidence = retry_evidence('conflict_inputs_changed', 'a' * 64,
                              'b' * 40, 'c' * 40, 'd' * 40, paths)
    assert evidence['conflictPathCount'] == 100
    assert evidence['conflictPathsTruncated'] is True
    assert len(json.dumps(evidence['conflictPaths'], ensure_ascii=False).encode()) <= MAX_CONFLICT_PATH_BYTES
    assert set(evidence['conflictPaths']) <= set(paths)


def test_git_pathspec_shaped_names_are_literal_retry_inputs(repo, monkeypatch):
    root, _, _ = repo
    name = ':(exclude)literal.txt'
    (root / name).write_text('baseline\n')
    git(root, '--literal-pathspecs', 'add', '--', name)
    git(root, 'commit', '-m', 'literal filename baseline')
    work = automatic_workspace(repo, monkeypatch)
    (work / name).write_text('task version\n')
    (root / name).write_text('canonical version\n')
    assert automatic_settle(repo)['state'] == 'blocked'
    assert not delivery.resume_pending_workspace_deliveries()
    (root / name).write_text('baseline\n')
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / name).read_text() == 'task version\n'


def test_gate_timeout_retains_transient_retry_without_input_change(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('candidate\n')
    calls = []

    def gate(*args, **kwargs):
        calls.append(1)
        return (False, 'integration_gate_timeout: temporary test deadline') if len(calls) == 1 else (True, '')

    monkeypatch.setattr(ic, '_gate_commands', gate)
    first = automatic_settle(repo)
    assert first['state'] == 'blocked'
    assert first['retry'] is None
    delivery.resume_pending_workspace_deliveries()
    assert len(calls) == 2
    assert receipt(root)['state'] == 'delivered'


def test_failed_optional_repair_narrative_does_not_lose_durable_blocker(repo, monkeypatch):
    root, _ = setup_conflict(repo, monkeypatch)
    from lib.conversations import project_brain

    def fail(*args, **kwargs):
        raise RuntimeError('optional projection unavailable')

    monkeypatch.setattr(project_brain, 'record_integration_failure', fail)
    first = automatic_settle(repo)
    assert first['state'] == 'blocked'
    assert first['retry']['conflictPaths'] == ['base.txt']
    assert receipt(root) == first


def test_real_gate_deadline_is_transient_integration_failure(repo, monkeypatch, tmp_path):
    import shlex
    import sys
    root, _, _ = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'gates'))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_TIMEOUT', '0.05')
    command = shlex.join([sys.executable, '-c', 'import time; time.sleep(1)'])
    passed, detail = ic._gate_commands(root, 'HEAD', 'HEAD', command)
    assert not passed
    assert detail.startswith('integration_gate_timeout: ')
    assert git(root, 'worktree', 'list', '--porcelain').count('worktree ') == 2
