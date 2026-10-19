"""Finite standing authority: fake CAS, real dispatch gates, no paid effects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace
import pytest
from lib.identity import PrincipalContext
from lib.project_dispatch.repository import ProjectDispatchError, record_stop
from lib.project_dispatch.responsibilities import NAMESPACE, ProjectResponsibilities, validate
from lib.project_dispatch.responsibilities_adapter import responsibility_adapter
from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_repository import context as context
pytestmark = pytest.mark.unit


class AutomationPort:
    def __init__(self):
        self.definitions, self.runs = {}, {}
        self.published, self.paused, self.cancelled = [], [], []
    def get(self, kind, identity):
        return deepcopy((self.definitions if kind == 'definition' else self.runs).get(identity))
    def publish(self, prepared, **kwargs):
        from lib.automations.publication import publication_record, validate_publication_record
        p = self.principal
        record = publication_record(prepared, p)
        validate_publication_record(record, prepared.definition_json, prepared.action_contracts_json,
                                    owner=p.require_owner(), tenant=p.tenant_id or '')
        self.published.append(prepared.definition)
        self.definitions[prepared.definition['id']] = SimpleNamespace(value=record, version=1)
        return self.definitions[prepared.definition['id']]
    def pause_definition(self, entity, **kwargs):
        identity = entity.value['definition']['id']
        self.definitions[identity].value['definition']['enabled'] = False
        self.paused.append(identity)
    def cancel(self, run_id, **kwargs):
        self.cancelled.append(run_id)


class ChildPort:
    def __init__(self, repo):
        self.repo, self.dispatched, self.cancelled, self.states = repo, [], [], {}
    def dispatch(self, identity, *, principal):
        self.repo.validate_own(self.repo.admission(identity))
        self.dispatched.append((identity, principal.to_payload()))
        self.repo.bind_execution(identity, 'child-turn', 'child-attempt')
        self.states.setdefault(identity, 'running')
        return self.view(identity, principal=principal)
    def view(self, identity, *, principal):
        return {'execution': {'status': self.states[identity]}}
    def revoke(self, identity, *, principal, expected_revision):
        if self.repo.revoke(identity, expected_revision):
            self.cancelled.append(identity)

            self.states[identity] = 'cancelled'
        return {'revoked': True}


@pytest.fixture
def standing(context):
    c = context
    c.principal = PrincipalContext.user(subject_id='authenticated-api-key-7', owner_user_id=7,
        tenant_id='tenant-a', scopes=['chat', 'automations:manage', 'automations:read', 'automations:execute'])
    c.request['profile'] = {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}
    c.create = {'projectPath': '/project', 'name': 'finite consult', 'assignment': deepcopy(c.request),
        'trigger': {'kind': 'manual'}, 'allowance': {'maxRuns': 2, 'maxTotalTokens': 200, 'deadline': 2000},
        'perRunSeconds': 60}
    c.automation = AutomationPort()
    c.automation.principal = c.principal
    c.child = ChildPort(c.repo)
    c.new_service = lambda: ProjectResponsibilities(7, '/project', dispatch_repository=c.repo,
        dispatch_service=c.child, automation_factory=lambda p: c.automation, client=c.client, clock=lambda: c.now[0])
    c.service = c.new_service()
    c.view = c.service.create(c.create, principal=c.principal, command_id='standing-create')
    c.identity = c.view['responsibilityId']
    return c


def view(c):
    return c.service.read(c.identity, principal=c.principal)


def command(c):
    return {'projectPath': '/project', 'responsibilityId': c.identity, 'expectedRevision': view(c)['revision']}


def settle(c, usage=True):
    identity = view(c)['occurrences'][0]['assignmentId']
    c.child.states[identity] = 'completed'
    def mutation(a):
        a['modelAttempts'] = [{'root_task_id': 'root', 'attempt_ordinal': 1, 'attempt_task_id': 'physical',
            'reserved_tokens': 100, 'phase': 'settled', 'reported_tokens': 1, 'usage_complete': usage,
            'account_observed_tokens': 1, 'account_nonce': 'a'*32}]
    c.repo.mutate(identity, mutation)


def test_contract_original_principal(standing):
    c = standing
    validate(c.create, 'create'); validate(c.view, 'view')
    assert c.view['state'] == 'active' and len(c.automation.published) == 1
    assert 'principal' not in c.view
    assert c.service.run_occurrence(c.identity, 'one') == 'active'
    assert c.child.dispatched[0][1] == c.principal.to_payload()
    assert view(c)['occurrences'][0]['deadline'] == 1060


@pytest.mark.parametrize('runs,tokens', [(1, 200), (2, 100)])
def test_budget_exhaustion(standing, runs, tokens):
    c = standing
    request = deepcopy(c.create)
    request['allowance'].update(maxRuns=runs, maxTotalTokens=tokens)
    identity = c.service.create(request, principal=c.principal, command_id='other')['responsibilityId']
    assert c.service.run_occurrence(identity, 'first') == 'active'
    assert c.service.run_occurrence(identity, 'second') == 'cancelled'
    result = c.service.read(identity, principal=c.principal)
    assert result['state'] == 'exhausted'
    assert (result['runsReserved'], result['tokensReserved']) == (1, 100)
    assert c.automation.paused == [identity]


def test_duplicate_restart_lost_cas_ack(standing):
    c = standing
    c.client.uncertain_once = True
    c.service.run_occurrence(c.identity, 'one')
    assert c.new_service().run_occurrence(c.identity, 'one', reconcile=True) == 'active'
    assert (view(c)['runsReserved'], view(c)['tokensReserved']) == (1, 100)
    assert len(c.child.dispatched) == len(c.repo.list()) == 1


@pytest.mark.parametrize('keys,expected', [(['same', 'same'], 1), (['a', 'b'], 2)])
def test_multiworker_cas(standing, keys, expected):
    c = standing
    c.client.reads, c.client.first_reads = 0, Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda key: c.new_service()._reserve(c.identity, key), keys))
    assert view(c)['runsReserved'] == expected and view(c)['tokensReserved'] == expected*100
    assert c.client.conflicts >= 1


def test_pause_cancel_no_renewal(standing):
    c = standing
    c.service.run_occurrence(c.identity, 'one')
    assert c.service.pause(command(c), principal=c.principal, command_id='pause')['state'] == 'paused'
    assert not c.child.cancelled
    assert c.service.run_occurrence(c.identity, 'future') == 'cancelled'
    assert c.service.run_occurrence(c.identity, 'one', reconcile=True) == 'active'
    assert c.service.cancel(command(c), principal=c.principal, command_id='cancel')['state'] == 'cancelled'
    assert len(c.child.cancelled) == 1
    assert c.service.create(c.create, principal=c.principal, command_id='standing-create')['state'] == 'cancelled'
    c.new_service().cancel(command(c), principal=c.principal, command_id='cancel')
    assert view(c)['tokensReserved'] == 100


def test_deadline_offline_no_reset(standing):
    c = standing
    c.service.run_occurrence(c.identity, 'one'); c.now[0] = 2001
    assert c.new_service().run_occurrence(c.identity, 'catchup') == 'cancelled'
    assert view(c)['state'] == 'expired'
    assert c.service.run_occurrence(c.identity, 'one', reconcile=True) == 'cancelled'
    assert view(c)['tokensReserved'] == 100 and len(c.child.dispatched) == 1


def test_missing_usage_unknown_no_replay_refund(standing):
    c = standing
    c.service.run_occurrence(c.identity, 'one'); settle(c, usage=False)
    assert c.service.run_occurrence(c.identity, 'one', reconcile=True) == 'unknown'
    c.new_service().reconcile(command(c), principal=c.principal, command_id='reconcile')
    assert view(c)['occurrences'][0]['status'] == 'unknown' and view(c)['tokensReserved'] == 100
    assert len(c.child.dispatched) == 1
    settle(c)
    assert c.service.run_occurrence(c.identity, 'one', reconcile=True) == 'completed'
    assert view(c)['tokensReserved'] == 100


def test_lost_grant_ack_exact_readback(standing, monkeypatch):
    c = standing
    authorize = c.repo.authorize
    def lost(*args, **kwargs):
        authorize(*args, **kwargs)
        raise StorageError('database_timeout', 'lost ACK', retryable=True)
    monkeypatch.setattr(c.repo, 'authorize', lost)
    with pytest.raises(StorageError): c.service.run_occurrence(c.identity, 'one')
    assert view(c)['occurrences'][0]['status'] == 'unknown'
    assert c.new_service().run_occurrence(c.identity, 'one', reconcile=True) == 'active'
    assert len(c.repo.list()) == len(c.child.dispatched) == 1


def test_unknown_absent_child_never_reauthorizes(standing, monkeypatch):
    c, calls = standing, []
    def unavailable(*args, **kwargs):
        calls.append(kwargs['command_id'])
        raise StorageError('database_unavailable', 'offline', retryable=True)
    monkeypatch.setattr(c.repo, 'authorize', unavailable)
    with pytest.raises(StorageError): c.service.run_occurrence(c.identity, 'one')
    assert c.new_service().run_occurrence(c.identity, 'one', reconcile=True) == 'unknown'
    assert c.service.run_occurrence(c.identity, 'one') == 'unknown'
    assert len(calls) == 1 and view(c)['tokensReserved'] == 100


@pytest.mark.parametrize('gate', ['source', 'owner_bool', 'binding', 'scope', 'stop'])
def test_current_gates_every_occurrence(standing, gate):
    c = standing
    c.service.run_occurrence(c.identity, 'one')
    if gate == 'source': c.turn['projectionRevision'] += 1
    elif gate == 'owner_bool': c.turn['user_id'] = True
    elif gate == 'binding': c.binding.value['conversationId'] = 'different'
    elif gate == 'scope': c.conversations.rows['recipient']['settings']['readOnly'] = True
    else: record_stop(7, 'coordinator', 1001, client=c.client)
    with pytest.raises(ProjectDispatchError): c.service.run_occurrence(c.identity, 'two')
    assert view(c)['runsReserved'] == len(c.child.dispatched) == 1


def test_private_bool_owner_denied(standing):
    c = standing
    c.client.rows[(NAMESPACE, c.service.key)]['value']['responsibilities'][0]['principal']['owner_user_id'] = True
    with pytest.raises(ProjectDispatchError): c.new_service().run_occurrence(c.identity, 'one')
    assert not c.child.dispatched


def test_adapter_exact_system_publication_binding(standing):
    c = standing
    system = PrincipalContext.system(subject_id=f'automation:7:{c.identity}', owner_user_id=7,
        tenant_id='tenant-a', scopes=['chat', 'automations:execute'])
    ctx = SimpleNamespace(principal=system, cancellation_requested=False, checkpoint=lambda **kwargs: None)
    invocation = SimpleNamespace(context=ctx, arguments={'projectPath': '/project', 'responsibilityId': c.identity},
                                 event={}, effect_key='ar_fake:responsibility')
    adapter = responsibility_adapter(client_factory=c.client, service_factory=lambda *args: c.service)
    assert adapter.execute(invocation)['status'] == 'running'
    assert c.child.dispatched[0][1]['subject_id'] == 'authenticated-api-key-7'
    ctx.principal = c.principal
    with pytest.raises(PermissionError): adapter.reconcile(invocation)


def test_cancel_between_authorization_and_entry(standing, monkeypatch):
    c = standing
    authorize = c.repo.authorize
    def cancel_between(*args, **kwargs):
        result = authorize(*args, **kwargs)
        c.service.cancel(command(c), principal=c.principal, command_id='race-cancel')
        return result
    monkeypatch.setattr(c.repo, 'authorize', cancel_between)
    assert c.service.run_occurrence(c.identity, 'one') == 'cancelled'
    assert not c.child.dispatched and c.repo.list()[0]['grant']['revoked']
    assert view(c)['tokensReserved'] == 100


def test_public_cannot_supply_principal(standing):
    request = deepcopy(standing.create); request['principal'] = standing.principal.to_payload()
    with pytest.raises(ProjectDispatchError):
        standing.service.create(request, principal=standing.principal, command_id='forged')


def test_unknown_external_file_effect_retains_full_allowance(standing):
    c = standing
    request = deepcopy(c.create)
    request['assignment']['profile'] = {'mode': 'files', 'tools': ['read_files', 'write_file'],
        'readPaths': ['src'], 'writePaths': ['src'], 'maxToolCalls': 2}
    c.identity = c.service.create(request, principal=c.principal, command_id='files')['responsibilityId']
    c.service.run_occurrence(c.identity, 'file-effect')
    settle(c)
    identity = view(c)['occurrences'][0]['assignmentId']
    def uncertain(a):
        a['toolAttempts'] = [{'root_task_id': 'root', 'task_id': 'physical', 'tool_ordinal': 1,
            'tool_call_id': 'write', 'tool_name': 'write_file', 'phase': 'executor_exception',
            'account_nonce': 'b'*32}]
    c.repo.mutate(identity, uncertain)
    assert c.service.run_occurrence(c.identity, 'file-effect', reconcile=True) == 'unknown'
    assert c.new_service().run_occurrence(c.identity, 'file-effect', reconcile=True) == 'unknown'
    assert view(c)['tokensReserved'] == 100 and len(c.child.dispatched) == 1


def test_cancel_uncertain_child_waits_for_terminal_effect_evidence(standing, monkeypatch):
    c = standing
    c.service.run_occurrence(c.identity, 'one')
    original = c.child.revoke
    def revoke_only(*args, **kwargs):
        original(*args, **kwargs)
        c.child.states[args[0]] = 'running'
    monkeypatch.setattr(c.child, 'revoke', revoke_only)
    c.service.cancel(command(c), principal=c.principal, command_id='cancel-active')
    assert view(c)['occurrences'][0]['status'] == 'unknown'
    assert c.service.run_occurrence(c.identity, 'one', reconcile=True) == 'unknown'
    assert len(c.child.dispatched) == 1
    monkeypatch.setattr(c.child, 'revoke', original)
    assert c.new_service().run_occurrence(c.identity, 'one', reconcile=True) == 'cancelled'
    assert view(c)['tokensReserved'] == 100


def test_deadline_between_grant_and_dispatch(standing, monkeypatch):
    c = standing
    original = c.repo.authorize
    def slow_authorization(*args, **kwargs):
        child = original(*args, **kwargs)
        c.now[0] = 1061
        return child
    monkeypatch.setattr(c.repo, 'authorize', slow_authorization)
    assert c.service.run_occurrence(c.identity, 'one') == 'cancelled'
    assert not c.child.dispatched and c.repo.list()[0]['grant']['revoked']
    assert view(c)['tokensReserved'] == 100


def test_cancel_lost_grant_ack_readback_withdraws_exact_child(standing, monkeypatch):
    c = standing
    original = c.repo.authorize
    def cancel_before_return(*args, **kwargs):
        child = original(*args, **kwargs)
        c.service.cancel(command(c), principal=c.principal, command_id='cancel-lost-ack')
        raise StorageError('database_timeout', 'ACK lost after cancellation', retryable=True)
    monkeypatch.setattr(c.repo, 'authorize', cancel_before_return)
    with pytest.raises(StorageError): c.service.run_occurrence(c.identity, 'one')
    assert c.new_service().run_occurrence(c.identity, 'one', reconcile=True) == 'cancelled'
    assert len(c.repo.list()) == 1 and c.repo.list()[0]['grant']['revoked']
    assert not c.child.dispatched


def test_cancel_pending_occurrence_does_not_disable_future_triggers(standing):
    c = standing
    assert c.service.run_occurrence(c.identity, 'never-reserved', cancelling=True) == 'cancelled'
    assert view(c)['state'] == 'active' and not c.automation.paused
    assert c.service.run_occurrence(c.identity, 'next') == 'active'


def test_mismatched_preexisting_child_command_scope_denied(standing):
    c = standing
    occurrence = c.service._reserve(c.identity, 'one')
    request = deepcopy(c.request)
    request['budget']['deadline'] = occurrence['deadline']
    request['model'] = 'different-model'
    c.repo.authorize(request, c.principal, occurrence['commandId'])
    with pytest.raises(ProjectDispatchError, match='frozen human authorization'):
        c.service.run_occurrence(c.identity, 'one', reconcile=True)
    assert not c.child.dispatched


@pytest.mark.parametrize('child_exists', [False, True])
def test_original_binding_epoch_cannot_revive_after_a_b_a(standing, child_exists):
    c = standing
    if child_exists:
        c.service.run_occurrence(c.identity, 'old')
    c.binding.value.update(conversationId='coordinator-b', revision=2)
    c.binding.value.update(conversationId='coordinator', revision=3)
    with pytest.raises(ProjectDispatchError, match='binding epoch'):
        c.new_service().run_occurrence(c.identity, 'future')
    assert view(c)['runsReserved'] == int(child_exists)
    assert len(c.child.dispatched) == int(child_exists)
    if child_exists:
        assert c.new_service().run_occurrence(c.identity, 'old', reconcile=True) == 'cancelled'
        assert c.repo.list()[0]['grant']['revoked']
    # Replay of the original human create is evidence, never epoch renewal.
    assert c.service.create(c.create, principal=c.principal, command_id='standing-create')['runsReserved'] == int(child_exists)
    assert c.service._row(c.identity)['bindingRevision'] == 1


@pytest.mark.parametrize('epoch', [True, 1.0, 0, '1', None])
def test_original_binding_epoch_strict_type_and_missing_deny(standing, epoch):
    c = standing
    c.client.rows[(NAMESPACE, c.service.key)]['value']['responsibilities'][0]['bindingRevision'] = epoch
    with pytest.raises(ProjectDispatchError):
        c.new_service().run_occurrence(c.identity, 'one')
    assert not c.child.dispatched


def test_live_binding_epoch_bool_denied(standing):
    c = standing
    c.binding.value['revision'] = True
    with pytest.raises(ProjectDispatchError, match='binding epoch'):
        c.service.run_occurrence(c.identity, 'one')
    assert not c.child.dispatched


def project_stop(c, *, targets=None, kind='stop_project'):
    if targets is None:
        targets = [{'assignmentId': row['id'], 'grantRevision': row['grant']['revision']}
                   for row in c.repo.list() if not row['grant']['revoked']]
    return c.repo.revoke_scope_change({
        'projectPath': '/project', 'coordinatorId': 'coordinator',
        'authorizationSource': deepcopy(c.request['authorizationSource']),
        'kind': kind, 'note': 'Stop previous authority', 'targets': targets,
    }, principal=c.principal, command_id='project-stop')


def test_project_stop_fences_existing_responsibility_without_new_debit(standing):
    c = standing
    c.service.run_occurrence(c.identity, 'first'); settle(c)
    assert c.service.run_occurrence(c.identity, 'first', reconcile=True) == 'completed'
    c.now[0] += 1
    project_stop(c)
    assert c.new_service().run_occurrence(c.identity, 'second') == 'cancelled'
    assert view(c)['state'] == 'cancelled'
    assert view(c)['tokensReserved'] == 100 and view(c)['runsReserved'] == 1
    assert len(c.child.dispatched) == len(c.repo.list()) == 1
    assert c.automation.paused == [c.identity]
    c.now[0] += 1
    fresh = c.service.create(c.create, principal=c.principal, command_id='new-human-authorization')
    assert c.service.run_occurrence(fresh['responsibilityId'], 'fresh') == 'active'
    assert len(c.child.dispatched) == 2


def test_project_stop_without_current_grants_still_withdraws_future_authority(standing):
    c = standing
    c.now[0] += 1
    project_stop(c, targets=[])
    assert c.new_service().run_occurrence(c.identity, 'first') == 'cancelled'
    assert not c.repo.list() and not c.child.dispatched
    assert view(c)['tokensReserved'] == view(c)['runsReserved'] == 0
    assert c.automation.paused == [c.identity]


def test_empty_shrink_cannot_claim_project_withdrawal(standing):
    c = standing
    with pytest.raises(ProjectDispatchError, match='at least one'):
        project_stop(c, targets=[], kind='shrink')
    assert not c.repo.scope_changes()
    assert c.service.run_occurrence(c.identity, 'first') == 'active'


def test_project_stop_racing_child_authorization_blocks_entry(standing, monkeypatch):
    c = standing
    authorize = c.repo.authorize
    def stop_after_grant(*args, **kwargs):
        result = authorize(*args, **kwargs)
        c.now[0] += 1
        project_stop(c)
        return result
    monkeypatch.setattr(c.repo, 'authorize', stop_after_grant)
    assert c.service.run_occurrence(c.identity, 'first') == 'cancelled'
    assert not c.child.dispatched and c.repo.list()[0]['grant']['revoked']
    assert view(c)['tokensReserved'] == 100
