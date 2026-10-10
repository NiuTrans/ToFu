"""Immutable delivery validation must not retain canonical/source writer locks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from lib.project_mod.workspace_lease import workspace_write_lease
from tests.storage.test_integration_delivery import repo as _repo_fixture, git, settle, automatic_workspace

repo = _repo_fixture  # Reuse the existing isolated SQLite/Git fixture authority.
pytestmark = pytest.mark.unit


def _row(root, task='task'):
    return state.get_workspace(str(root), task, user_id=1)


def _adopt(root):
    return delivery.deliver_registered_workspace(str(root), 'task', user_id=1)


def _pause(monkeypatch, name):
    entered, release = threading.Event(), threading.Event()
    original = getattr(ic, name)

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(15), 'test failed to release validation'
        return original(*args, **kwargs)

    monkeypatch.setattr(ic, name, paused)
    return entered, release


@pytest.mark.parametrize('validation', ['_merge_checkpoint', '_gate_commands'])
def test_canonical_writer_and_repo_admission_progress_during_validation(repo, monkeypatch, validation):
    root, work, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    entered, release = _pause(monkeypatch, validation)
    before = git(root, 'rev-parse', 'HEAD')
    with ThreadPoolExecutor(max_workers=2) as pool:
        adoption = pool.submit(_adopt, root)
        try:
            assert entered.wait(15)

            def writer():
                with workspace_write_lease(str(root), timeout=0.0), ic._repo_lock(str(root)):
                    (root / 'user.txt').write_text('concurrent writer\n')
                    git(root, 'add', 'user.txt')
                    git(root, 'commit', '-m', 'user writer')

            pool.submit(writer).result(timeout=5)
        finally:
            release.set()
        result = adoption.result(timeout=20)
    assert result['reason'] == 'canonical_changed_during_validation'
    assert not result.get('targetSha')
    assert git(root, 'rev-parse', 'HEAD') != before
    assert not (root / 'feature.txt').exists()
    assert (root / 'user.txt').read_text() == 'concurrent writer\n'


@pytest.mark.parametrize('race', ['dirty', 'branch', 'presence', 'cancel'])
def test_unlocked_gate_revalidates_all_publication_authority(repo, monkeypatch, race):
    root, work, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    entered, release = _pause(monkeypatch, '_gate_commands')
    before = git(root, 'rev-parse', 'HEAD')
    with ThreadPoolExecutor(max_workers=2) as pool:
        adoption = pool.submit(_adopt, root)
        try:
            assert entered.wait(15)

            def change():
                with workspace_write_lease(str(root), timeout=0.0):
                    if race == 'dirty':
                        (root / 'base.txt').write_text('user owns this\n')
                    elif race == 'branch':
                        git(root, 'update-ref', 'refs/heads/other', before)
                        git(root, 'symbolic-ref', 'HEAD', 'refs/heads/other')
                    elif race == 'presence':
                        monkeypatch.setattr(delivery, '_canonical_in_use', lambda *a: True)
                    else:
                        delivery.settle_task_workspace(str(work), 'task', user_id=owner, outcome='cancelled')

            pool.submit(change).result(timeout=5)
        finally:
            release.set()
        result = adoption.result(timeout=20)
    assert result['state'] != 'delivered'
    assert not result.get('targetSha')
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not (root / 'feature.txt').exists()
    if race == 'cancel':
        assert result['taskOutcome'] == 'cancelled'


@pytest.mark.parametrize('fault', ['gate', 'intent', 'receipt'])
def test_faults_keep_prepublication_safe_and_postpublication_recoverable(repo, monkeypatch, fault):
    root, work, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    before = git(root, 'rev-parse', 'HEAD')
    original = delivery._save
    if fault == 'gate':
        monkeypatch.setattr(ic, '_gate_commands', lambda *a, **k: (False, 'injected gate failure'))
        assert _adopt(root)['state'] == 'failed'
    else:
        def fail(row, receipt, **updates):
            if ((fault == 'intent' and updates.get('targetSha') and updates.get('state') == 'pending')
                    or (fault == 'receipt' and updates.get('state') == 'delivered')):
                raise RuntimeError('injected storage fault')
            return original(row, receipt, **updates)
        monkeypatch.setattr(delivery, '_save', fail)
        with pytest.raises(RuntimeError, match='injected storage fault'):
            _adopt(root)
    if fault == 'receipt':
        monkeypatch.setattr(delivery, '_save', original)
        head = git(root, 'rev-parse', 'HEAD')
        assert head != before
        assert _adopt(root)['state'] == 'delivered'
        assert git(root, 'rev-parse', 'HEAD') == head
    else:
        assert git(root, 'rev-parse', 'HEAD') == before
        assert not (root / 'feature.txt').exists()


@pytest.mark.parametrize('race', ['edit', 'checkpoint', 'discard', 'cancel'])
def test_automatic_source_lease_released_but_checkpoint_is_fenced(repo, monkeypatch, race):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured task delta\n')
    delivery.settle_task_workspace(str(work), 'automatic', user_id=owner, outcome='completed')
    row = _row(root, 'automatic')
    entered, release = _pause(monkeypatch, '_gate_commands')
    before = git(root, 'rev-parse', 'HEAD')
    with ThreadPoolExecutor(max_workers=2) as pool:
        adoption = pool.submit(delivery._settle_registered, row, delivery._receipt(row))
        try:
            assert entered.wait(15)

            def source_writer():
                with workspace_write_lease(str(work), timeout=0.0):
                    (work / 'later.txt').write_text('not in captured checkpoint\n')
                    if race == 'checkpoint':
                        ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
                    elif race == 'discard':
                        ic.discard_workspace(str(root), 'automatic', user_id=owner)
                    elif race == 'cancel':
                        delivery.settle_task_workspace(str(work), 'automatic', user_id=owner, outcome='cancelled')

            pool.submit(source_writer).result(timeout=5)
        finally:
            release.set()
        result = adoption.result(timeout=20)
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not (root / 'later.txt').exists()
    if race == 'edit':
        assert result['state'] == 'delivered'
        assert (root / 'task.txt').read_text() == 'captured task delta\n'
        assert (work / 'later.txt').read_text() == 'not in captured checkpoint\n'
    else:
        assert result['state'] != 'delivered'
        assert not (root / 'task.txt').exists()
        assert work.exists()
        if race == 'cancel':
            assert result['taskOutcome'] == 'cancelled'


def test_source_busy_cannot_reopen_delivered_receipt(repo):
    root, work, owner = repo
    settle(repo)
    captured = _row(root)
    assert ic.process_ready_once()
    assert _adopt(root)['state'] == 'delivered'
    entered, release = threading.Event(), threading.Event()

    def hold():
        with workspace_write_lease(work):
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        assert entered.wait(5)
        try:
            result = delivery._settle_registered(captured, delivery._receipt(captured))
            assert result['state'] == 'delivered'
            assert delivery._receipt(_row(root))['state'] == 'delivered'
        finally:
            release.set()
        holder.result(timeout=5)


def test_discard_entrypoint_waits_for_inflight_publication_fence(repo, monkeypatch):
    from contextlib import contextmanager
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('linearized publication\n')
    delivery.settle_task_workspace(str(work), 'automatic', user_id=owner, outcome='completed')
    row = _row(root, 'automatic')
    at_intent, release, discard_requested, discarded = (threading.Event() for _ in range(4))
    save = delivery._save
    repo_lock = ic._repo_lock

    def pause(row, receipt, **updates):
        if updates.get('state') == 'pending' and updates.get('targetSha'):
            at_intent.set()
            assert release.wait(15)
        return save(row, receipt, **updates)

    @contextmanager
    def observe_lock(root):
        if threading.current_thread().name.startswith('discard-test'):
            discard_requested.set()
        with repo_lock(root):
            yield

    monkeypatch.setattr(delivery, '_save', pause)
    monkeypatch.setattr(ic, '_repo_lock', observe_lock)

    def discard():
        ic.discard_workspace(str(root), 'automatic', user_id=owner)
        discarded.set()

    with ThreadPoolExecutor(max_workers=1) as publish_pool, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='discard-test') as discard_pool:
        publication = publish_pool.submit(delivery._settle_registered, row, delivery._receipt(row))
        try:
            assert at_intent.wait(15)
            revocation = discard_pool.submit(discard)
            assert discard_requested.wait(5)
            assert not discarded.is_set(), 'discard bypassed actual publication admission'
            assert _row(root, 'automatic')['state'] != 'discarded'
        finally:
            release.set()
        assert publication.result(timeout=20)['state'] == 'delivered'
        revocation.result(timeout=10)
    assert discarded.is_set()
    assert (root / 'task.txt').read_text() == 'linearized publication\n'


def test_cleanup_scan_does_not_hold_canonical_repo_admission(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    delivery.settle_task_workspace(str(work), 'automatic', user_id=owner, outcome='completed')
    row = _row(root, 'automatic')
    entered, release = threading.Event(), threading.Event()
    git_call = ic._git

    def pause(where, arguments, **kwargs):
        if (Path(where) == work and arguments[:1] == ['ls-files']
                and delivery._receipt(_row(root, 'automatic')).get('state') == 'delivered'):
            entered.set()
            assert release.wait(15)
        return git_call(where, arguments, **kwargs)

    monkeypatch.setattr(ic, '_git', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(delivery._settle_registered, row, delivery._receipt(row))
        try:
            assert entered.wait(15)

            def probe():
                with ic._repo_lock(str(root)):
                    return 'entered'

            assert pool.submit(probe).result(timeout=5) == 'entered'
        finally:
            release.set()
        assert completion.result(timeout=20)['state'] == 'delivered'
