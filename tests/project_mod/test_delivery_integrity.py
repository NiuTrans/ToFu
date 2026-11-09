"""A project delivery is canonical only once its own commit is on the branch.

Recorded on 2026-11-07: reviewed deliveries were reported delivered while their
bytes sat uncommitted in the shared checkout, because their deltas built on
borrowed uncommitted edits and the commit step acknowledged them ``entangled``
once and never retried; a no-change delivery recorded a commit of the live
checkout (other writers' dirty files) as its target; and ``canonicalDelivery``
read only the receipt state. In a repository that opted into automatic
commits, a reviewed project delivery now lands as exactly its own commit, with
other writers' uncommitted edits carried forward, or is held with a visible
reason until it can; a no-change delivery names HEAD; and canonical delivery
requires commit evidence the receipt producer verified.
"""
import time

import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import checkout_landing, delivery, task_delta_delivery
from tests.project_mod.test_coordinator_workers import (
    base_runtime as base_runtime, context as context, runtime as runtime)
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit

LINES = ''.join(f'line {number}\n' for number in range(1, 21))


def commit_file(root, path, text, message):
    (root / path).write_text(text)
    git(root, 'add', path)
    git(root, 'commit', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


def checkout_bytes(root):
    listed = git(root, 'ls-files', '--cached', '--others', '--exclude-standard').splitlines()
    return {path: (root / path).read_bytes() for path in listed if (root / path).is_file()}


def reviewed_project_task(repo, monkeypatch, *, opted_in=True):
    """An automatic Worker checkout whose Coordinator review authorizes publication."""
    root, _work, owner = repo
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true' if opted_in else 'false')
    # Review and publication are validated by their owner (test_project_publication.py).
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    work = automatic_workspace(repo, monkeypatch)
    state.set_workspace_meta(project_root=str(root), task_id='automatic', user_id=owner,
                             now=ic._now(), patch={KEY: {'assignmentId': 'assignment-1'}})
    return work


def origin_of(repo):
    root, _work, owner = repo
    return state.get_workspace(str(root), 'automatic', user_id=owner)['origin']


def settle(repo):
    root, _work, owner = repo
    delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    return origin_of(repo)


ENTANGLED_SHAPES = {
    # The task edits the line next to another writer's uncommitted edit.
    'adjacent_lines': dict(
        path='shared.txt', committed=LINES,
        borrowed=LINES.replace('line 2\n', 'line 2 (writer)\n'),
        task=LINES.replace('line 2\n', 'line 2 (writer)\n').replace(
            'line 3\n', 'line 3 (builds on the writer)\n'),
        later=lambda text: text.replace('line 18\n', 'line 18 (writer, uncommitted)\n')),
    # The task extends a file another writer created and has not committed.
    'borrowed_new_file': dict(
        path='notes.txt', committed=None, borrowed='writer notes\n',
        task='writer notes\ntask line\n',
        later=lambda text: 'writer preface\n' + text),
}


@pytest.mark.parametrize('shape', sorted(ENTANGLED_SHAPES))
def test_entangled_delivery_is_held_then_lands_as_its_own_commit_with_wip_carried_forward(
        repo, monkeypatch, shape):
    root, _work, _owner = repo
    case = ENTANGLED_SHAPES[shape]
    path = case['path']
    if case['committed'] is not None:
        commit_file(root, path, case['committed'], 'committed history')
    # Another writer's uncommitted edit; the Worker's input snapshot borrows it.
    (root / path).write_text(case['borrowed'])
    head = git(root, 'rev-parse', 'HEAD')
    work = reviewed_project_task(repo, monkeypatch)
    assert (work / path).read_text() == case['borrowed']
    (work / path).write_text(case['task'])
    before = checkout_bytes(root)

    origin = settle(repo)

    held = origin['delivery']
    assert held['state'] == 'blocked', held
    assert held['reason'].startswith('task_delta_entangled:'), held['reason']
    assert path in held['reason'] and held['retry']['conflictPaths'] == [path]
    assert not delivery.canonical_delivery(origin)
    # Nothing was committed or written: the checkout holds only the writer's bytes.
    assert git(root, 'rev-parse', 'HEAD') == head
    assert checkout_bytes(root) == before
    # Unchanged inputs keep the hold without merging again.
    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta',
                        lambda *a, **k: pytest.fail('unchanged hold merged again'))
    delivery.resume_pending_workspace_deliveries()
    assert origin_of(repo)['delivery'] == held
    monkeypatch.undo()
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')

    # The writer commits the bytes the task built on and keeps editing the file.
    writer_commit = commit_file(root, path, case['borrowed'], 'writer commits its edit')
    (root / path).write_text(case['later'](case['borrowed']))

    delivery.resume_pending_workspace_deliveries()

    origin = origin_of(repo)
    landed = origin['delivery']
    assert landed['state'] == 'delivered', landed
    assert landed['publicationMode'] == 'committed' and landed['reason'] == ''
    assert delivery.canonical_delivery(origin)
    assert git(root, 'rev-parse', 'HEAD') == landed['targetSha'] == landed['headSha']
    assert git(root, 'rev-parse', 'HEAD^') == writer_commit
    assert git(root, 'diff', '--name-only', 'HEAD^', 'HEAD') == path
    assert (git(root, 'show', f'HEAD:{path}') + '\n') == case['task']
    message = git(root, 'log', '-1', '--format=%B')
    assert 'Tofu-Task: automatic' in message and 'Tofu-Assignment: assignment-1' in message
    # The writer's later edit is carried forward on top of the delivery, uncommitted.
    assert (root / path).read_text() == case['later'](case['task'])
    assert git(root, 'diff', '--cached', '--name-only') == ''
    assert git(root, 'status', '--porcelain') == f'M {path}'


def test_held_landing_takes_no_claims_lease_until_an_input_changes(repo, monkeypatch):
    """A held commit landing is rechecked when HEAD, the delta paths' bytes or
    their index entries change, not under its claims lease on every pass."""
    root, _work, _owner = repo
    case = ENTANGLED_SHAPES['adjacent_lines']
    path = case['path']
    commit_file(root, path, case['committed'], 'committed history')
    (root / path).write_text(case['borrowed'])
    work = reviewed_project_task(repo, monkeypatch)
    (work / path).write_text(case['task'])
    monkeypatch.setattr(task_delta_delivery, '_UNCHANGED_HOLDS', {})
    held = settle(repo)['delivery']
    assert held['state'] == 'blocked' and held['phase'] == 'commit_adoption'
    leases = []
    original = task_delta_delivery.workspace_claims_lease
    monkeypatch.setattr(task_delta_delivery, 'workspace_claims_lease',
                        lambda claims, **kwargs: leases.append(len(claims)) or original(claims, **kwargs))
    time.sleep(task_delta_delivery._TIMESTAMP_RESOLUTION_SECONDS + 1.1)
    # A fresh process: the first pass rechecks fully, then remembers the inputs.
    task_delta_delivery._UNCHANGED_HOLDS.clear()
    for _ in range(3):
        delivery.resume_pending_workspace_deliveries()
    assert len(leases) == 1 and origin_of(repo)['delivery'] == held
    # Staging the delta path changes an input of the landing, though not its bytes.
    git(root, 'add', path)
    delivery.resume_pending_workspace_deliveries()
    assert len(leases) == 2
    git(root, 'reset', '-q', '--', path)
    # The writer commits what the task built on: HEAD moves and the delivery lands.
    commit_file(root, path, case['borrowed'], 'writer commits its edit')
    delivery.resume_pending_workspace_deliveries()
    assert origin_of(repo)['delivery']['state'] == 'delivered'


@pytest.mark.parametrize('kind', ['project', 'task'])
def test_no_change_delivery_names_head_and_snapshots_nothing(repo, monkeypatch, kind):
    root, _work, _owner = repo
    # Other writers' uncommitted work: a modified, a staged and an untracked file.
    (root / 'base.txt').write_text('writer edit\n')
    (root / 'staged.txt').write_text('staged by a writer\n')
    git(root, 'add', 'staged.txt')
    (root / 'scratch.txt').write_text('untracked notes\n')
    head = git(root, 'rev-parse', 'HEAD')
    if kind == 'project':
        reviewed_project_task(repo, monkeypatch)
    else:
        automatic_workspace(repo, monkeypatch)
    before = checkout_bytes(root)
    monkeypatch.setattr(task_delta_delivery, '_canonical_tree',
                        lambda *a, **k: pytest.fail('a no-change delivery captured the checkout'))

    origin = settle(repo)

    receipt = origin['delivery']
    assert receipt['state'] == 'delivered' and receipt['phase'] == 'no_changes', receipt
    assert receipt['targetSha'] == receipt['headSha'] == head
    assert git(root, 'rev-parse', 'HEAD') == head
    assert checkout_bytes(root) == before
    assert delivery.canonical_delivery(origin)
    if kind == 'project':
        assert receipt['publicationMode'] == 'committed'


def test_delivered_commit_contains_only_the_paths_the_worker_changed(repo, monkeypatch):
    """Borrowed paths the Worker did not change never enter its commit (regression guard)."""
    root, _work, _owner = repo
    commit_file(root, 'shared.txt', LINES, 'shared')
    borrowed = {f'wip/{number}.txt': f'writer file {number}\n' for number in range(12)}
    for name, text in borrowed.items():
        (root / name).parent.mkdir(exist_ok=True)
        (root / name).write_text(text)
    (root / 'base.txt').write_text('writer edit of a tracked file\n')
    (root / 'shared.txt').write_text(LINES.replace('line 2\n', 'line 2 (writer)\n'))
    head = git(root, 'rev-parse', 'HEAD')
    work = reviewed_project_task(repo, monkeypatch)
    text = (work / 'shared.txt').read_text()
    (work / 'shared.txt').write_text(text.replace('line 15\n', 'line 15 (task)\n'))
    (work / 'feature.txt').write_text('task feature\n')

    receipt = settle(repo)['delivery']

    assert receipt['state'] == 'delivered' and receipt['publicationMode'] == 'committed', receipt
    assert git(root, 'rev-parse', 'HEAD^') == head
    assert git(root, 'diff', '--name-only', 'HEAD^', 'HEAD').splitlines() == ['feature.txt', 'shared.txt']
    assert git(root, 'show', 'HEAD:shared.txt') + '\n' == LINES.replace('line 15\n', 'line 15 (task)\n')
    # Every borrowed edit stays in the checkout, uncommitted, with the delivery merged in.
    assert (root / 'shared.txt').read_text() == LINES.replace(
        'line 2\n', 'line 2 (writer)\n').replace('line 15\n', 'line 15 (task)\n')
    assert (root / 'feature.txt').read_text() == 'task feature\n'
    assert (root / 'base.txt').read_text() == 'writer edit of a tracked file\n'
    assert all((root / name).read_text() == text for name, text in borrowed.items())
    assert sorted(line.strip() for line in git(root, 'status', '--porcelain').splitlines()) == [
        '?? wip/', 'M base.txt', 'M shared.txt']


def test_a_commit_the_branch_no_longer_reaches_is_not_reported_delivered(repo, monkeypatch):
    root, _work, _owner = repo
    head = git(root, 'rev-parse', 'HEAD')
    work = reviewed_project_task(repo, monkeypatch)
    (work / 'feature.txt').write_text('reviewed work\n')
    published = checkout_landing.publish

    def publish_then_reset(top, branch_ref, base, commit, steps, reflog):
        published(top, branch_ref, base, commit, steps, reflog)
        # Another writer rewinds the branch before the receipt is written.
        git(top, 'update-ref', branch_ref, base, commit)

    monkeypatch.setattr(checkout_landing, 'publish', publish_then_reset)
    receipt = settle(repo)['delivery']
    assert receipt['state'] != 'delivered', receipt
    assert not delivery.canonical_delivery(origin_of(repo))
    assert git(root, 'rev-parse', 'HEAD') == head

    monkeypatch.setattr(checkout_landing, 'publish', published)
    delivery.resume_pending_workspace_deliveries()
    origin = origin_of(repo)
    assert origin['delivery']['state'] == 'delivered', origin['delivery']
    assert delivery.canonical_delivery(origin)
    target = origin['delivery']['targetSha']
    assert git(root, 'merge-base', '--is-ancestor', target, 'refs/heads/main') == ''
    assert git(root, 'show', f'{target}:feature.txt') == 'reviewed work'


def test_project_delivery_without_the_commit_opt_in_stays_a_working_tree_delivery(repo, monkeypatch):
    root, _work, _owner = repo
    head = git(root, 'rev-parse', 'HEAD')
    work = reviewed_project_task(repo, monkeypatch, opted_in=False)
    (work / 'feature.txt').write_text('reviewed work\n')

    origin = settle(repo)

    assert origin['delivery']['state'] == 'delivered'
    assert origin['delivery']['publicationMode'] == 'working_tree'
    assert origin['deliveryCheckpoint'] == {'status': 'disabled'}
    assert git(root, 'rev-parse', 'HEAD') == head
    assert (root / 'feature.txt').read_text() == 'reviewed work\n'
    # The repository grants no commit authority: its working tree is the publication.
    assert delivery.canonical_delivery(origin)


COMMITTED = {'status': 'committed', 'branchRef': 'refs/heads/main', 'parentSha': 'b' * 40,
             'commitSha': 'c' * 40, 'indexSynchronized': True}
WORKING_TREE = {'state': 'delivered', 'phase': 'workspace_adoption', 'reason': '',
                'checkpointSha': 'a' * 40, 'targetSha': 'd' * 40, 'publicationMode': 'working_tree'}
# (label, receipt, commit acknowledgement, canonical, projected delivery status)
CANONICAL_EVIDENCE = [
    ('working tree, commit pending', WORKING_TREE, None, False, 'pending'),
    ('working tree, entangled', WORKING_TREE,
     {'status': 'entangled', 'branchRef': 'refs/heads/main', 'headSha': 'b' * 40,
      'reason': 'conflict', 'conflictPaths': ['a.txt'], 'conflictPathCount': 1,
      'conflictPathsTruncated': False}, False, 'blocked'),
    ('working tree, whole-workspace checkpoint', WORKING_TREE,
     {'schema': 'tofu.linear-git-checkpoint/v2', 'status': 'committed', 'repositories': []},
     False, 'blocked'),
    ('working tree, commit skipped', WORKING_TREE,
     {'status': 'skipped', 'reason': 'The checkout has no committed branch to receive the delivery'},
     False, 'blocked'),
    ('working tree, committed', WORKING_TREE, COMMITTED, True, 'delivered'),
    ('working tree, delta already at HEAD', WORKING_TREE,
     {'status': 'no_changes', 'branchRef': 'refs/heads/main', 'headSha': 'b' * 40}, True, 'delivered'),
    ('working tree, repository takes no automatic commits', WORKING_TREE, {'status': 'disabled'},
     True, 'delivered'),
    ('committed delivery', {**WORKING_TREE, 'phase': 'commit_adoption', 'publicationMode': 'committed',
                            'targetSha': 'c' * 40, 'headSha': 'c' * 40}, None, True, 'delivered'),
    ('no-change delivery', {**WORKING_TREE, 'phase': 'no_changes', 'targetSha': 'b' * 40}, None,
     True, 'delivered'),
    ('held delivery', {**WORKING_TREE, 'state': 'blocked', 'reason': 'task_delta_entangled: a.txt'},
     None, False, 'blocked'),
]


@pytest.mark.parametrize('label, receipt, acknowledgement, canonical, status', CANONICAL_EVIDENCE,
                         ids=[case[0] for case in CANONICAL_EVIDENCE])
def test_canonical_delivery_requires_commit_evidence(runtime, monkeypatch, label, receipt,
                                                     acknowledgement, canonical, status):
    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id'])
    r.attempts[row['execution']['attemptId']]['status'] = 'completed'
    origin = {KEY: r.c.repo._admission(row), 'delivery': dict(receipt)}
    if acknowledgement is not None:
        origin['deliveryCheckpoint'] = dict(acknowledgement)

    def workspace_get(project, task_id, *, user_id):
        assert task_id == row['execution']['taskId']
        return {'task_id': task_id, 'user_id': user_id, 'project_root': project, 'origin': origin}

    monkeypatch.setattr(r.c.repo, 'workspace_get', workspace_get)
    view = r.service.view(row['id'], principal=r.c.principal)
    assert delivery.canonical_delivery(origin) is canonical
    assert view['canonicalDelivery'] is canonical
    # Every delivery_view consumer reads status: delivered means committed.
    assert view['delivery']['status'] == status
    if receipt['state'] == 'delivered' and not canonical:
        assert view['delivery']['reason'].startswith('delivered_uncommitted:')
