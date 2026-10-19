"""One live consumption account for an invocation tree, with optional caps.

Model requests reserve their admitted input estimate and output allowance before
dispatch. Reported usage settles the reservation; unknown consumption fences
finite authorization and unreconciled checkpoints. Unbounded live retries retain
unknown usage without inventing a cap. Provider accounting may exceed estimates. Tool slots
are reserved atomically and never refunded. No application stores.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import lru_cache
import time

from lib.cost import observed_usage_totals
from lib.llm.llm_errors import AbortedError
from lib.agent_core.terminal_delivery import TERMINAL_STATUSES

from lib.log import get_logger

logger = get_logger(__name__)


def tokens(usage: dict | None) -> int:
    return observed_usage_totals(usage)[0]


@lru_cache(maxsize=1)
def _shared_account_validator():
    """Compile the generated checkpoint schema once, only when first used."""
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import SHARED_ACCOUNT_SCHEMA
    Draft202012Validator.check_schema(SHARED_ACCOUNT_SCHEMA)
    return Draft202012Validator(SHARED_ACCOUNT_SCHEMA)


class ExecutionBudget:
    def __init__(self, root: dict):
        limits = (root.get('config') or {}).get('executionLimits')
        from lib.agent_core.execution_limits import validate_execution_limits
        validate_execution_limits(limits)
        self.limits = dict(limits) if limits is not None else {
            'max_tokens': None, 'max_tool_calls': None, 'deadline': None}
        self.root_id = root['id']
        self._lock = threading.RLock()
        self._active = {root['id']: root}
        self._attachment_parents = {}
        self._settled_tokens = tokens(root.get('_swarmChildUsage'))
        self._tool_calls = int(root.get('_executionToolsAdmitted') or 0)
        self._control_tool_calls = {root['id']:self._tool_calls}
        self._condition = threading.Condition(self._lock)
        self._model_leases = {}
        self._model_ordinal = 0
        self._actor_model_ordinals = {root['id']: 0}
        self._actor_tool_ordinals = {root['id']: 0}
        self._model_account_restored = False
        self._restored_descendant_tokens = 0
        self._invoice_prefix_restored = None
        self._model_tokens = {root['id']: tokens(root.get('usage'))}
        self._unknown_usage = root.get('_executionActualsComplete') is False
        self._model_attempt_observer = root.get('_executionModelAttemptObserver')
        self._child_observer = root.get('_executionChildObserver')
        self._child_ordinal = 0
        self._child_records = {}
        self._tool_observer = root.get('_executionToolObserver')
        self._tool_ordinal = 0
        self._tool_attempts = {}
        self._tool_reservation_observer = None
        self._tool_reservation_ordinal = 0
        self._model_allowance_provider = None
        self._replayed_account = None
        self._replayed_actor_frontier = None
        self._pending_actor_prefixes = None
        self._rebound_actor_sources = None
        self._account_frontier_observer = None
        self._actor_rebind_observer = None
        self._actor_rebind_bytes = None
        self._account_frontier_version = 0
        self._account_frontier_failed = False
        self._account_frontier_publishing = False

    def bind_account_frontier_observer(self, observer, *, actor_rebind_observer=None, actor_rebind_bytes=None):
        """Bind whole-account durability before this generation admits effects.

        Restoration may already have installed its verified spent prefix. This
        port adds no execution authority and cannot replace an active observer.
        """
        with self._lock:
            if (not callable(observer) or self._account_frontier_observer is not None
                    or ((actor_rebind_observer is None) != (actor_rebind_bytes is None))
                    or (actor_rebind_observer is not None and (not callable(actor_rebind_observer)
                        or type(actor_rebind_bytes) is not int or actor_rebind_bytes < 1))
                    or self._model_leases or self._tool_attempts or self._child_ordinal
                    or set(self._active) != {self.root_id}
                    or ((self._model_ordinal or self._tool_ordinal or self._tool_calls)
                        and self._replayed_account is None)):
                raise ValueError('Account frontier observation requires a pristine generation')
            self._account_frontier_observer = observer
            self._actor_rebind_observer = actor_rebind_observer
            self._actor_rebind_bytes = actor_rebind_bytes
            self._publish_account_frontier('bound')

    def _publish_account_frontier(self, reason, *, actor_rebinding=None):
        """Caller holds the budget lock; a failed write revokes all admission."""
        if self._account_frontier_observer is None:
            return
        if self._account_frontier_failed or self._account_frontier_publishing:
            raise ValueError('Execution account frontier durability is unavailable')
        from lib.agent_core.account_frontier import capture_frontier
        self._account_frontier_version += 1
        try:
            frontier = capture_frontier(self, version=self._account_frontier_version,
                reason=reason, actor_rebinding=actor_rebinding)
            self._account_frontier_publishing = True
            self._account_frontier_observer(frontier)
        except BaseException:
            self._account_frontier_failed = True
            self._mark_observation_incomplete()
            raise
        finally:
            self._account_frontier_publishing = False

    def capture_replay_snapshot(self, task: dict, *, owner: int) -> dict:
        """Freeze tree totals after a leaf result, including concurrent debt.

        The supplied participant may already have detached. Its registered
        result owner validates birth; only this account reads shared totals.
        An unsettled sample never authorizes restoring a smaller spent prefix.
        """
        from lib.agent_core.budget_snapshot import FORMAT, validate_snapshot
        with self._lock:
            root = self._active.get(self.root_id)
            if (task.get('_executionBudget') is not self or not isinstance(root, dict)
                    or type(owner) is not int or owner < 1
                    or any(type(value.get('_userId')) is not int or value['_userId'] != owner
                           for value in (task, root))):
                raise ValueError('Execution budget snapshot requires its owned account')
            consumed, calls = self.consumption()
            participant_known = task.get('_executionActualsComplete') is not False
            return validate_snapshot(dict(format=FORMAT, owner_user_id=owner,
                root_task_id=self.root_id, tokens=consumed,
                root_tokens=max(tokens(root.get('usage')),
                    self._model_tokens[self.root_id] - self._restored_descendant_tokens),
                tool_calls=calls, model_ordinal=self._model_ordinal, tool_ordinal=self._tool_ordinal,
                complete=participant_known and self.actuals_complete(),
                settled=not self._model_leases and not self._tool_attempts,
                usage_known=participant_known and not self._unknown_usage and all(
                    value.get('_executionActualsComplete') is not False for value in self._active.values()),
                limits=dict(self.limits)))

    def restore_replay_snapshot(self, task: dict, snapshot: dict, *, owner: int) -> None:
        """Restore verified spent consumption once, before new tree admission.

        The caller owns accepted recovery lineage. An identical retry is a
        no-op even after subsequent work; it can never rewind consumption.
        """
        from lib.agent_core.budget_snapshot import validate_snapshot
        snapshot = validate_snapshot(snapshot)
        with self._lock:
            if (task.get('id') != self.root_id or self._active.get(self.root_id) is not task
                    or task.get('_executionBudget') is not self
                    or not self.authorizes_model_owner(task, owner)
                    or snapshot['owner_user_id'] != owner or snapshot['limits'] != self.limits
                    or not snapshot['complete'] or not snapshot['settled'] or not snapshot['usage_known']):
                raise ValueError('Execution budget replay ownership, policy or settlement differs')
            self._restore_spent_prefix(task, snapshot)

    def _restore_spent_prefix(self, task, snapshot):
        """Caller holds the lock and has validated the recovery scope/settlement."""
        from copy import deepcopy
        if self._replayed_account is not None:
            if self._replayed_account != snapshot:
                raise ValueError('Execution budget replay candidate changed')
            return
        displayed, known = observed_usage_totals(task.get('usage'))
        hydrated = (known and task.get('usage') == task.get('_checkpointUsage')
            and displayed in (snapshot['root_tokens'], snapshot['tokens']))
        if (self._model_account_restored or self._model_ordinal or self._model_leases
                or self._child_ordinal or self._tool_ordinal or self._tool_calls or self._tool_attempts
                or self._unknown_usage or set(self._active) != {self.root_id}
                or self._settled_tokens or self._model_tokens[self.root_id] != displayed
                or (displayed and not hydrated)):
            raise ValueError('Execution budget replay requires a pristine owned account')
        frozen_snapshot = deepcopy(snapshot)
        self._model_tokens[self.root_id] = snapshot['tokens']
        self._restored_descendant_tokens = snapshot['tokens'] - displayed
        self._tool_calls = snapshot['tool_calls']
        self._control_tool_calls[self.root_id] = snapshot['tool_calls']
        self._model_ordinal = snapshot['model_ordinal']
        self._tool_ordinal = snapshot['tool_ordinal']
        self._model_account_restored = True
        self._replayed_account = frozen_snapshot

    def restore_pending_frontier(self, task, frontier, *, owner):
        """Restore one verified whole account; pending actors retain their paid debt.

        The application must prove accepted lineage and original native actor
        versions before invoking this port. It grants no lease or effect replay.
        """
        from lib.agent_core.account_recovery import restore_pending_frontier
        restore_pending_frontier(self, task, frontier, owner=owner)

    def rebind_actor_prefix(self, task, *, source_task_id, owner):
        """Move a previously paid local prefix into a pristine child exactly once."""
        from lib.agent_core.account_recovery import rebind_actor_prefix
        rebind_actor_prefix(self, task, source_task_id=source_task_id, owner=owner)

    def validate_actor_recipients(self, bindings, *, owner, expected_observer):
        """Inspect a whole pristine set without moving prefixes or publishing."""
        from lib.agent_core.account_recovery_batch import validate_actor_recipients
        validate_actor_recipients(self, bindings, owner=owner, expected_observer=expected_observer)

    def rebind_all_actor_prefixes(self, bindings, *, owner, expected_observer=None):
        """Acknowledge every pending actor through one prebound atomic sink.

        The sink must commit these ordered cuts together with its native actor
        handoff. This accounting port creates no lease or execution permission.
        """
        from lib.agent_core.account_recovery_batch import rebind_all_actor_prefixes
        rebind_all_actor_prefixes(self, bindings, owner=owner, expected_observer=expected_observer)

    def verify_recovered_actor_account(self, state, *, owner, task, counter_policy):
        """Validate an already acknowledged actor prefix without moving credit."""
        _shared_account_validator().validate(state)
        with self._lock:
            original = self.recovered_actor_prefix_tokens(task, owner=owner)
            if original is None or state != self.capture_actor_account(
                    owner=owner, task=task, counter_policy=counter_policy):
                raise ValueError('Recovered actor checkpoint differs from its acknowledged account')

    def recovered_actor_source(self, task, *, owner):
        """Read the acknowledged account predecessor, independent of tool origins."""
        with self._lock:
            if self.recovered_actor_prefix_tokens(task, owner=owner) is None:
                raise ValueError('Recovered actor has no acknowledged account source')
            return dict(task_id=self._rebound_actor_sources[task['id']],
                        parent_task_id=self._replayed_actor_frontier['snapshot']['root_task_id'])

    def verify_recovered_actor_pending_tools(self, state, *, owner, task, source_task_id, count, counter_policy):
        """Read the original accounted tool prefix without authorizing replay."""
        from lib.agent_core.account_recovery import pending_actor_tools
        _shared_account_validator().validate(state)
        with self._lock:
            return pending_actor_tools(self, state, task=task, owner=owner,
                source_task_id=source_task_id, count=count, counter_policy=counter_policy)

    def reconcile_recovered_actor_tools(self, state, *, owner, task, source_task_id, count):
        """Commit consumption after complete domain proof, preserving paid calls."""
        from lib.agent_core.account_recovery import reconcile_actor_tools
        _shared_account_validator().validate(state)
        with self._lock:
            reconcile_actor_tools(self, state, task=task, owner=owner,
                source_task_id=source_task_id, count=count)

    def refuse_recovered_actor_tools(self, *, owner, task):
        """Unknown recovered effects retain all spend and fence the shared account."""
        with self._lock:
            if self.recovered_actor_prefix_tokens(task, owner=owner) is None:
                raise ValueError('Pending actor refusal requires an acknowledged source')
            self._mark_observation_incomplete(task)
            # Missing domain evidence blocks this recovery regardless of token
            # caps. Reuse the existing continuity fence; cleanup still joins.
            self._account_frontier_failed = True

    def narrow_limits(self, limits):
        """Compose an additional admitted ceiling before this account can spend."""
        from lib.agent_core.execution_limits import validate_execution_limits
        validate_execution_limits(limits)
        with self._lock:
            if (self._model_ordinal or self._tool_calls or self._model_leases or self._tool_attempts
                    or self._child_ordinal or set(self._active) != {self.root_id}
                    or self._model_account_restored or self._replayed_account or self._settled_tokens
                    or any(self._model_tokens.values()) or self._unknown_usage or self._account_frontier_observer):
                raise ValueError('Execution ceilings must be composed before effects')
            self.limits = {key: min(values) if values else None for key in self.limits
                for values in [[value for value in (self.limits[key], limits[key]) if value is not None]]}

    def bind_model_attempt_observer(self, observer, *, child_observer=None, tool_observer=None,
                                    tool_reservation_observer=None, model_allowance_provider=None):
        """Bind durability at composition, before any model admission exists."""
        if not callable(observer):
            raise ValueError('model attempt observer must be callable')
        with self._lock:
            if self._model_ordinal or self._model_attempt_observer is not None:
                raise ValueError('model attempt observation cannot replace an active account port')
            if child_observer is not None:
                if not callable(child_observer) or self._child_ordinal or self._child_observer is not None:
                    raise ValueError('child observation cannot replace an active account port')
            if tool_observer is not None:
                if (not callable(tool_observer) or self._tool_calls or self._tool_ordinal
                        or self._tool_observer is not None):
                    raise ValueError('tool observation cannot replace an active account port')
            if (tool_reservation_observer is not None and (not callable(tool_reservation_observer)
                    or self._tool_calls or self._tool_reservation_observer is not None)
                    or model_allowance_provider is not None and not callable(model_allowance_provider)):
                raise ValueError('Invalid composed execution allowance observer')
            self._model_attempt_observer = observer
            if child_observer is not None:self._child_observer = child_observer
            if tool_observer is not None:self._tool_observer = tool_observer
            self._tool_reservation_observer = tool_reservation_observer
            self._model_allowance_provider = model_allowance_provider

    def restore_model_account(self, *, root_task_id, consumed_tokens,
                              last_attempt_ordinal, usage_complete):
        """Restore a model-only account before admission, without granting replay.

        The durable composition must fence former writers, validate retained
        receipts and bind its observer before calling this port. Unknown usage
        stays unknown; this method does not infer settlement or ownership.
        """
        if (root_task_id != self.root_id
                or type(consumed_tokens) is not int or consumed_tokens < 0
                or type(last_attempt_ordinal) is not int or last_attempt_ordinal < 0
                or type(usage_complete) is not bool):
            raise ValueError('invalid model account recovery values')
        with self._lock:
            if (self._model_account_restored or self._model_ordinal
                    or self._model_leases or self._child_ordinal or self._tool_ordinal
                    or self._tool_calls or self._tool_attempts or self._unknown_usage
                    or set(self._active) != {self.root_id}
                    or self.consumption() != (0, 0)
                    or self.limits['max_tool_calls'] is not None):
                raise ValueError('model account recovery requires a pristine model-only account')
            self._model_tokens[self.root_id] = consumed_tokens
            self._model_ordinal = last_attempt_ordinal
            self._unknown_usage = not usage_complete
            self._model_account_restored = True
            if not usage_complete:
                self._mark_observation_incomplete(self._active[self.root_id])

    def capture_shared_account(self, *, owner: int, task: dict, counter_policy: dict) -> dict:
        """Capture one quiescent invocation root, never another active tree."""
        with self._lock:
            if (task.get('id') != self.root_id or not self.authorizes_model_owner(task, owner)
                    or set(self._active) != {self.root_id} or self._model_leases or self._tool_attempts):
                raise ValueError('shared account checkpoint requires a quiescent owned root')
            snapshot = dict(format='tofu.native-shared-account/v1', owner_user_id=owner,
                root_task_id=self.root_id, tokens=self.consumption()[0], tool_calls=self._tool_calls,
                control_tool_calls=self._control_tool_calls[self.root_id], model_ordinal=self._model_ordinal,
                tool_ordinal=self._tool_ordinal, complete=self.actuals_complete(),
                limits={key:self.limits[key] for key in ('max_tokens','max_tool_calls','deadline')},
                counter_policy=dict(counter_policy))
            root_tokens = max(tokens(task.get('usage')),
                self._model_tokens[self.root_id] - self._restored_descendant_tokens)
            if root_tokens != snapshot['tokens']:
                snapshot['root_tokens'] = root_tokens
            _shared_account_validator().validate(snapshot)
            return snapshot

    def capture_actor_account(self, *, owner: int, task: dict, counter_policy: dict) -> dict:
        """Observe one settled actor inside the existing live shared account.

        Siblings may remain active. This is local consumption evidence, never
        a replacement account, a global quiescence certificate or a restore grant.
        """
        with self._lock:
            root = self._active.get(self.root_id)
            if (task.get('id') == self.root_id or not isinstance(root, dict)
                    or task.get('_executionBudget') is not self
                    or self._active.get(task.get('id')) is not task
                    or not self.authorizes_model_owner(task, owner)
                    or not self._is_live_descendant(task, root)
                    or not self.participant_actuals_complete(task)):
                raise ValueError('actor checkpoint requires an owned settled participant')
            calls = self._control_tool_calls[task['id']]
            snapshot = dict(format='tofu.native-shared-account/v1', owner_user_id=owner,
                root_task_id=self.root_id, actor_task_id=task['id'],
                tokens=self._model_actor_tokens(task), tool_calls=calls, control_tool_calls=calls,
                model_ordinal=self._actor_model_ordinals[task['id']],
                tool_ordinal=self._actor_tool_ordinals[task['id']],
                complete=True, limits=dict(self.limits), counter_policy=dict(counter_policy))
            _shared_account_validator().validate(snapshot)
            return snapshot

    def validate_pending_tool_checkpoint(self, snapshot: dict, *, owner: int, task: dict) -> None:
        """Require exact quiescent quota; observed attempts need their receipt policy."""
        with self._lock:
            capture = self.capture_actor_account if 'actor_task_id' in snapshot else self.capture_shared_account
            if (callable(self._tool_observer) or capture(owner=owner, task=task,
                    counter_policy=snapshot['counter_policy']) != snapshot):
                raise ValueError('pending tools require the exact unobserved quiescent account')

    def reconcile_pending_tools(self, snapshot: dict, *, owner: int, task: dict, count: int) -> None:
        """Restore spent calls proven by an exact fully inspected tool batch.

        The caller holds the recovered checkpoint lease and has checked every
        domain publication receipt. This adds consumption, never execution
        rights; model/fee authority and limits remain the restored originals.
        Observation-enabled tool attempts need their own durable receipt policy.
        """
        with self._lock:
            self.validate_pending_tool_checkpoint(snapshot, owner=owner, task=task)
            if type(count) is not int or count <= 0 or not self._model_account_restored:
                raise ValueError('tool reconciliation requires the exact restored quiescent account')
            cap = self.limits['max_tool_calls']
            if cap is not None and snapshot['tool_calls'] + count > cap:
                raise ValueError('reconciled tools exceed the original tool budget')
            self._tool_calls += count
            self._control_tool_calls[self.root_id] += count
            task['_executionToolsAdmitted'] = self._control_tool_calls[self.root_id]

    def refuse_pending_tool_recovery(self, snapshot: dict, *, owner: int, task: dict) -> None:
        """An unresolved batch leaves consumption incomplete, never free quota."""
        with self._lock:
            self.validate_pending_tool_checkpoint(snapshot, owner=owner, task=task)
            if not self._model_account_restored:
                raise ValueError('pending tool refusal requires the restored account')
            self._mark_observation_incomplete(task)

    def restore_completed_invoice_consumption(self, task: dict, *, root_tokens: int, total_tokens: int) -> None:
        """Increase historical spend once before dispatch; never restore execution rights.

        The application must verify the accepted source and exact durable invoice
        before this port. It restores neither model/tool ordinals nor a journal.
        """
        if (type(root_tokens) is not int or type(total_tokens) is not int
                or not 0 <= root_tokens <= total_tokens):
            raise ValueError('completed invoice token counts invalid')
        with self._lock:
            observed, complete = observed_usage_totals(task.get('_checkpointUsage'))
            if (self._active.get(self.root_id) is not task or task.get('_executionBudget') is not self
                    or not self.authorizes_model_owner(task, task.get('_userId'))
                    or task.get('usage') != task.get('_checkpointUsage') or not complete
                    or observed != root_tokens or self._model_ordinal or self._model_leases
                    or self._child_ordinal or self._tool_ordinal or self._tool_attempts
                    or self._unknown_usage or set(self._active) != {self.root_id}):
                raise ValueError('completed invoice requires the pristine owned root')
            candidate = (root_tokens, total_tokens)
            if self._invoice_prefix_restored is not None:
                if self._invoice_prefix_restored != candidate or self.consumption()[0] != total_tokens:
                    raise ValueError('completed invoice retry changed its consumption')
                return
            if (self._model_account_restored or self._settled_tokens
                    or self._model_tokens[self.root_id] != root_tokens
                    or self.consumption()[0] != root_tokens):
                raise ValueError('completed invoice conflicts with existing account restoration')
            self._model_tokens[self.root_id] = total_tokens
            self._restored_descendant_tokens = total_tokens - root_tokens
            self._model_account_restored = True
            self._invoice_prefix_restored = candidate

    def validate_restored_shared_account(self, *, owner: int, task: dict, snapshot: dict) -> None:
        """Authorize retained fee projection only after this exact root restores."""
        with self._lock:
            if (not self._model_account_restored or self._active.get(self.root_id) is not task
                    or self.capture_shared_account(owner=owner, task=task,
                        counter_policy=snapshot['counter_policy']) != snapshot):
                raise ValueError('retained fees require the exact restored shared account')

    def restore_shared_account(self, snapshot: dict, *, owner: int, task: dict, counter_policy: dict) -> None:
        """Restore spent budget into an unused owned account and matched display prefix.

        The caller must hold the existing journal lease and establish old-writer
        quiescence; this port never grants takeover or infers complete receipts.
        """
        _shared_account_validator().validate(snapshot)
        with self._lock:
            limits={key:self.limits[key] for key in ('max_tokens','max_tool_calls','deadline')}
            display_tokens, display_complete = observed_usage_totals(task.get('usage'))
            root_tokens = snapshot.get('root_tokens', snapshot['tokens'])
            if type(root_tokens) is not int or not 0 <= root_tokens <= snapshot['tokens']:
                raise ValueError('shared account root usage exceeds tree consumption')
            hydrated_prefix = (
                isinstance(task.get('_checkpointUsage'), dict)
                and task.get('usage') == task['_checkpointUsage']
                and display_complete and snapshot['complete']
                and display_tokens == root_tokens
                and self._settled_tokens == 0
                and self._model_tokens.get(self.root_id) in (0, display_tokens)
            )
            consumed = self.consumption()
            if ('actor_task_id' in snapshot
                    or task.get('id') != self.root_id or not self.authorizes_model_owner(task,owner)
                    or snapshot['owner_user_id'] != owner or snapshot['root_task_id'] != self.root_id
                    or snapshot['limits'] != limits or snapshot['counter_policy'] != counter_policy
                    or snapshot['control_tool_calls'] > snapshot['tool_calls']
                    or self._model_account_restored or self._model_ordinal or self._model_leases
                    or self._child_ordinal or self._tool_ordinal or self._tool_calls or self._tool_attempts
                    or self._unknown_usage or set(self._active) != {self.root_id}
                    or (task.get('_checkpointUsage') is not None and not hydrated_prefix)
                    or (consumed != (0,0)
                        and not (hydrated_prefix and consumed == (display_tokens,0)))):
                raise ValueError('shared account recovery requires identical policy and pristine owned root')
            self._model_tokens[self.root_id] = snapshot['tokens']
            self._restored_descendant_tokens = snapshot['tokens'] - root_tokens
            self._model_ordinal = snapshot['model_ordinal']
            self._tool_calls = snapshot['tool_calls']
            self._control_tool_calls[self.root_id] = snapshot['control_tool_calls']
            self._tool_ordinal = snapshot['tool_ordinal']
            self._unknown_usage = not snapshot['complete']
            self._model_account_restored = True
            if not snapshot['complete']:
                self._mark_observation_incomplete(self._active[self.root_id])

    def _observe_model_attempt(self, ordinal, row, phase, reported_tokens, usage_complete):
        if callable(self._model_attempt_observer):
            self._model_attempt_observer({'root_task_id':self.root_id,
                'attempt_ordinal':ordinal, 'attempt_task_id':row['task']['id'],
                'reserved_tokens':row['reserved'], 'phase':phase,
                'reported_tokens':reported_tokens, 'usage_complete':usage_complete,
                'account_observed_tokens':self.consumption()[0]})

    def _observe_child(self, ordinal, parent_id, child_id, phase):
        if callable(self._child_observer):
            self._child_observer({'root_task_id':self.root_id,'child_ordinal':ordinal,
                'parent_task_id':parent_id,'child_task_id':child_id,'phase':phase,
                'pending_model_requests':sum(row['task']['id']==child_id for row in self._model_leases.values()),
                'pending_tool_attempts':sum(value.get('control_task_id',value['task_id'])==child_id
                    for value in self._tool_attempts.values())})

    def _validate_child_parent(self, parent_id):
        parent=self._active.get(parent_id)
        if (parent is None or parent.get('status') in TERMINAL_STATUSES or parent.get('aborted')
                or (parent.get('abort_event') and parent['abort_event'].is_set())):
            raise ModelBudgetExceeded('execution_owner_cancelled')
        limits=(parent.get('config') or {}).get('executionLimits') or self.limits
        if any(deadline is not None and time.time()>=deadline
               for deadline in (self.limits['deadline'],limits.get('deadline'))):
            raise ModelBudgetExceeded('execution_deadline_exceeded')

    def _mark_observation_incomplete(self, task=None):
        self._unknown_usage=True
        if task is not None:task['_executionActualsComplete']=False
        if self.root_id in self._active:self._active[self.root_id]['_executionActualsComplete']=False

    def attach(self, child: dict, *, parent_task_id=None) -> None:
        with self._lock:
            if self._replayed_actor_frontier is not None and child['id'] in self._replayed_actor_frontier['actors']:
                raise ValueError('Recovered actors require fresh recipient identities')
            if child['id'] in self._active:
                raise ValueError('execution budget identity already active')
            parent_id=parent_task_id or self.root_id
            if (self._active.get(parent_id) or {}).get('status') in TERMINAL_STATUSES:
                raise ModelBudgetExceeded('execution_owner_cancelled')
            if callable(self._child_observer):self._validate_child_parent(parent_id)
            self._child_ordinal+=1;ordinal=self._child_ordinal
            try:
                self._observe_child(ordinal,parent_id,child['id'],'attach_intent')
                if callable(self._child_observer):self._validate_child_parent(parent_id)
            except BaseException:
                try:self._observe_child(ordinal,parent_id,child['id'],'attachment_voided')
                except Exception:self._mark_observation_incomplete()
                raise
            self._active[child['id']] = child
            self._attachment_parents[child['id']] = parent_id
            self._model_tokens[child['id']] = tokens(child.get('usage'))
            self._actor_model_ordinals[child['id']] = 0
            self._actor_tool_ordinals[child['id']] = 0
            self._control_tool_calls[child['id']] = int(child.get('_executionToolsAdmitted') or 0)
            if callable(self._child_observer):self._child_records[child['id']]=(ordinal,parent_id)
            # Restored usage/tool slots precede this process and charge once.
            self._tool_calls += int(child.get('_executionToolsAdmitted') or 0)
            if child.get('_executionActualsComplete') is False:self._unknown_usage = True
            self._publish_account_frontier('attached')

    def close(self, child: dict) -> None:
        with self._lock:
            value = self._active.pop(child['id'], None)
            if value is not None:
                if self._rebound_actor_sources is not None:
                    self._rebound_actor_sources.pop(child['id'], None)
                self._attachment_parents.pop(child['id'], None)
                self._actor_model_ordinals.pop(child['id'], None)
                self._actor_tool_ordinals.pop(child['id'], None)
                self._control_tool_calls.pop(child['id'],None)
                self._settled_tokens += max(tokens(value.get('usage')),
                                            self._model_tokens.pop(child['id'], 0))
                record=self._child_records.pop(child['id'],None)
                if record:
                    try:self._observe_child(record[0],record[1],child['id'],'detached')
                    except Exception:self._mark_observation_incomplete(child)
                try:self._publish_account_frontier('detached')
                except Exception:self._mark_observation_incomplete(child)
            self._condition.notify_all()

    def participant_actuals_complete(self, task: dict) -> bool:
        """Certify an exact actor without requiring unrelated tree actors to stop."""
        with self._lock:
            task_id = task.get('id')
            return (self._active.get(task_id) is task
                    and task.get('_executionActualsComplete') is not False
                    and not any(row['task'] is task for row in self._model_leases.values())
                    and not any(row.get('control_task_id', row['task_id']) == task_id
                                for row in self._tool_attempts.values()))

    @contextmanager
    def child_invoice_projection(self, parent: dict, child: dict):
        """Serialize original quote transfer with participant detachment.

        Callers acquire the parent content lock inside this scope and perform
        no I/O. The account lock always precedes the content lock.
        """
        with self._lock:
            if (parent.get('_executionBudget') is not self
                    or child.get('_executionBudget') is not self):
                raise ValueError('native child invoice account differs')
            yield

    def _is_live_descendant(self, child: dict, root: dict) -> bool:
        """Follow captured attachment edges, never mutable task parent hints."""
        current = child.get('id')
        visited = set()
        while current != root.get('id'):
            if current in visited or current not in self._active:
                return False
            visited.add(current)
            actor = self._active[current]
            if actor.get('_userId') != root.get('_userId'):
                return False
            current = self._attachment_parents.get(current)
        return bool(visited)

    def validate_child_invoice_receiver(self, root: dict, parent: dict) -> None:
        """Admit a fee recipient before descendants can dispatch paid requests."""
        with self._lock:
            if (root.get('id') != self.root_id
                    or self._active.get(self.root_id) is not root
                    or self._active.get(parent.get('id')) is not parent
                    or root.get('_executionBudget') is not self
                    or parent.get('_executionBudget') is not self
                    or not self.authorizes_model_owner(parent, root.get('_userId'))
                    or (parent is not root and not self._is_live_descendant(parent, root))):
                raise ValueError('native child fee receiver is not the owned execution root')

    def owned_execution_root(self, participant: dict, *, owner: int) -> dict:
        """Resolve an exact live participant's root for external policy admission."""
        with self._lock:
            root = self._active.get(self.root_id)
            if (root is None or type(owner) is not int or root.get('_userId') != owner
                    or participant.get('_userId') != owner):
                raise ValueError('Execution policy requires its owned live root')
            self.validate_child_invoice_receiver(root, participant)
            self._validate_child_parent(self.root_id)
            self._validate_child_parent(participant['id'])
            return root

    def recovered_actor_prefix_tokens(self, participant: dict, *, owner: int) -> int | None:
        """Read an acknowledged actor rebind's original spend for external fee policy."""
        with self._lock:
            if (type(owner) is not int or participant.get('_userId') != owner
                    or participant.get('_executionBudget') is not self
                    or self._active.get(participant.get('id')) is not participant
                    or not self.authorizes_model_owner(participant, owner)
                    or self._account_frontier_failed or self._account_frontier_publishing):
                raise ValueError('Recovered actor fees require their exact owned participant')
            source = (self._rebound_actor_sources or {}).get(participant['id'])
            if source is None:
                return None
            return self._replayed_actor_frontier['actors'][source]['tokens']

    def settled_child_invoice_tokens(self,parent: dict,child: dict) -> int:
        """Read one exact live child after its terminal settlement, for fee transfer."""
        with self._lock:
            if (parent.get('_executionBudget') is not self or child.get('_executionBudget') is not self
                    or self._active.get(parent.get('id')) is not parent
                    or self._active.get(child.get('id')) is not child
                    or parent.get('id') != self.root_id
                    or child.get('_parentTaskId') != self._attachment_parents.get(child.get('id'))
                    or not self._is_live_descendant(child, parent)
                    or not self.authorizes_model_owner(child,parent.get('_userId'))
                    or child.get('status') not in {'done','error','aborted'}
                    or child.get('_executionActualsComplete') is False
                    or child.get('_terminalPersistencePending')):
                raise ValueError('native child fees require an owned settled participant')
            return max(tokens(child.get('usage')),self._model_tokens.get(child['id'],0))

    def consumption(self) -> tuple[int, int]:
        with self._lock:
            return (self._settled_tokens + sum(max(tokens(t.get('usage')),
                    self._model_tokens.get(t['id'], 0))
                    for t in self._active.values()), self._tool_calls)

    def actuals_complete(self) -> bool:
        """A terminal snapshot cannot certify consumption with live requests.

        Reading does not close leases or turn a pending attempt into zero.
        The durable receiver records the current lower bound; late settlement
        remains owned by the original account.
        """
        with self._lock:
            return (not self._unknown_usage and not self._model_leases and not self._tool_attempts
                    and not self._pending_actor_prefixes
                    and all(task_id == self.root_id for task_id in self._active)
                    and all(task.get('_executionActualsComplete') is not False for task in self._active.values()))

    def validate_child_model_parent(self, task: dict, owner: int) -> None:
        """A declared child route can be issued only by the exact live actor."""
        with self._lock:
            root=self._active.get(self.root_id)
            if (not isinstance(root,dict) or self._active.get(task.get('id')) is not task
                    or task.get('_executionBudget') is not self
                    or not self.authorizes_model_owner(task,owner)
                    or (task is not root and not self._is_live_descendant(task,root))):
                raise ValueError('native child model parent is not an owned live actor')

    def authorizes_model_owner(self, task: dict, owner: int) -> bool:
        """Native account binding reads the same live tree/actor identities."""
        with self._lock:
            participants = (task, self._active.get(self.root_id), self._active.get(task.get('id')))
            return type(owner) is int and owner > 0 and all(
                isinstance(value, dict) and type(value.get('_userId')) is int
                and value['_userId'] == owner for value in participants)

    def consumption_snapshot(self) -> tuple[int, int, bool]:
        """Observe totals and completeness under the same settlement fence."""
        with self._lock:
            consumed_tokens, tool_calls = self.consumption()
            return consumed_tokens, tool_calls, self.actuals_complete()

    def continuity_snapshot(self) -> dict:
        """Observe unfinished tree work atomically; never release or settle it."""
        with self._lock:
            return {'root_task_id':self.root_id,
                    'active_child_task_ids':sorted(task_id for task_id in self._active if task_id!=self.root_id),
                    'pending_model_requests':len(self._model_leases),
                    'model_attempts':[{'attempt_ordinal':ordinal, 'task_id':row['task']['id'],
                        'dispatched':row['dispatched'], 'reserved_tokens':row['reserved']}
                        for ordinal,row in sorted(self._model_leases.items())],
                    'pending_tool_attempts':[dict(value) for _,value in sorted(self._tool_attempts.items())],
                    'unknown_usage':self._unknown_usage}

    def _model_actor_limits(self, task):
        """A detached copy can restrict, never expand, its active owner's slice."""
        owner=self._active.get(task['id']) or task
        scopes=[(value.get('config') or {}).get('executionLimits') or self.limits
                for value in (owner,task)]
        effective={}
        for key in ('max_tokens','max_tool_calls','deadline'):
            values=[scope[key] for scope in scopes if scope[key] is not None]
            effective[key]=min(values) if values else None
        return effective

    def _model_actor_tokens(self, task):
        owner=self._active.get(task['id']) or task
        return max(tokens(task.get('usage')),tokens(owner.get('usage')),
                   self._model_tokens.get(task['id'],0))

    def _model_actor_usage_incomplete(self, task):
        owner=self._active.get(task['id']) or task
        return task.get('_executionActualsComplete') is False or owner.get('_executionActualsComplete') is False

    def _model_actor_has_unreconciled_checkpoint(self, task):
        owner = self._active.get(task['id']) or task
        root = self._active.get(self.root_id, {})
        return any(value.get('_checkpointUsageComplete') is False
                   for value in (task, owner, root))

    def allowance_snapshot(self, task: dict) -> dict:
        """Observe authorization headroom, never reserve, refund or settle it.

        Outstanding leases spend headroom but are not observed consumption.
        The effective slice matches model admission's tree/local intersection;
        tool slots intersect tree and per-task admission counters. Detached carriers may share an
        active identity, but a foreign or closed identity cannot read this port.
        """
        from lib.agent_core.execution_limits import validate_execution_limits
        limits = (task.get('config') or {}).get('executionLimits')
        validate_execution_limits(limits)
        with self._lock:
            task_id = task['id']
            if task_id not in self._active:
                raise ModelBudgetExceeded('execution_owner_cancelled')
            limits = self._model_actor_limits(task)
            used, calls = self.consumption()
            reserved = sum(row['reserved'] for row in self._model_leases.values())
            local_used = self._model_actor_tokens(task)
            local_reserved = sum(row['reserved'] for row in self._model_leases.values()
                                 if row['task']['id'] == task_id)
            caps = [(cap, spent, pending) for cap, spent, pending in (
                (self.limits['max_tokens'], used, reserved),
                (limits['max_tokens'], local_used, local_reserved)) if cap is not None]
            known = (not self._unknown_usage
                     and task.get('_executionActualsComplete') is not False
                     and all(t.get('_executionActualsComplete') is not False
                             for t in self._active.values()))
            headrooms = [max(0, cap - spent - pending) for cap, spent, pending in caps]
            deadlines = [d for d in (self.limits['deadline'], limits['deadline'])
                         if d is not None]
            deadline = min(deadlines) if deadlines else None
            tool_caps = [(cap, admitted) for cap, admitted in (
                (self.limits['max_tool_calls'], calls),
                (limits['max_tool_calls'], self.tool_calls_for_control(task)))
                if cap is not None]
            tool_headrooms = [max(0, cap - admitted) for cap, admitted in tool_caps]
            return {
                'token_bounded': bool(caps),
                'tokens_observed': used,
                'tokens_reserved': reserved,
                'tokens_available': min(headrooms) if caps and known else None,
                'tokens_remaining': min(max(0, cap - spent) for cap, spent, _ in caps)
                    if caps and known else None,
                'token_percent_available': min(
                    remaining * 100 // cap if cap else 0
                    for remaining, (cap, _, _) in zip(headrooms, caps))
                    if caps and known else None,
                'token_percent_remaining': min(
                    max(0, cap - spent) * 100 // cap if cap else 0
                    for cap, spent, _ in caps) if caps and known else None,
                'usage_known': known,
                'actuals_complete': known and self.actuals_complete(),
                'tool_calls_admitted': calls,
                'tool_calls_available': min(tool_headrooms) if tool_caps else None,
                'tool_percent_available': min(
                    remaining * 100 // cap if cap else 0
                    for remaining, (cap, _) in zip(tool_headrooms, tool_caps))
                    if tool_caps else None,
                'seconds_left': int(max(0, deadline - time.time())) if deadline is not None else None,
            }

    def reason(self, *, pending_tools: int = 0) -> str | None:
        with self._lock:
            if (self._account_frontier_failed or self._account_frontier_publishing
                    or (self._pending_actor_prefixes and self._actor_rebind_observer is not None)):
                return 'execution_account_frontier_unavailable'
            used, calls = self.consumption()
            if self.limits['deadline'] is not None and time.time() >= self.limits['deadline']:
                return 'execution_deadline_exceeded'
            if self._unknown_usage and self.limits['max_tokens'] is not None:
                return 'execution_token_usage_unknown'
            if self.limits['max_tokens'] is not None and used >= self.limits['max_tokens']:
                return 'execution_token_budget_exhausted'
            if pending_tools and self.limits['max_tool_calls'] is not None and calls + pending_tools > self.limits['max_tool_calls']:
                return 'execution_tool_budget_exhausted'
            return None

    def tool_calls_for_control(self, task):
        """Read the caller/control owner's charged local slice, not a copy."""
        with self._lock:
            control_id=task.get('_executionBudgetControlTaskId',task['id'])
            return max(int(task.get('_executionToolsAdmitted') or 0),
                       self._control_tool_calls.get(control_id,0))

    def reserve_tool(self, task=None) -> str | None:
        with self._lock:
            reason = self.reason(pending_tools=1)
            actor = task if task is not None else self._active.get(self.root_id)
            control_id = (actor or {}).get('_executionBudgetControlTaskId',(actor or {}).get('id'))
            owner = self._active.get(control_id)
            if reason is None and (owner is None or owner.get('status') in TERMINAL_STATUSES or owner.get('aborted')
                    or (owner.get('abort_event') and owner['abort_event'].is_set())):
                reason = 'execution_owner_cancelled'
            owner_limit=((owner or {}).get('config') or {}).get('executionLimits') or {}
            cap=owner_limit.get('max_tool_calls')
            charged=self._control_tool_calls.get(control_id,0)
            if reason is None and cap is not None and charged>=cap:
                reason='execution_tool_budget_exhausted'
            if reason is None:
                if self._tool_reservation_observer is not None:
                    ordinal = self._tool_reservation_ordinal + 1
                    try:
                        self._tool_reservation_observer({'root_task_id': self.root_id,
                            'task_id': control_id, 'reservation_ordinal': ordinal,
                            'account_tool_calls': self._tool_calls + 1})
                    except ModelBudgetExceeded as error:
                        return error.reason
                    except BaseException:
                        self._mark_observation_incomplete(actor)
                        raise
                    self._tool_reservation_ordinal = ordinal
                self._tool_calls += 1
                self._control_tool_calls[control_id]=charged+1
                self._publish_account_frontier('tool_reserved')
            return reason

    def _validate_tool_actor(self, task):
        self._validate_child_parent(task.get('_executionBudgetControlTaskId',task['id']))
        if task.get('status') in TERMINAL_STATUSES or task.get('aborted') or (task.get('abort_event') and task['abort_event'].is_set()):
            raise ModelBudgetExceeded('execution_owner_cancelled')
        deadline=((task.get('config') or {}).get('executionLimits') or {}).get('deadline')
        if deadline is not None and time.time()>=deadline:
            raise ModelBudgetExceeded('execution_deadline_exceeded')

    def begin_tool_attempt(self, task, tool_call_id, tool_name, round_num, *, scope='executor'):
        """Acknowledge effect-bearing entry; executor quota is charged separately."""
        if not callable(self._tool_observer):return None
        with self._lock:
            self._validate_tool_actor(task)
            self._tool_ordinal+=1
            self._actor_tool_ordinals[task.get('_executionBudgetControlTaskId',task['id'])] = self._tool_ordinal
            event={'tool_ordinal':self._tool_ordinal,'task_id':task['id'],
                   'control_task_id':task.get('_executionBudgetControlTaskId',task['id']),'scope':scope,
                   'tool_call_id':tool_call_id,'tool_name':tool_name,'round':round_num}
            try:
                self._tool_observer({'root_task_id':self.root_id,**event,'phase':'intent',
                    'account_tool_calls':self._tool_calls})
                self._validate_tool_actor(task)
            except BaseException:
                try:self._tool_observer({'root_task_id':self.root_id,**event,'phase':'not_entered',
                    'account_tool_calls':self._tool_calls})
                except Exception:self._mark_observation_incomplete(task)
                raise
            self._tool_attempts[event['tool_ordinal']]=event
            return event

    def finish_tool_attempt(self, event, *, phase, result_sha256=None, hook_errors=None):
        """Release observation state even if terminal durability is unavailable."""
        if event is None:return
        with self._condition:
            value=self._tool_attempts.pop(event['tool_ordinal'],None)
            if value is None:return
            try:
                self._tool_observer({'root_task_id':self.root_id,**value,'phase':phase,
                    'account_tool_calls':self._tool_calls,
                    **({'hook_errors':hook_errors} if hook_errors is not None else {}),
                    **({'result_sha256':result_sha256} if result_sha256 is not None else {})})
            except Exception:self._mark_observation_incomplete()
            self._condition.notify_all()

    def reserve_model(self, task, *, input_tokens, max_output_tokens,
                      minimum_output_tokens=1, cancelled=lambda: False):
        """Wait for live reservations, then admit once under tree/local caps.

        Input counts come from sealed ingress, never model-provided hints.
        Only reported consumption can refund a sent request. A local preparation
        failure before dispatch can void its unsent lease. Waiting uses the existing caller.
        """
        for value in (input_tokens, max_output_tokens, minimum_output_tokens):
            if type(value) is not int or value < 0:
                raise ValueError('model admission requires nonnegative integer counts')
        if minimum_output_tokens < 1:
            raise ValueError('model output minimum must be positive')
        with self._condition:
            while True:
                if (task['id'] not in self._active or task.get('status') in TERMINAL_STATUSES
                        or self._active[task['id']].get('status') in TERMINAL_STATUSES):
                    raise ModelBudgetExceeded('execution_owner_cancelled')
                if cancelled():
                    raise ModelBudgetExceeded('execution_owner_cancelled')
                reason = self.reason()
                limits = self._model_actor_limits(task)
                if limits.get('deadline') is not None and time.time() >= limits['deadline']:
                    reason = 'execution_deadline_exceeded'
                if (self._model_actor_has_unreconciled_checkpoint(task)
                        or (limits.get('max_tokens') is not None
                            and self._model_actor_usage_incomplete(task))):
                    reason = 'execution_token_usage_unknown'
                if reason:
                    raise ModelBudgetExceeded(reason)
                uncapped = (max_output_tokens == 0 and self.limits['max_tokens'] is None
                            and limits.get('max_tokens') is None and self._model_allowance_provider is None)
                if max_output_tokens < minimum_output_tokens and not uncapped:
                    raise ModelBudgetExceeded('execution_token_budget_exhausted')
                used, _ = self.consumption()
                pending = sum(row['reserved'] for row in self._model_leases.values())
                available = max_output_tokens + input_tokens
                if self.limits['max_tokens'] is not None:
                    available = min(available, self.limits['max_tokens'] - used - pending)
                if limits.get('max_tokens') is not None:
                    local_used = self._model_actor_tokens(task)
                    local_pending = sum(row['reserved'] for row in self._model_leases.values()
                                        if row['task']['id'] == task['id'])
                    available = min(available, limits['max_tokens'] - local_used - local_pending)
                if self._model_allowance_provider is not None:
                    external = self._model_allowance_provider()
                    if type(external) is not int or external < 0:
                        raise ValueError('Composed model allowance must be a nonnegative token count')
                    available = min(available, external)
                output = min(max_output_tokens, available - input_tokens)
                if output >= minimum_output_tokens or uncapped:
                    self._model_ordinal += 1
                    ordinal = self._model_ordinal
                    self._actor_model_ordinals[task['id']] = ordinal
                    self._model_leases[ordinal] = {'task': task, 'reserved': input_tokens + output, 'dispatched': False, 'cancelled': cancelled}
                    return ModelBudgetLease(self, ordinal, output, task=task)
                if not self._model_leases:
                    raise ModelBudgetExceeded('execution_token_budget_exhausted')
                self._condition.wait(timeout=0.1)

    def mark_model_dispatched(self, ordinal):
        with self._lock:
            row = self._model_leases.get(ordinal)
            if row is None:
                raise ModelBudgetExceeded('execution_owner_cancelled')
            self._validate_model_dispatch(row)
            if row['dispatched']:
                return
            if not row['dispatched'] and callable(self._model_attempt_observer):
                self._observe_model_attempt(ordinal,row,'dispatch_intent',0,False)
                self._validate_model_dispatch(row)
            row['dispatched'] = True
            self._publish_account_frontier('model_dispatched')

    def _validate_model_dispatch(self,row):
        """Caller holds the account lock; observer latency cannot bypass cancellation."""
        task = row['task']
        if (task['id'] not in self._active or task.get('status') in TERMINAL_STATUSES
                or self._active[task['id']].get('status') in TERMINAL_STATUSES or row['cancelled']()):
            raise ModelBudgetExceeded('execution_owner_cancelled')
        reason = self.reason()
        limits = self._model_actor_limits(task)
        if limits.get('deadline') is not None and time.time() >= limits['deadline']:
            reason = 'execution_deadline_exceeded'
        if (self._model_actor_has_unreconciled_checkpoint(task)
                or (limits.get('max_tokens') is not None
                    and self._model_actor_usage_incomplete(task))):
            reason = 'execution_token_usage_unknown'
        pending = sum(item['reserved'] for item in self._model_leases.values())
        used, _ = self.consumption()
        if self.limits['max_tokens'] is not None and used + pending > self.limits['max_tokens']:
            reason = reason or 'execution_token_budget_exhausted'
        if limits.get('max_tokens') is not None:
            local_pending = sum(item['reserved'] for item in self._model_leases.values()
                                if item['task']['id'] == task['id'])
            local_used = self._model_actor_tokens(task)
            if local_used + local_pending > limits['max_tokens']:
                reason = reason or 'execution_token_budget_exhausted'
        if reason:
            task['_executionLimitReason'] = reason
            raise ModelBudgetExceeded(reason)

    def settle_model(self, ordinal, usage=None, *, unsent=False):
        with self._condition:
            row = self._model_leases.pop(ordinal, None)
            if row is None:
                return
            task = row['task']
            observed, complete = 0, True
            # A caller cannot void an attempt after physical dispatch began.
            if not unsent or row['dispatched']:
                observed, complete = observed_usage_totals(usage)
                if observed:
                    if task['id'] in self._active:
                        self._model_tokens[task['id']] += observed
                        total = self._model_tokens[task['id']]
                    else:
                        self._settled_tokens += observed
                        total = int(task.get('_executionModelActualTokens') or 0) + observed
                    task['_executionModelActualTokens'] = total
                if not complete:
                    self._unknown_usage = True
                    task['_executionActualsComplete'] = False
                    if self.root_id in self._active:
                        self._active[self.root_id]['_executionActualsComplete'] = False
            try:
                self._observe_model_attempt(ordinal,row,
                    'voided' if unsent and not row['dispatched'] else 'settled',observed,complete)
                self._publish_account_frontier('model_settled')
            except Exception:
                # Settlement/teardown must finish even when observation fails.
                # Keep the paid lower bound, but never claim complete durability.
                self._unknown_usage = True
                task['_executionActualsComplete'] = False
                if self.root_id in self._active:
                    self._active[self.root_id]['_executionActualsComplete'] = False
            self._condition.notify_all()


class ModelBudgetExceeded(RuntimeError):
    """Finite model authorization was refused, or owner wait was cancelled."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class ModelBudgetAdmissionDenied(AbortedError):
    """A reserve_model refusal, before this request receives a dispatch lease."""
    def __init__(self, reason, budget):
        self.budget = budget
        super().__init__(reason)

    def proves_complete_usage_for(self, task):
        return (self.budget is task.get('_executionBudget')
                and self.budget.consumption_snapshot()[2])


class ModelBudgetLease:
    """An idempotent per-request settlement port; no transcript is retained."""
    def __init__(self, budget, ordinal, max_output_tokens, *, task=None):
        self.budget = budget
        self.ordinal = ordinal
        self.max_output_tokens = max_output_tokens
        self.dispatched = False
        self.task = task
        self._input_confirmations = []

    def prepare_input(self, wire_body):
        if self.task is not None:
            from lib.tools.registry._spec import prepare_model_input_observers
            self._input_confirmations = prepare_model_input_observers(self.task, wire_body)

    def confirm_input(self, *, successful=False):
        confirmations, self._input_confirmations = self._input_confirmations, []
        if successful and self.dispatched:
            for confirm in confirmations:
                try:
                    confirm()
                except Exception as diagnostic_error:
                    # Missing receipt fails source acceptance closed; it must
                    # not prevent request budget settlement or teardown.
                    logger.debug('confirm_input failure handled: %s', type(diagnostic_error).__name__)
                    pass

    def mark_dispatched(self):
        try:
            self.budget.mark_model_dispatched(self.ordinal)
        except ModelBudgetExceeded as error:
            from lib.llm.llm_errors import AbortedError
            raise AbortedError(error.reason) from error
        self.dispatched = True

    def settle(self, usage=None, *, unsent=False):
        self.budget.settle_model(self.ordinal, usage, unsent=unsent)


def execution_usage_evidence(task: dict) -> dict:
    """Observe completeness without creating an account or settling a lease.

    A missing account is unobserved, even when visible counters are present.
    The shared tree's atomic snapshot includes pending requests and children.
    """
    budget = task.get('_executionBudget')
    if not isinstance(budget, ExecutionBudget):
        return {'complete': False if task.get('_executionActualsComplete') is False else None,
                'observedTokens': None, 'scope': 'unobserved', 'rootTaskId': ''}
    consumed, _, complete = budget.consumption_snapshot()
    return {'complete': complete and task.get('_executionActualsComplete') is not False,
            'observedTokens': consumed, 'scope': 'execution_tree',
            'rootTaskId': budget.root_id}


def _ensure_execution_budget(task):
    """Create the one canonical account, shared by observation and admission."""
    limits = (task.get('config') or {}).get('executionLimits')
    from lib.agent_core.execution_limits import validate_execution_limits
    validate_execution_limits(limits)
    lock = task.setdefault('_executionBudgetLock', threading.Lock())
    with lock:
        budget = task.get('_executionBudget')
        if budget is None:
            budget = ExecutionBudget(task)
            task['_executionBudget'] = budget
    return budget


def execution_deadline_unix_ms(task: dict) -> int | None:
    """Read the earliest task/tree UTC deadline without creating an account.

    Native execution carries this exact expiry across recovery; rounding down
    cannot grant time beyond the canonical Python admission policy.
    """
    from lib.agent_core.execution_limits import validate_execution_limits
    import math
    limits = (task.get('config') or {}).get('executionLimits')
    validate_execution_limits(limits)
    budget = task.get('_executionBudget')
    if budget is not None and not isinstance(budget, ExecutionBudget):
        raise ValueError('execution deadline requires the canonical account')
    tree_limits = budget.limits if budget is not None else None
    validate_execution_limits(tree_limits)
    deadlines = [source['deadline'] for source in (limits, tree_limits)
                 if source is not None and source['deadline'] is not None]
    if not deadlines:
        return None
    milliseconds = min(deadlines) * 1000
    if not math.isfinite(milliseconds) or not 0 <= milliseconds <= 18446744073709551615:
        raise ValueError('execution deadline exceeds native timestamp bounds')
    return math.floor(milliseconds)


def execution_allowance_snapshot(task: dict) -> dict | None:
    """Finite invocation observation; unbounded work gets no notice/account."""
    budget = task.get('_executionBudget')
    limits = (task.get('config') or {}).get('executionLimits')
    from lib.agent_core.execution_limits import validate_execution_limits
    validate_execution_limits(limits)
    tree_limits = budget.limits if budget is not None else {}
    if not any(value is not None for source in (limits or {}, tree_limits)
               for key, value in source.items()
               if key in ('max_tokens', 'max_tool_calls', 'deadline')):
        return None
    return _ensure_execution_budget(task).allowance_snapshot(task)


def model_budget_admission(task, *, cancelled=None):
    """Observe each physical request, enforcing only the account's actual limits.

    Ordinary unbounded tasks use the same settlement port as finite work. The
    existing account policy permits live unbounded retries after unknown usage,
    keeps their evidence incomplete, and fences unreconciled restored checkpoints.
    """
    budget = _ensure_execution_budget(task)

    def admit(**counts):
        try:
            return budget.reserve_model(task, **counts,
                cancelled=cancelled or (lambda: bool(task.get('aborted') or
                    (task.get('abort_event') and task['abort_event'].is_set()))))
        except ModelBudgetExceeded as error:
            if error.reason != 'execution_owner_cancelled':
                task['_executionLimitReason'] = error.reason
                raise ModelBudgetAdmissionDenied(error.reason, budget) from error
            raise AbortedError(error.reason) from error
    return admit


def share_execution_budget(parent: dict, carrier: dict) -> None:
    """Copy the control port to a detached carrier, without counting it as work."""
    limits = (parent.get('config') or {}).get('executionLimits')
    budget = parent.get('_executionBudget')
    if budget is None and limits is None:
        return
    budget = _ensure_execution_budget(parent)
    carrier['config']['executionLimits'] = dict(limits or budget.limits)
    carrier['_executionBudget'] = budget
    carrier['_executionBudgetControlTaskId'] = parent.get('_executionBudgetControlTaskId',parent['id'])


def inherit_execution_budget(parent: dict, child: dict) -> None:
    """Attach both Flow and Swarm descendants to the same parent allowance."""
    share_execution_budget(parent, child)
    budget = child.get('_executionBudget')
    if budget is not None:
        budget.attach(child,parent_task_id=parent.get('_executionBudgetControlTaskId',parent['id']))
        child['_executionBudgetControlTaskId']=child['id']


def close_execution_budget(child: dict) -> None:
    budget = child.get('_executionBudget')
    if budget is not None:
        budget.close(child)
