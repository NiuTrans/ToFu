"""A reviewed project delivery is committed as exactly its own delta.

Working-tree delivery leaves a task's bytes uncommitted in a checkout other
writers keep editing. In a repository that opted into automatic commits, the
delivery receiver records a working-tree delivery's baseline-to-checkpoint
delta on the current branch without writing any checkout file, so concurrent
and borrowed uncommitted edits stay exactly where they were. A reviewed
project delivery there lands as its own commit in its delivery pass
(tests/project_mod/test_delivery_integrity.py).
"""
import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import delivery, task_delta_delivery
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit

LINES = ''.join(f'line {number}\n' for number in range(1, 21))


def checkout_bytes(root):
    listed = git(root, 'ls-files', '--cached', '--others', '--exclude-standard').splitlines()
    return {path: (root / path).read_bytes() for path in listed if (root / path).is_file()}


def commit_shared_file(root):
    (root / 'shared.txt').write_text(LINES)
    git(root, 'add', 'shared.txt')
    git(root, 'commit', '-m', 'shared')


def delivered(repo, monkeypatch, edit):
    """Deliver one automatic task as working-tree bytes and return its row."""
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    edit(work)
    receipt = delivery.settle_task_workspace(str(root), 'automatic', user_id=owner,
                                             outcome='completed')
    assert receipt['state'] == 'delivered', receipt
    assert receipt['publicationMode'] == 'working_tree'
    return state.get_workspace(str(root), 'automatic', user_id=owner)


def test_commit_records_only_the_task_delta_and_writes_no_checkout_file(repo, monkeypatch):
    root, _work, _owner = repo
    commit_shared_file(root)
    # Another writer's uncommitted work, which the task borrows as its input.
    (root / 'shared.txt').write_text(LINES.replace('line 2\n', 'line 2 (writer)\n'))
    (root / 'staged.txt').write_text('staged by a writer\n')
    git(root, 'add', 'staged.txt')
    (root / 'scratch.txt').write_text('untracked writer notes\n')
    head = git(root, 'rev-parse', 'HEAD')

    def edit(work):
        text = (work / 'shared.txt').read_text()
        (work / 'shared.txt').write_text(text.replace('line 15\n', 'line 15 (task)\n'))
        (work / 'feature.txt').write_text('task feature\n')

    row = delivered(repo, monkeypatch, edit)
    before = checkout_bytes(root)

    ack = task_delta_delivery.commit_delivered_task_delta(row)

    assert ack['status'] == 'committed' and ack['parentSha'] == head, ack
    assert git(root, 'rev-parse', 'HEAD') == ack['commitSha']
    assert git(root, 'rev-parse', 'HEAD^') == head
    assert checkout_bytes(root) == before
    assert git(root, 'show', 'HEAD:shared.txt') == LINES.replace(
        'line 15\n', 'line 15 (task)\n').strip()
    assert git(root, 'show', 'HEAD:feature.txt') == 'task feature'
    assert git(root, 'diff', '--cached', '--name-only') == 'staged.txt'
    remaining = git(root, 'diff', 'HEAD', '--', 'shared.txt')
    assert 'line 2 (writer)' in remaining and 'line 15' not in remaining
    # git() strips its output: 'M shared.txt' is the unstaged ' M' entry.
    assert sorted(line.strip() for line in git(root, 'status', '--porcelain').splitlines()) == [
        '?? scratch.txt', 'A  staged.txt', 'M shared.txt']


def test_delta_entangled_with_borrowed_bytes_keeps_the_branch_and_checkout(repo, monkeypatch):
    root, _work, _owner = repo
    commit_shared_file(root)
    (root / 'shared.txt').write_text(LINES.replace('line 2\n', 'line 2 (writer)\n'))
    head = git(root, 'rev-parse', 'HEAD')

    def edit(work):
        text = (work / 'shared.txt').read_text()
        (work / 'shared.txt').write_text(text.replace(
            'line 2 (writer)\nline 3\n', 'line 2 (writer)\nline 3 (builds on the writer)\n'))

    row = delivered(repo, monkeypatch, edit)
    before = checkout_bytes(root)

    ack = task_delta_delivery.commit_delivered_task_delta(row)

    assert ack['status'] == 'entangled', ack
    assert ack['conflictPaths'] == ['shared.txt'] and ack['conflictPathCount'] == 1
    assert git(root, 'rev-parse', 'HEAD') == head
    assert checkout_bytes(root) == before


def test_busy_index_keeps_the_commit_due_and_is_never_unlocked_by_us(repo, monkeypatch):
    root, _work, _owner = repo
    row = delivered(repo, monkeypatch, lambda work: (work / 'feature.txt').write_text('task\n'))
    head = git(root, 'rev-parse', 'HEAD')
    lock = root / '.git' / 'index.lock'
    lock.write_text('')

    assert task_delta_delivery.commit_delivered_task_delta(row) is None
    assert git(root, 'rev-parse', 'HEAD') == head
    assert lock.exists()

    lock.unlink()
    ack = task_delta_delivery.commit_delivered_task_delta(row)
    assert ack['status'] == 'committed', ack
    assert git(root, 'status', '--porcelain') == ''


def test_moved_branch_keeps_the_commit_due_and_staged_work_survives(repo, monkeypatch):
    root, _work, _owner = repo
    row = delivered(repo, monkeypatch, lambda work: (work / 'feature.txt').write_text('task\n'))
    original = task_delta_delivery._merge_workspace_delta

    def merge_while_another_branch_lands(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / 'other.txt').write_text('landed meanwhile\n')
        git(root, 'add', 'other.txt')
        git(root, 'commit', '-m', 'landed meanwhile')
        return result

    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta',
                        merge_while_another_branch_lands)
    assert task_delta_delivery.commit_delivered_task_delta(row) is None
    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', original)
    moved = git(root, 'rev-parse', 'HEAD')

    # A writer's staged entry on a delta path is preserved, never a reason to wait.
    (root / 'feature.txt').write_text('a writer staged this\n')
    git(root, 'add', 'feature.txt')
    ack = task_delta_delivery.commit_delivered_task_delta(row)
    assert ack['status'] == 'committed' and ack['parentSha'] == moved, ack
    assert ack['indexSynchronized'] is False
    assert git(root, 'show', 'HEAD:feature.txt') == 'task'
    assert git(root, 'show', ':feature.txt') == 'a writer staged this'
    assert (root / 'feature.txt').read_text() == 'a writer staged this\n'
    assert git(root, 'status', '--porcelain') == 'M  feature.txt'


def test_contained_checkpoint_is_acknowledged_without_a_new_commit(repo, monkeypatch):
    root, _work, _owner = repo
    row = delivered(repo, monkeypatch, lambda work: (work / 'feature.txt').write_text('task\n'))
    git(root, 'update-ref', 'refs/heads/main', row['checkpoint_sha'])

    ack = task_delta_delivery.commit_delivered_task_delta(row)

    assert ack == {'status': 'committed', 'branchRef': 'refs/heads/main',
                   'commitSha': row['checkpoint_sha'], 'contained': True}
    assert git(root, 'rev-parse', 'HEAD') == row['checkpoint_sha']


@pytest.mark.parametrize('opted_in', [True, False])
def test_reviewed_project_delivery_commits_only_with_the_automatic_commit_opt_in(
        repo, monkeypatch, opted_in):
    root, _work, owner = repo
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true' if opted_in else 'false')
    # The Coordinator's review and publication are validated by their owner;
    # this specification starts from an authorized publication.
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    work = automatic_workspace(repo, monkeypatch)
    state.set_workspace_meta(project_root=str(root), task_id='automatic', user_id=owner,
                             now=ic._now(), patch={KEY: {'assignmentId': 'assignment-1'}})
    (work / 'feature.txt').write_text('reviewed work\n')
    head = git(root, 'rev-parse', 'HEAD')

    receipt = delivery.settle_task_workspace(str(root), 'automatic', user_id=owner,
                                             outcome='completed')
    assert receipt['state'] == 'delivered', receipt
    delivery.resume_pending_workspace_deliveries()

    origin = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']
    assert delivery.canonical_delivery(origin)
    if opted_in:
        # The delivery pass lands the commit itself; its receipt is the evidence.
        assert receipt['publicationMode'] == 'committed', receipt
        assert receipt['targetSha'] == git(root, 'rev-parse', 'HEAD')
        assert 'deliveryCheckpoint' not in origin
        assert git(root, 'rev-parse', 'HEAD^') == head
        assert git(root, 'show', 'HEAD:feature.txt') == 'reviewed work'
        assert git(root, 'status', '--porcelain') == ''
        message = git(root, 'log', '-1', '--format=%B')
        assert 'Tofu-Task: automatic' in message
        assert 'Tofu-Assignment: assignment-1' in message
    else:
        assert receipt['publicationMode'] == 'working_tree', receipt
        assert origin['deliveryCheckpoint'] == {'status': 'disabled'}
        assert git(root, 'rev-parse', 'HEAD') == head
        assert (root / 'feature.txt').read_text() == 'reviewed work\n'
    commit = git(root, 'rev-parse', 'HEAD')
    assert not delivery.resume_pending_workspace_deliveries()
    assert git(root, 'rev-parse', 'HEAD') == commit
