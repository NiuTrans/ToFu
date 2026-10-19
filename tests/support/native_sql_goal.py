"""Real accepted Goal/actor fixtures for the Rust selected SQL transaction port.

The account/fee fixtures are also consumed by TofuDB. Every durable row is
created through its semantic owner; actual child processes own native journals.
"""
from contextlib import contextmanager
import copy
import hashlib
import multiprocessing
import os
from pathlib import Path
import time
import uuid

import orjson

from lib.storage_authority_sql.native_checkpoint import NativeSqlCheckpoint
from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_native_checkpoint_storage import acquire, call

EVIDENCE = 1 << 20
ROOT = Path(__file__).resolve().parents[2]


def fixture(name):
    return orjson.loads((ROOT / 'packages/tofu-db/tests/fixtures' / (name + '.json')).read_bytes())


def goal_call(port, operation, payload):
    return port.control(operation, {'format': 'tofu.native-checkpoint-control/v1', operation: payload},
                        deadline_at=time.monotonic()+20, response_bytes=EVIDENCE+4096)


class GoalCase:
    def __init__(self, backend, *, cost_only=False):
        self.backend = backend
        self.client = TransactionClient(backend)
        self.cost_only = cost_only
        self.source = fixture('native_goal_actor_claim')
        if cost_only:
            self.source['invoice']['format'] = 'tofu.native-aggregate-cost/v1'
            for key in ('wallet_user_id', 'reserved_micro'):
                self.source['invoice'].pop(key)
        for owner in range(2, 8):
            account = self.command('tenant.user.create', user_id=f'wallet-{owner}', email=f'{owner}@example.test',
                                   password_hash='fixture', display_name='fixture', role='user', metadata={}, created_at=1000)
            assert account['owner_user_id'] == owner
        self.apply('topup', 200000, 'source-root', reference='payment')
        self.reserve('source-root', 100000)
        source = self.command('turn.create_pair', conversation_id='conversation', command_id='source',
            input_projection={'content': 'Preserve work'}, config={'_goalObjective': 'Preserve work', '_turnPrincipalTenantId': '3'},
            conversation_defaults={'allowCreate': True, 'title': 'Atomic Goal', 'settings': {}})
        self.bind(source['attempt']['attemptId'], 'source-root')
        self.start('source-root')
        launch = self.cursor('source-root')
        self.append('source-root', launch, self.launch('source-root', source['attempt']))
        cursor = launch + 1
        actors = []
        for task in ('pending-a', 'pending-b', 'finished-c'):
            birth = self.command('invocation.register_child', conversation_id='conversation', parent_task_id='source-root', child_task_id=task)
            binding = dict(format='tofu.flow.invocation-completion/v1', nonce='a'*32, identity=birth, account_root_task_id='source-root')
            admission = dict(type='execution_effect_started', key=f'root:agent:{task}:0:0', kind='agent',
                             input_hash='a'*64, invocation_id=task, invocation_completion=binding)
            self.append('source-root', cursor, admission)
            record = self.query('record.get', namespace='task_results', key=task)
            if task == 'finished-c':
                receipt = dict(kind='agent', input_hash='a'*64, invocation_id=task, invocation_completion=binding,
                               execution_account=self.source['cut']['snapshot'], result={'output': 'Preserved completion'})
                record['value']['flow_invocation_completion'] = dict(admission=admission, receipt=receipt, native_fees=self.source['completed_fees'])
                self.command('task_results.checkpoint', key=task, value=record['value'], expected_version=record['version'],
                             guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, require_parent=True)
                record = self.query('record.get', namespace='task_results', key=task)
            actors.append(dict(event_sequence=cursor, registration_version=record['version'],
                               **({'checkpoint_version': 3} if task != 'finished-c' else {})))
            cursor += 1
        account = cursor
        self.append('source-root', cursor, dict(type='execution_account_frontier', launch_id='launch', frontier=self.source['cut']))
        cursor += 1
        self.command('orchestration.run.update_status', run_id='goal_source-root', tenant_id='3', status='error',
                     error={'kind': 'worker_lost', 'source': 'orchestration.startup_recovery'})
        self.command('turn.event.record', attempt_id=source['attempt']['attemptId'], task_id='source-root', terminal=True,
            status='interrupted', slim=False, content='', thinking='checkpoint',
            projection={'content': '', 'thinking': 'checkpoint', 'toolRounds': [], 'segments': []},
            settlement={'outcome': 'interrupted', 'cause': 'server_restart', 'resumeOptions': []},
            error={}, event_type='aborted', event_payload={}, now=1000)
        target = self.command('turn.create_pair', conversation_id='conversation',
            command_id='goal-recovery-' + hashlib.sha256(b'goal_source-root').hexdigest(),
            input_actor='virtual_user', input_kind='continuation', parent_turn_id=source['turn']['turnId'],
            input_projection={'content': 'Continue'}, config={'_goalObjective': 'Preserve work',
                '_turnPrincipalTenantId': '3', '_goalRecoverySourceRunId': 'goal_source-root'})
        self.target = target['attempt']
        self.bind(self.target['attemptId'], 'target-root')
        self.start('target-root')
        self.request = dict(format='tofu.native-goal-actor-claim/v1', tenant_id='3', source_run_id='goal_source-root',
            target_attempt_id=self.target['attemptId'], source_cursor=cursor, launch_sequence=launch,
            account_sequence=account, actors=actors, invoice=self.source['invoice'])

    def command(self, operation, **payload):
        return self.client.command(operation, {'user_id': 7, **payload}, str(uuid.uuid4()))

    def query(self, operation, **payload):
        return self.client.query(operation, {'user_id': 7, **payload})

    def apply(self, kind, amount, root, reference='reserve'):
        return self.command('billing.wallet.apply', user_id='wallet-7', kind=kind, amount_micro=amount,
                            ref_type=reference, ref_id=root, note='Goal fixture', ledger_id=f'{root}-{kind}', occurred_at=1000)

    def reserve(self, root, amount):
        if not self.cost_only:
            self.apply('reserve', -amount, root)
            self.apply('native_admission', 0, root)

    def bind(self, attempt, task):
        assert self.command('turn.attempt.claim', attempt_id=attempt, dispatch_owner_id='test')
        self.command('turn.attempt.bind', attempt_id=attempt, task_id=task, dispatch_owner_id='test')
        assert self.command('turn.attempt.start', attempt_id=attempt, task_id=task)['status'] == 'running'

    def start(self, root):
        return self.command('goal.run.start', run_id='goal_'+root, conversation_id='conversation', objective='Preserve work',
                            tenant_id='3', definition={'nodes': []}, policy=self.source['goal_policy'])

    def cursor(self, root):
        return self.query('orchestration.event.page', run_id='goal_'+root, tenant_id='3', cursor=0)['next_cursor']

    def append(self, root, cursor, event):
        return self.command('orchestration.event.append', run_id='goal_'+root, tenant_id='3', sequence=cursor, event=event)

    def launch(self, root, attempt):
        metadata = dict(format='tofu.goal-execution-launch/v2', run_id='goal_'+root, task_id=root, turn_id=attempt['turnId'],
            attempt_id=attempt['attemptId'], conversation_id='conversation', owner_user_id=7, tenant_id='3',
            launch_id='launch' if root == 'source-root' else 'target-launch', objective='Preserve work', launch={'model': 'alpha'})
        if self.cost_only:
            metadata['fee_account'] = {'mode': 'cost'}
        return dict(type='execution_journal_start', launch_metadata=metadata)

    def claim(self, port, request=None):
        return goal_call(port, 'goal_claim', dict(owner_user_id=7, header_bytes=4096, snapshot_bytes=262144,
                                                evidence_bytes=EVIDENCE, request=request or self.request))

    def prepare_target(self):
        target = fixture('native_goal_actor_handoff')
        cursor = launch = self.cursor('target-root')
        event = self.launch('target-root', self.target)
        event['parent_run_id'] = 'goal_source-root'
        self.append('target-root', cursor, event)
        cursor += 1
        self.append('target-root', cursor, {'type': 'execution_journal_ready'})
        cursor += 1
        actors = []
        for source, task in (('pending-a', 'target-a'), ('pending-b', 'target-b')):
            self.command('invocation.register_child', conversation_id='conversation', parent_task_id='target-root', child_task_id=task)
            record = self.query('record.get', namespace='task_results', key=task)
            actors.append(dict(source_task_id=source, target_task_id=task, target_registration_version=record['version']))
        account = cursor
        self.append('target-root', cursor, dict(type='execution_account_frontier', launch_id='target-launch', frontier=target['before']))
        self.reserve('target-root', 50000)
        authorization = dict(format='tofu.native-goal-actor-handoff/v1', claim_request=self.request, target_launch_sequence=launch,
            target_account_sequence=account, target_cursor=cursor+1, target_reserved_micro=0 if self.cost_only else 50000,
            actors=actors, account_frontiers=target['frontiers'])
        return dict(owner_user_id=7, header_bytes=4096, snapshot_bytes=262144, identity_bytes=1024, evidence_bytes=EVIDENCE,
            source_root_task_id='source-root', target_root_task_id='target-root', authorization=authorization,
            actors=[dict(source_task_id=source, task_id=target, epoch='epoch', binding='binding', token='resume-'+target)
                    for source, target in (('pending-a', 'target-a'), ('pending-b', 'target-b'))])


def _writer(config, ready, states):
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
    from lib.storage_authority_sql.adapters.postgres import PostgresBackend
    backend = (SQLiteBackend if config.backend == 'sqlite' else PostgresBackend)(config)
    backend.start()
    pipeline = None
    if config.logical_shadow_mode != 'off':
        from lib.storage_authority_sql.logical_outbox import LogicalOutboxPipeline
        pipeline = LogicalOutboxPipeline.from_config(config, backend)
        pipeline.start()
    image = Path(os.environ['TOFU_CHECKPOINT_SQL_TEST_LIBRARY'])
    port = NativeSqlCheckpoint(backend=backend, tenant_id=3, owner_user_id=7, document_bytes=EVIDENCE, identity_bytes=1024,
        library=image, sha256=os.environ['TOFU_CHECKPOINT_SQL_TEST_SHA256'], binary_bytes=image.stat().st_size,
        logical_outbox=pipeline)
    try:
        for task, state in states.items():
            cap = acquire(port, task=task)
            call(port, 'load', cap)
            assert call(port, 'begin', cap, round=0)['accepted']
            assert call(port, 'pending', cap, state=state)['accepted']
        ready.send('durable-group')
        ready.recv()
    finally:
        port.close()
        if pipeline is not None:
            pipeline.close()
        backend.close()
        ready.close()


@contextmanager
def writer(case, *, config=None):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_writer, args=(config or case.backend.config, child, copy.deepcopy(case.source['states'])))
    process.start()
    child.close()
    try:
        assert parent.poll(25) and parent.recv() == 'durable-group'
        yield process
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        assert not process.is_alive()
        process.close()
        parent.close()
