"""Project review is not delivery; only current explicit publication releases it."""
from copy import deepcopy
from pathlib import Path

import json

import jsonschema
import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import admission, publication
from lib.project_mod import delivery
from tests.storage.test_integration_delivery import repo as repo_fixture, git, automatic_settle

repo = repo_fixture  # Temporary Git + offline SQLite authority, never live storage.
pytestmark = pytest.mark.unit


class FakeRepository:
    """Only dispatch authority is replaced; workspace/Git delivery stays real."""
    def __init__(self, root):
        self.binding = {'version': 1, 'owner': 1, 'tenant': None, 'projectPath': str(root),
                        'assignmentId': 'assignment', 'revision': 1, 'recipientId': 'recipient'}
        self.assignment = {
            'id': 'assignment',
            'request': {'projectPath': str(root), 'recipientId': 'recipient',
                        'profile': {'mode': 'files', 'tools': ['read_files', 'write_file'],
                                    'readPaths': ['allowed', 'base.txt'], 'writePaths': ['allowed'], 'maxToolCalls': 8}},
            'grant': {'owner': 1, 'tenant': None, 'revision': 1, 'revoked': False},
            'execution': {'turnId': 'turn', 'attemptId': 'attempt'},
            'review': None, 'publication': None,
        }
        self.attempt = {'attemptId': 'attempt', 'turnId': 'turn', 'conversationId': 'recipient',
                        'taskId': 'automatic', 'status': 'completed'}
        self.result_revision = 7
        self.source_current = True
        self.calls = []

    def accept(self):
        self.assignment['review'] = {'decision': 'accept', 'resultRevision': 7}

    def publish(self, checkpoint_sha=None):
        self.accept()
        if checkpoint_sha is None:
            root = self.binding['projectPath']
            row = state.get_workspace(root, self.attempt['taskId'], user_id=self.binding['owner'])
            if not row.get('checkpoint_sha'):
                ic.checkpoint_workspace(root, self.attempt['taskId'], user_id=self.binding['owner'])
                row = state.get_workspace(root, self.attempt['taskId'], user_id=self.binding['owner'])
            checkpoint_sha = row['checkpoint_sha']
        self.assignment['publication'] = {'grantRevision': 1, 'resultRevision': 7,
                                          'checkpointSha': checkpoint_sha, 'subject': 'user:1', 'requestedAt': 1.0}

    def validate_publication(self, binding, task_id):
        self.calls.append(task_id)
        row = self.assignment
        if (binding != self.binding or row['grant']['revoked'] or not self.source_current
                or not row['publication'] or not row['review']
                or row['review']['decision'] != 'accept'
                or row['publication']['resultRevision'] != self.result_revision
                or self.attempt['taskId'] != task_id or self.attempt['status'] != 'completed'):
            raise PermissionError('No current exact publication authority')
        return deepcopy(row)


@pytest.fixture
def authority(repo, monkeypatch):
    root, _, _ = repo
    repository = FakeRepository(root)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: repository)
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(root.parent / 'managed'))
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **k: (True, 'offline gate'))
    return repository


def workspace(repo, authority):
    root, _, owner = repo
    created = ic.create_task_workspace(str(root), 'automatic', user_id=owner,
                                      conversation_id='recipient',
                                      task={'config': {admission.KEY: authority.binding}})
    delivery.bind_task_workspace(str(root), 'automatic', user_id=owner, conversation_id='recipient')
    return Path(created['workspacePath'])


def receipt(root):
    return state.get_workspace(str(root), 'automatic', user_id=1)['origin']['delivery']


def test_first_registration_pins_independent_binding_and_never_backfills(repo, authority, monkeypatch):
    root, _, _ = repo
    registrations = []
    original = state.register_workspace
    def capture(**kwargs):
        registrations.append(deepcopy(kwargs['origin']))
        return original(**kwargs)
    monkeypatch.setattr(state, 'register_workspace', capture)
    workspace(repo, authority)
    assert registrations[0][admission.KEY] == authority.binding
    authority.binding['revision'] = 99
    row = state.get_workspace(str(root), 'automatic', user_id=1)
    assert row['origin'][admission.KEY]['revision'] == 1
    # Repeated registration cannot replace the originally admitted authority.
    workspace(repo, authority)
    assert len(registrations) == 1
    assert state.get_workspace(str(root), 'automatic', user_id=1)['origin'][admission.KEY]['revision'] == 1


def test_checkpoint_held_accepted_held_then_explicitly_published_and_projected(repo, authority):
    root, _, _ = repo
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('reviewable result\n')
    before_head, before_index = git(root, 'rev-parse', 'HEAD'), git(root, 'write-tree')
    held = automatic_settle(repo)
    assert held['state'] == 'blocked'
    assert held['reason'] == 'project_publication_authority_missing'
    assert held['checkpointSha']
    assert not (root / 'allowed/result.txt').exists()
    row = state.get_workspace(str(root), 'automatic', user_id=1)
    assert row['state'] == 'checkpointed'
    view = publication.delivery_view(authority.assignment, authority.attempt, state.get_workspace)

    schema = json.loads((Path(__file__).resolve().parents[2] /
                         'contracts/project_dispatch_v1.schema.json').read_text())
    jsonschema.validate(view, {**schema, '$ref': '#/$defs/delivery'})
    assert view == {'taskId': 'automatic', 'status': 'blocked', 'reason': held['reason'],
                    'checkpointSha': held['checkpointSha'], 'targetSha': ''}
    authority.accept()
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'blocked'
    assert not (root / 'allowed/result.txt').exists()
    # Explicit command authorization wakes the existing integration receiver;
    # this offline fixture runs its one bounded tick directly.
    authority.publish()
    assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'allowed/result.txt').read_text() == 'reviewable result\n'
    assert git(root, 'rev-parse', 'HEAD') == before_head
    assert git(root, 'write-tree') == before_index
    assert publication.delivery_view(authority.assignment, authority.attempt,
                                     state.get_workspace)['status'] == 'delivered'


@pytest.mark.parametrize('mutation', ['revoke', 'source', 'result', 'task'])
def test_postmerge_authority_mutation_cannot_apply(repo, authority, monkeypatch, mutation):
    root, _, _ = repo
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('must not publish\n')
    authority.publish()
    from lib.project_mod import task_delta_delivery
    original = task_delta_delivery._merge_workspace_delta
    def merge(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == 'revoke':
            authority.assignment['grant']['revoked'] = True
        elif mutation == 'source':
            authority.source_current = False
        elif mutation == 'result':
            authority.result_revision += 1
        else:
            authority.attempt['taskId'] = 'replacement-task'
        return result
    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', merge)
    result = automatic_settle(repo)
    assert result['state'] == 'blocked'
    assert result['reason'] == 'project_publication_authority_missing'
    assert not result.get('targetSha')
    assert not (root / 'allowed/result.txt').exists()
    assert len(authority.calls) >= 3


def test_revocation_after_intent_ack_is_fenced_before_apply(repo, authority, monkeypatch):
    root, _, _ = repo
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('must not apply\n')
    authority.publish()
    original = delivery._save
    def revoke_after_intent(row, current, **updates):
        saved = original(row, current, **updates)
        if updates.get('state') == 'pending' and updates.get('targetSha'):
            authority.assignment['grant']['revoked'] = True
        return saved
    monkeypatch.setattr(delivery, '_save', revoke_after_intent)
    result = automatic_settle(repo)
    assert result['state'] == 'blocked'
    assert result['reason'] == 'project_publication_authority_missing'
    assert not (root / 'allowed/result.txt').exists()


@pytest.mark.parametrize('accepted', [False, True])
def test_no_change_does_not_bypass_explicit_publication(repo, authority, accepted):
    root, _, _ = repo
    work = workspace(repo, authority)
    if accepted:
        authority.accept()
    assert automatic_settle(repo)['state'] == 'blocked'
    assert work.exists()  # No premature no-change source cleanup.
    authority.publish()
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert receipt(root)['phase'] == 'no_changes'


def test_borrowed_wip_is_readable_but_excluded_from_write_scope_and_publication(repo, authority):
    root, _, _ = repo
    (root / 'base.txt').write_text('borrowed canonical WIP\n')
    git(root, 'add', 'base.txt')
    before_index = git(root, 'write-tree')
    work = workspace(repo, authority)
    assert (work / 'base.txt').read_text() == 'borrowed canonical WIP\n'
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('scoped task result\n')
    authority.publish()
    assert automatic_settle(repo)['state'] == 'delivered'
    assert (root / 'base.txt').read_text() == 'borrowed canonical WIP\n'
    assert git(root, 'write-tree') == before_index


@pytest.mark.parametrize('change', ['new', 'delete', 'rename', 'prefix'])
def test_checkpoint_scope_denies_outside_write_paths_without_partial_delivery(repo, authority, change):
    root, _, _ = repo
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('scoped task result\n')
    if change == 'new':
        (work / 'outside.txt').write_text('out of scope\n')
    elif change == 'delete':
        (work / 'base.txt').unlink()
    elif change == 'rename':
        (work / 'base.txt').rename(work / 'allowed/renamed.txt')
    else:
        (work / 'allowed-evil').mkdir()
        (work / 'allowed-evil/file.txt').write_text('not a directory prefix\n')
    authority.publish()
    result = automatic_settle(repo)
    assert result['state'] == 'blocked'
    assert result['reason'] == 'project_publication_write_scope_exceeded'
    assert not (root / 'allowed/result.txt').exists()
    assert (root / 'base.txt').read_text() == 'base\n'


def test_lost_receipt_recovery_rechecks_live_authority(repo, authority):
    root, _, _ = repo
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('already delivered\n')
    authority.publish()
    delivered = automatic_settle(repo)
    state.set_workspace_meta(project_root=str(root), task_id='automatic', user_id=1,
                             now=ic._now(), patch={'delivery': {**delivered, 'state': 'pending'}})
    authority.assignment['grant']['revoked'] = True
    delivery._RECOVERY_CURSOR = 0
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'blocked'
    assert receipt(root)['reason'] == 'project_publication_authority_missing'
    # Revocation does not undo an already-published filesystem fact.
    assert (root / 'allowed/result.txt').read_text() == 'already delivered\n'


def test_projection_rejects_wrong_association_or_workspace_owner(repo, authority):
    root, _, _ = repo
    workspace(repo, authority)
    automatic_settle(repo)
    wrong = {**authority.attempt, 'taskId': 'other'}
    assert publication.delivery_view(authority.assignment, wrong, state.find_workspace) is None
    wrong = {**authority.attempt, 'attemptId': 'other'}
    assert publication.delivery_view(authority.assignment, wrong, state.find_workspace) is None
    row = state.get_workspace(str(root), 'automatic', user_id=1)
    assert publication.delivery_view(authority.assignment, authority.attempt,
                                     lambda *a, **k: {**row, 'user_id': 2}) is None
    assert publication.publication_reason({**row, 'user_id': 2}, receipt(root)) == 'project_publication_identity_changed'
    generic = {**row, 'origin': {k: v for k, v in row['origin'].items() if k != admission.KEY}}
    assert publication.publication_reason(generic, receipt(root)) == ''


@pytest.mark.parametrize('fence', ['postgate', 'postintent'])
def test_explicit_adoption_rechecks_before_fast_forward(repo, authority, monkeypatch, fence):
    root, work, owner = repo
    before = git(root, 'rev-parse', 'HEAD')
    authority.attempt['taskId'] = 'task'
    authority.assignment['request']['profile']['writePaths'] = ['feature.txt']
    # The explicit-workspace fixture registers its carrier at creation too.
    state.register_workspace(project_root=str(root), task_id='task', user_id=owner,
                             workspace_path=str(work), title='task', base_sha=before,
                             managed=False, now=ic._now(),
                             origin={admission.KEY: deepcopy(authority.binding), 'sourceSnapshot': before})
    delivery.bind_task_workspace(str(work), 'task', user_id=owner, conversation_id='recipient')
    (work / 'feature.txt').write_text('must not reach canonical\n')
    delivery.settle_task_workspace(str(work), 'task', user_id=owner, outcome='completed')
    authority.publish()
    delivery.settle_task_workspace(str(work), 'task', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    assert ic.process_ready_once()
    if fence == 'postgate':
        def gate(*args, **kwargs):
            authority.assignment['grant']['revoked'] = True
            return True, 'passed but revoked'
        monkeypatch.setattr(ic, '_gate_commands', gate)
    else:
        original = delivery._save
        def save(row, current, **updates):
            result = original(row, current, **updates)
            if updates.get('state') == 'pending' and updates.get('targetSha'):
                authority.assignment['grant']['revoked'] = True
            return result
        monkeypatch.setattr(delivery, '_save', save)
    result = delivery.deliver_registered_workspace(str(root), 'task', user_id=owner)
    assert result['state'] == 'blocked'
    assert result['reason'] == 'project_publication_authority_missing'
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not (root / 'feature.txt').exists()


def test_missing_baseline_is_a_concrete_publication_limitation(tmp_path, monkeypatch):
    authority = FakeRepository(tmp_path)
    authority.publish('immutable-checkpoint')
    monkeypatch.setattr(admission, 'repository_for', lambda binding: authority)
    row = {'user_id': 1, 'project_root': str(tmp_path), 'task_id': 'automatic',
           'checkpoint_sha': 'immutable-checkpoint', 'origin': {admission.KEY: authority.binding}}
    assert publication.publication_reason(row, {}) == 'project_publication_checkpoint_unavailable'
    authority.assignment['request'].pop('profile')
    assert publication.publication_reason(row, {}) == 'project_publication_scope_missing'
    authority.assignment['request']['profile'] = {'mode': 'consult', 'writePaths': []}
    assert publication.publication_reason(row, {}) == 'project_publication_scope_missing'


@pytest.mark.parametrize('bad_owner', [True, 1.0, 1.5, '1', 0, None],
                         ids=['bool', 'integral-float', 'fractional-float', 'string', 'zero', 'null'])
def test_publication_and_projection_reject_untyped_workspace_owner(repo, authority, bad_owner):
    root, _, _ = repo
    workspace(repo, authority)
    automatic_settle(repo)
    authority.publish()
    row = state.get_workspace(str(root), 'automatic', user_id=1)
    # Positive control uses the same exact checkpoint and authority.
    assert publication.publication_reason(row, receipt(root)) == ''
    malformed = {**row, 'user_id': bad_owner}
    before = len(authority.calls)
    assert publication.publication_reason(malformed, receipt(root)) == 'project_publication_identity_changed'
    assert len(authority.calls) == before  # Identity refusal precedes publication authority/Git admission.
    assert publication.delivery_view(authority.assignment, authority.attempt,
                                     lambda *a, **k: malformed) is None
