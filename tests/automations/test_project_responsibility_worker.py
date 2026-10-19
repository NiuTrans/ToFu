"""Actual canonical automation worker suspends and reconciles a responsibility."""
from copy import deepcopy

import hashlib
import time
import pytest

from lib.automations.execution import AutomationExecutionHandler, ExecutionRegistry
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner
from lib.identity import PrincipalContext
from lib.project_dispatch.responsibilities import ProjectResponsibilities
from lib.project_dispatch.responsibilities_adapter import responsibility_adapter, responsibility_registry
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_project_responsibilities import ChildPort, settle, view
from tests.support.worker_job_time import wall_ms, wait_until_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_real_worker_waits_reconciles_exact_occurrence(semantic_authority, context):
    factory, owner, native_engine = semantic_authority
    if native_engine is not None:
        # The native authority admits only its compiled closed action catalog.
        # A historical binary cannot validate this newly registered action.
        with native_engine._binary.open('rb') as binary:
            binary_sha256 = hashlib.file_digest(binary, 'sha256').hexdigest()
        compiled_source = native_engine._build_identity.get('source_sha256')
        checkout_source = native_engine._checkout_source_sha256
        assert compiled_source and compiled_source == checkout_source, (
            'Native responsibility acceptance requires a source-matched TOFUDB_BIN: '
            f'binary_source={native_engine._binary_source}; '
            f'binary_sha256={binary_sha256}; '
            f'compiled_source_sha256={compiled_source}; '
            f'checkout_source_sha256={checkout_source}'
        )
    c = context
    for conversation in c.conversations.rows.values():
        conversation['user_id'] = owner
    c.repo = c.repository(owner=owner)
    c.now[0] = time.time()
    c.principal = PrincipalContext.user(subject_id='original-authenticated-publisher', owner_user_id=owner,
        tenant_id='test-tenant', scopes=['chat', 'automations:manage', 'automations:execute', 'automations:read'])
    c.request['budget']['deadline'] = c.now[0] + 120
    c.request['profile'] = {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}
    c.child = ChildPort(c.repo)
    repo = AutomationExecutionRepository(c.principal, client_factory=factory)
    c.service = ProjectResponsibilities(owner, '/project', client=c.client, dispatch_repository=c.repo,
        dispatch_service=c.child, automation_factory=lambda principal: repo, clock=lambda: c.now[0])
    request = {'projectPath': '/project', 'name': 'finite manual responsibility', 'assignment': deepcopy(c.request),
        'trigger': {'kind': 'manual'}, 'allowance': {'maxRuns': 1, 'maxTotalTokens': 100,
            'deadline': c.now[0]+120}, 'perRunSeconds': 60}
    created = c.service.create(request, principal=c.principal, command_id='real-worker-create')
    c.identity = created['responsibilityId']
    event = {'id': 'explicit-manual', 'kind': 'manual', 'occurredAt': c.now[0], 'origin': 'human',
             'evidence': {}, 'automationChain': []}
    run = repo.admit(c.identity, event, now=c.now[0])
    class Store(StorageWorkerJobStore):
        def _client(self, *, write=False):
            return factory(write=write)
    adapter = responsibility_adapter(client_factory=c.client, service_factory=lambda *args: c.service)
    handler = AutomationExecutionHandler(client_factory=factory, registry=ExecutionRegistry([adapter]),
        now_ms=wall_ms, poll_interval_ms=1000)
    runner = SynchronousWorkerRunner(worker_id='responsibility-worker', store=Store(),
        registrations=[handler.registration()], lease_ms=10000, heartbeat_interval_ms=1000, now_ms=wall_ms)
    assert runner.run_once().state == 'suspended'
    saved = repo.get('run', run.id).value
    assert saved['status'] == 'waiting_agent'
    assert saved['steps']['responsibility']['output']['status'] == 'running'
    effect = saved['steps']['responsibility']['effectKey']
    assert view(c)['occurrences'][0]['occurrenceId'] == effect
    assert c.child.dispatched[0][1] == c.principal.to_payload()
    job = factory().query('worker_job.get', {'task_id': run.value['jobId'], 'user_id': owner})
    assert job['status'] == 'suspended' and job['leaseDeadlineMs'] == 0
    settle(c, usage=False)
    wait_until_ms(job['availableAtMs'])
    assert runner.run_once().state == 'suspended'
    assert repo.get('run', run.id).value['status'] == 'waiting_agent'
    assert view(c)['occurrences'][0]['status'] == 'unknown'
    settle(c, usage=True)
    job = factory().query('worker_job.get', {'task_id': run.value['jobId'], 'user_id': owner})
    wait_until_ms(job['availableAtMs'])
    assert runner.run_once().state == 'succeeded'
    assert len(c.child.dispatched) == 1 and view(c)['tokensReserved'] == 100
    assert repo.get('run', run.id).value['steps']['responsibility']['effectKey'] == effect


def test_default_composition_retains_presentation():
    from lib.automations.registry import builtin_registry
    registry = responsibility_registry(client_factory=lambda **kwargs: None)
    for identity in ('task.show_time', 'project.responsibility.run'):
        assert registry.get(builtin_registry().get(identity).document()).contract.id == identity
