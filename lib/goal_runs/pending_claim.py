"""Bind inspected pending Goal actors to the existing atomic native claim port.

claim_pending_goal_actors runs on the caller's owned preparation worker. Original
fee evidence and physical journal positions produce one native request; native
storage revalidates accepted lineage, wallet debt, versions and dead writers.
The returned claim admits journal preparation only, never an actor or tool.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import time

from lib.agent_core.execution_budget import execution_deadline_unix_ms
from lib.goal_runs.contract import goal_run_id_for_task
from lib.orchestration.execution_journal import JournalRecovery, RecoveryRefused, fingerprint
from tofu_db.generated_checkpoint_control import FORMAT, GOAL_CLAIM_FORMAT, RESPONSE_HEADROOM_BYTES


def _seed_digest(seed):
    pending = seed.pending_actors
    value = dict(source_run_id=seed.source_run_id, definition=seed.definition,
        metadata=seed.metadata, records=seed.records, pending=vars(pending) if pending is not None else None)
    digest = hashlib.sha256()
    for part in json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).iterencode(value):
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _task_binding(task, seed, principal):
    from lib.goal_runs.recovery import _execution_limits
    owner = principal.require_owner(context='pending Goal claim')
    metadata = seed.metadata
    if (task.get('_goalRecoverySeed') is not seed or seed.pending_actors is None
            or type(task.get('_userId')) is not int or task['_userId'] != owner
            or metadata['owner_user_id'] != owner
            or str(task.get('_tenant_id') or '') != str(principal.tenant_id or '')
            or metadata['tenant_id'] != str(principal.tenant_id or '')
            or not isinstance(task.get('id'), str) or not task['id']
            or task.get('id') == metadata['task_id']
            or task.get('_goalRunId') != goal_run_id_for_task(task.get('id'))
            or task.get('convId') != metadata['conversation_id']
            or not isinstance(task.get('_attemptId'), str) or not task['_attemptId']
            or (task.get('config') or {}).get('_goalRecoverySourceRunId') != seed.source_run_id
            or (task.get('config') or {}).get('_goalObjective') != metadata['objective']
            or _execution_limits(task.get('config')) != seed.pending_actors.account_frontier['snapshot']['limits']):
        raise RecoveryRefused('Pending Goal claim has no matching accepted task binding')
    if task.get('abort_event') is not None and task['abort_event'].is_set():
        raise RecoveryRefused('Pending Goal claim preparation was cancelled')
    expiry = execution_deadline_unix_ms(task)
    if expiry is not None and expiry <= time.time_ns() // 1000000:
        raise RecoveryRefused('Pending Goal claim execution deadline expired')
    return owner, expiry


def _request(seed, fees, task):
    pending = seed.pending_actors
    positions = pending.positions
    if (not isinstance(positions, dict) or fees.source_run_id != seed.source_run_id
            or fees.source_launch_id != seed.metadata['launch_id']
            or fees.account_frontier != pending.account_frontier
            or set(positions.get('actors', {})) != set(fees.actors)):
        raise RecoveryRefused('Pending Goal claim lacks its complete positioned fee inventory')
    actors = []
    for key, observed in fees.actors.items():
        row = dict(event_sequence=positions['actors'][key], registration_version=observed['registration_version'])
        if key in pending.actors:
            original = pending.actors[key]
            if (observed['admission'] != original['admission']
                    or row['registration_version'] != original['registration_version']
                    or observed.get('checkpoint_version') != original['frontier']['version']):
                raise RecoveryRefused('Pending Goal claim actor evidence changed')
            row['checkpoint_version'] = observed['checkpoint_version']
        if any(type(value) is not int or value < (0 if name == 'event_sequence' else 1) for name, value in row.items()):
            raise RecoveryRefused('Pending Goal claim actor position or version is invalid')
        actors.append(row)
    actors.sort(key=lambda row: row['event_sequence'])
    launch, account, cursor = (positions.get(key) for key in ('launch_sequence','account_sequence','source_cursor'))
    if (any(type(value) is not int for value in (launch, account, cursor))
            or not 0 <= launch < account < cursor
            or any(row['event_sequence'] >= cursor for row in actors)
            or len({row['event_sequence'] for row in actors}) != len(actors)):
        raise RecoveryRefused('Pending Goal claim journal positions are invalid')
    return dict(format=GOAL_CLAIM_FORMAT, tenant_id=seed.metadata['tenant_id'],
        source_run_id=seed.source_run_id, target_attempt_id=task['_attemptId'],
        source_cursor=cursor, launch_sequence=launch, account_sequence=account,
        actors=actors, invoice=deepcopy(fees.invoice))


def _verify_receipt(receipt, request, seed, task):
    body = receipt.get('native_actor_claim') if isinstance(receipt, dict) else None
    rows = body.get('actors') if isinstance(body, dict) else None
    expected = {observed['admission']['invocation_id']: observed['frontier']['version']
                for observed in seed.pending_actors.actors.values()}
    if (not isinstance(body, dict) or receipt.get('type') != 'execution_recovery_claim'
            or receipt.get('successor_run_id') != task['_goalRunId']
            or body.get('format') != GOAL_CLAIM_FORMAT or body.get('request_sha256') != fingerprint(request)
            or body.get('target_attempt_id') != task['_attemptId'] or body.get('source_cursor') != request['source_cursor']
            or type(body.get('source_cursor')) is not int or type(body.get('account_version')) is not int
            or body.get('account_version') != seed.pending_actors.account_frontier['version']
            or body.get('invoice') != request['invoice'] or not isinstance(rows, list) or len(rows) != len(expected)
            or any(not isinstance(row, dict) or row.get('task_id') not in expected
                or type(row.get('source_version')) is not int or row['source_version'] != expected[row['task_id']]
                or type(row.get('version')) is not int or row['version'] != row['source_version'] + 1 for row in rows)
            or len({row['task_id'] for row in rows}) != len(expected)):
        raise RecoveryRefused('Native Goal claim acknowledgement is missing or differs')


@dataclass(frozen=True)
class ClaimedGoalActors:
    source_digest: str
    target_task_id: str
    target_attempt_id: str
    request_digest: str
    request: dict
    receipt: dict
    principal: object = field(repr=False, compare=False)

    def require_matches(self, task, seed):
        _task_binding(task, seed, self.principal)
        if (task['id'] != self.target_task_id or task['_attemptId'] != self.target_attempt_id
                or _seed_digest(seed) != self.source_digest or fingerprint(self.request) != self.request_digest):
            raise RecoveryRefused('Claimed Goal preparation changed its original evidence')
        _verify_receipt(self.receipt, self.request, seed, task)


def claim_pending_goal_actors(task, seed, *, principal, storage, wallet_user_id, reserved_micro,
                              header_bytes, snapshot_bytes, evidence_bytes, io_timeout_ms):
    """Retire the complete source set once; unknown outcomes never auto-retry."""
    from lib.goal_runs.execution_journal import GoalExecutionEvidence
    from lib.goal_runs.pending_fee_evidence import preflight_pending_fees
    owner, expiry = _task_binding(task, seed, principal)
    if (any(type(value) is not int or value <= 0 for value in (header_bytes,snapshot_bytes,evidence_bytes,io_timeout_ms))
            or not callable(getattr(storage,'native_checkpoint_control',None))
            or not callable(getattr(storage,'native_checkpoint_available',None))
            or not storage.native_checkpoint_available(user_id=owner)):
        raise RecoveryRefused('Pending Goal claim requires the selected native authority and explicit bounds')
    attempt = storage.query('turn.attempt.get',dict(attempt_id=task['_attemptId'],user_id=owner,include_execution_config=True))
    accepted = attempt.get('config') if isinstance(attempt, dict) else None
    if (not isinstance(attempt, dict) or attempt.get('taskId') != task['id']
            or attempt.get('turnId') != task.get('_turnId') or attempt.get('conversationId') != task['convId']
            or attempt.get('status') != 'running' or not isinstance(accepted, dict)
            or accepted.get('_goalRecoverySourceRunId') != seed.source_run_id
            or accepted.get('_turnPrincipalTenantId','') != str(principal.tenant_id or '')):
        raise RecoveryRefused('Pending Goal claim accepted executor changed before submission')
    pending = seed.pending_actors
    digest = _seed_digest(seed)
    evidence = GoalExecutionEvidence(seed.metadata, JournalRecovery(seed.records,
        {key:row['admission'] for key,row in pending.actors.items()}),pending.account_frontier,pending.positions)
    fees = preflight_pending_fees(evidence,pending,storage=storage,wallet_user_id=wallet_user_id,reserved_micro=reserved_micro)
    request = _request(seed,fees,task)
    _task_binding(task,seed,principal)
    if _seed_digest(seed) != digest:
        raise RecoveryRefused('Pending Goal claim evidence changed during preparation')
    deadline = time.time_ns() // 1000000 + io_timeout_ms
    if expiry is not None: deadline = min(deadline,expiry)
    response = storage.native_checkpoint_control('goal_claim',dict(format=FORMAT,goal_claim=dict(
        owner_user_id=owner,header_bytes=header_bytes,snapshot_bytes=snapshot_bytes,evidence_bytes=evidence_bytes,request=request)),
        user_id=owner,deadline_unix_ms=deadline,response_bytes=evidence_bytes+RESPONSE_HEADROOM_BYTES)
    receipt = response.get('state') if isinstance(response,dict) and response.get('accepted') is True else None
    _verify_receipt(receipt,request,seed,task)
    return ClaimedGoalActors(digest,task['id'],task['_attemptId'],fingerprint(request),deepcopy(request),deepcopy(receipt),principal)
