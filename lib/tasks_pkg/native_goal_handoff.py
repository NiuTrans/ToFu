"""Compose the existing budget batch sink with native whole-set capabilities.

Entry: NativeGoalHandoff. The caller binds commit_frontiers before attaching
children, then consumes on its owned executor. Goal/native storage proves lineage
and billing; existing child checkpoint policies own load/reconcile/close. This
adapter starts no actor, worker or retry loop and does not authorize tool replay.
"""
from copy import deepcopy
from contextlib import asynccontextmanager
from contextvars import copy_context
from concurrent.futures import Executor
from functools import partial
import asyncio
import time
import json

from lib.agent_core.execution_budget import ExecutionBudget
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from lib.orchestration.execution_journal import ExecutionJournal
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES, GOAL_HANDOFF_ROW_HEADROOM_BYTES


class NativeGoalHandoff:
    def __init__(self, *, principal, task, storage, evidence_bytes, io_timeout_ms, journal=None):
        self.owner = principal.require_owner(context='whole Goal actor handoff')
        self.tenant_id = str(principal.tenant_id or '')
        budget = task.get('_executionBudget')
        if (not isinstance(budget, ExecutionBudget) or task.get('id') != budget.root_id
                or not budget.authorizes_model_owner(task, self.owner)
                or (journal is not None and not isinstance(journal, ExecutionJournal))
                or any(type(value) is not int or value <= 0 for value in (evidence_bytes, io_timeout_ms))
                or not callable(getattr(storage, 'native_checkpoint_control', None))
                or not callable(getattr(storage, 'native_checkpoint_available', None))
                or not storage.native_checkpoint_available(user_id=self.owner)):
            raise ValueError('whole actor handoff requires its owned selected account and storage')
        self.task, self.budget, self.storage = task, budget, storage
        self.evidence_bytes, self.io_timeout_ms = evidence_bytes, io_timeout_ms
        self.journal = journal
        self._prepared = None
        self._capabilities = None
        self._policies = None
        self._started = False
        self._preparation_started = False
        self._recovery_claim = None
        self._effect_origins = {}

    def bind_recovery_claim(self, claim, seed):
        """Retain exact inspected effect origins covered by the accepted claim."""
        if self._recovery_claim is not None or self._started or self._preparation_started:
            raise ValueError('Goal handoff recovery claim is already bound')
        claim.require_matches(self.task, seed)
        self._recovery_claim = (claim, seed)
        self._effect_origins = {row['admission']['invocation_id']: deepcopy(row['effect_origin'])
            for row in seed.pending_actors.actors.values() if row.get('effect_origin')}

    @asynccontextmanager
    async def prepare_recipients(self, bindings, *, authorization, executor):
        """Own asynchronous readiness of the complete, already prepared child set.

        Bind the yielded scope to each child's NativePreparedInvocation before
        executing it normally. Its genuine ROOT request enters before factory
        construction. This scope owns waits and joins; the ordinary invocation
        still owns policy closure, model/tool execution and terminal settlement.
        """
        from lib.agent_core.abort_signal import AbortSignal
        from lib.agent_core.native_process import _joined
        from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest

        if (self._preparation_started or self._started or not isinstance(executor, Executor)
                or not isinstance(bindings, (list, tuple)) or not bindings):
            raise ValueError('Goal recipient preparation requires one complete owned set')
        recipients, sources = {}, set()
        for task, source in bindings:
            if (not isinstance(task, dict) or not isinstance(task.get('id'), str)
                    or task['id'] in recipients or not isinstance(source, str) or not source
                    or source in sources or task.get('_executionBudget') is not self.budget
                    or task.get('_parentTaskId') != self.task['id']
                    or not self.budget.authorizes_model_owner(task, self.owner)
                    or not isinstance(task.get('abort_event'), AbortSignal)):
                raise ValueError('Goal recipient preparation identity differs')
            recipients[task['id']] = (task, source)
            sources.add(source)
        self.budget.validate_actor_recipients(bindings, owner=self.owner,
            expected_observer=self.commit_frontiers)
        if not isinstance(self.task.get('abort_event'), AbortSignal):
            raise ValueError('Goal recipient preparation requires the root abort owner')
        authorization = deepcopy(authorization)
        recovery_epochs = {}
        if self._recovery_claim is not None:
            _, seed = self._recovery_claim
            recovery_epochs = {row['admission']['invocation_id']:
                (row['frontier']['epoch'], row['frontier']['binding'])
                for row in seed.pending_actors.actors.values()}
        self._preparation_started = True
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
        entered, active, policies, subscriptions = set(), set(), {}, []
        failure = work = None
        acknowledged = closed = False

        def stop_listening():
            for listener in subscriptions:
                listener.future.remove_done_callback(aborted)
                listener.close()
            subscriptions.clear()

        def fail(error):
            nonlocal failure
            if acknowledged or closed:
                return
            failure = failure or error
            for task, _ in recipients.values():
                task['abort_event'].set()
            if not completed.done():
                completed.set_result(None)

        def aborted(_):
            fail(asyncio.CancelledError())

        def settled(future):
            nonlocal acknowledged
            try:
                future.result()
            except BaseException as error:
                fail(error)
            else:
                if self.task['abort_event'].is_set() or any(
                        task['abort_event'].is_set() for task, _ in recipients.values()):
                    fail(asyncio.CancelledError())
                if failure is None:
                    acknowledged = True
                    stop_listening()
                    if not completed.done():
                        completed.set_result(None)

        @asynccontextmanager
        async def prepare(request):
            nonlocal work
            if asyncio.get_running_loop() is not loop:
                raise ValueError('Goal checkpoint preparation belongs to another event loop')
            execution = asyncio.current_task()
            task = request.task if isinstance(request, RootLoopRequest) else None
            identity = task.get('id') if isinstance(task, dict) else None
            if (closed or identity not in recipients
                    or recipients[identity][0] is not task or identity in entered):
                error = ValueError('Goal checkpoint preparation is closed, foreign or already entered')
                fail(error)
                raise error
            entered.add(identity)
            active.add(execution)

            async def ready(policy):
                nonlocal work
                try:
                    if (failure is not None or task['abort_event'].is_set()
                            or self.task['abort_event'].is_set()):
                        raise failure if failure is not None else asyncio.CancelledError()
                    if (not isinstance(policy, NativeCheckpointPolicy) or policy.task is not task
                            or policy.executor is not executor or identity in policies
                            or policy.storage is not self.storage or not policy.registered_child
                            or policy.owner != self.owner or policy.closed or policy.creation_started
                            or policy.capability is not None):
                        raise ValueError('Goal checkpoint readiness requires its pristine owned policy')
                    if recovery_epochs:
                        source = recipients[identity][1]
                        epoch, binding = recovery_epochs[source]
                        policy.prepare_recovered_epoch(epoch=epoch, binding=binding)
                    policies[identity] = policy
                    if len(policies) == len(recipients):
                        pairs = [(policies[key], source) for key, (_, source) in recipients.items()]
                        work = loop.run_in_executor(executor, copy_context().run,
                            partial(self.consume, pairs, authorization=authorization))
                        work.add_done_callback(settled)
                    await asyncio.shield(completed)
                    if failure is not None:
                        raise failure
                    if task['abort_event'].is_set() or self.task['abort_event'].is_set():
                        raise asyncio.CancelledError()
                except BaseException as error:
                    fail(error)
                    raise
                finally:
                    # Policy closure follows this callback. Even cancellation
                    # must wait for an in-flight handoff and capability adoption.
                    if work is not None:
                        import sys
                        primary = sys.exception()
                        try:
                            await _joined(work)
                        except BaseException as error:
                            if primary is None:
                                raise
                            primary.add_note('Goal handoff join failed: '+type(error).__name__)

            try:
                if failure is not None:
                    raise failure
                yield ready
                if identity not in policies:
                    raise ValueError('Goal recipient exited without checkpoint readiness')
            except BaseException as error:
                fail(error)
                raise
            finally:
                active.discard(execution)

        try:
            for task in (self.task, *(task for task, _ in recipients.values())):
                listener = task['abort_event'].subscribe_async()
                subscriptions.append(listener)
                listener.future.add_done_callback(aborted)
            yield prepare
            if not acknowledged:
                raise failure if failure is not None else ValueError('Goal recipient set was not acknowledged')
        finally:
            import sys
            primary = sys.exception()
            if not acknowledged:
                fail(primary or ValueError('Goal recipient preparation scope closed'))
            closed = True
            stop_listening()
            # No new worker/task is created. Join invocations already entered
            # through this scope, including a peer whose factory is still busy.
            for execution in active:
                if execution is not asyncio.current_task():
                    for task, _ in recipients.values():
                        task['abort_event'].set()
                    break
            waits = [execution for execution in active if execution is not asyncio.current_task()]
            if work is not None:
                waits.append(work)
            try:
                if waits:
                    joining = asyncio.gather(*waits, return_exceptions=True)
                    try:
                        outcomes = await asyncio.shield(joining)
                    except asyncio.CancelledError as cancelled:
                        outcomes = await _joined(joining)
                        if primary is None:
                            for outcome in outcomes:
                                if isinstance(outcome, BaseException):
                                    cancelled.add_note('Joined Goal invocation ended with '+type(outcome).__name__)
                            raise
                        primary.add_note('Goal recipient cleanup was interrupted; all entered invocations joined')
                    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)
                              and not isinstance(outcome, asyncio.CancelledError) and outcome is not primary]
                    if errors:
                        if primary is None:
                            raise BaseExceptionGroup('Goal recipient closure failed', errors)
                        primary.add_note('Joined Goal invocation cleanup also failed: '+
                                         ', '.join(type(error).__name__ for error in errors))
            finally:
                policies.clear()
                recipients.clear()
                entered.clear()
                authorization.clear()
                recovery_epochs.clear()
                failure = work = None

    def consume(self, bindings, *, authorization):
        """Move the complete set, then bind its acknowledged existing policies."""
        if self._started or not isinstance(bindings, (list, tuple)) or not bindings:
            raise ValueError('whole actor handoff consumption is one-shot')
        if self._recovery_claim is not None:
            claim, seed = self._recovery_claim
            claim.require_matches(self.task, seed)
            if authorization['claim_request'] != claim.request:
                raise ValueError('Goal handoff changed its acknowledged recovery claim')
        policies, sources = [], []
        for policy, source in bindings:
            if (not isinstance(policy, NativeCheckpointPolicy) or not policy.registered_child
                    or policy.owner != self.owner or policy.storage is not self.storage
                    or policy.task.get('_executionBudget') is not self.budget
                    or policy.child_identity['parent_task_id'] != self.task['id']
                    or policy.closed or policy.creation_started or policy.capability is not None
                    or not isinstance(source, str) or not source):
                raise ValueError('whole actor handoff requires pristine registered recipient policies')
            policy._verify_child(self.storage)
            policies.append(policy); sources.append(source)
        if len(set(map(id, policies))) != len(policies) or len(set(sources)) != len(sources):
            raise ValueError('whole actor handoff recipients must be unique')
        # Every bound must agree; no target silently borrows another's allowance.
        bounds = {key: policies[0].parameters[key] for key in ('header_bytes', 'snapshot_bytes', 'identity_bytes')}
        if any(any(policy.parameters[key] != value for key, value in bounds.items()) for policy in policies):
            raise ValueError('whole actor handoff checkpoint bounds differ')
        source_root = authorization['claim_request']['invoice']['task_id']
        self._prepared = dict(owner_user_id=self.owner, **bounds, evidence_bytes=self.evidence_bytes,
            source_root_task_id=source_root, target_root_task_id=self.task['id'], authorization=deepcopy(authorization),
            actors=[dict(source_task_id=source, **{key: policy.parameters[key] for key in ('task_id', 'epoch', 'binding', 'token')})
                    for policy, source in zip(policies, sources)])
        self._started = True
        self._policies = tuple(policies)
        try:
            self.budget.rebind_all_actor_prefixes([(policy.task, source) for policy, source in zip(policies, sources)], owner=self.owner, expected_observer=self.commit_frontiers)
            if self._capabilities is None:
                raise ValueError('whole actor handoff sink was not acknowledged')
            # Policies have not been dispatched. Their ordinary load method
            # consumes these handles and retains all existing recovery checks.
            for policy, capability, source in zip(policies, self._capabilities, sources):
                origin = self._effect_origins.get(source)
                if origin is None:
                    policy.adopt_recovered_capability(capability)
                else:
                    policy.adopt_recovered_capability(capability, effect_origin=origin)
            self._capabilities = None
            self._policies = None
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup('whole actor handoff and capability cleanup failed', [error, cleanup]) from None
            raise
        finally:
            self._prepared = None
            self._recovery_claim = None
            self._effect_origins.clear()

    def close(self):
        """Forget unadopted handles; adopted policies retain ordinary ownership.

        Forgetting is volatile registry cleanup and never releases a durable
        pending journal. Failed cleanup remains available to an explicit retry.
        """
        if self._capabilities is None:
            self._policies = None
            return
        remaining = list(zip(self._policies, self._capabilities))
        failures = []
        for policy, capability in tuple(remaining):
            if policy.capability == capability:
                remaining.remove((policy, capability))
                continue
            try:
                response = self.storage.native_checkpoint_control('forget',
                    dict(format=FORMAT, capability_id=capability), user_id=self.owner,
                    deadline_unix_ms=time.time_ns() // 1000000 + self.io_timeout_ms,
                    response_bytes=policy.response_bytes)
                if response.get('accepted') is not True:
                    raise ValueError('unadopted native capability cleanup refused')
                remaining.remove((policy, capability))
            except BaseException as error:
                failures.append(error)
        self._policies = tuple(policy for policy, _ in remaining) or None
        self._capabilities = tuple(capability for _, capability in remaining) or None
        if failures:
            raise BaseExceptionGroup('unadopted native capability cleanup failed', failures)

    def commit_frontiers(self, cuts):
        """Prebound ExecutionBudget callback; no capability escapes before ACK."""
        if self._prepared is None or self._capabilities is not None:
            raise ValueError('whole actor handoff has no prepared durable batch')
        if self.journal is not None:
            self.journal.commit_account_handoff(cuts, lambda positions: self._commit_frontiers(cuts, positions),
                                                tenant_id=self.tenant_id)
        else:
            self._commit_frontiers(cuts)

    def _commit_frontiers(self, cuts, positions=None):
        payload = deepcopy(self._prepared)
        if positions is not None:
            for key, value in positions.items():
                if key in payload['authorization'] and (type(payload['authorization'][key]) is not int
                        or payload['authorization'][key] != value):
                    raise ValueError('whole actor handoff journal position changed')
            payload['authorization'].update(positions)
        payload['authorization']['account_frontiers'] = list(cuts)
        response = self.storage.native_checkpoint_control('goal_handoff',
            dict(format=FORMAT, goal_handoff=payload), user_id=self.owner,
            deadline_unix_ms=time.time_ns() // 1000000 + self.io_timeout_ms,
            response_bytes=RESPONSE_HEADROOM_BYTES + sum(GOAL_HANDOFF_ROW_HEADROOM_BYTES
                + len(json.dumps(actor['task_id'], ensure_ascii=False).encode('utf-8')) for actor in payload['actors']))
        state = response.get('state') if isinstance(response, dict) and response.get('accepted') is True else None
        rows = state.get('leases', []) if isinstance(state, dict) else []
        if (not isinstance(rows, list) or len(rows) != len(payload['actors'])
                or any(not isinstance(row, dict) or set(row) != {'task_id', 'capability_id'}
                    or row['task_id'] != actor['task_id'] or type(row['capability_id']) is not int or row['capability_id'] <= 0
                    for row, actor in zip(rows, payload['actors']))
                or len({row['capability_id'] for row in rows}) != len(rows)):
            raise ValueError('native whole actor handoff refused or returned incomplete capabilities')
        self._capabilities = tuple(row['capability_id'] for row in rows)
