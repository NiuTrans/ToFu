"""One live consumption account for a bounded invocation tree.

Model requests reserve their admitted input estimate and output allowance before
dispatch. Reported usage settles the reservation; unknown consumption fences
future admission. Provider accounting may exceed local estimates. Tool slots
are reserved atomically and never refunded. No application stores.
"""
from __future__ import annotations

import threading
import time

from lib.cost import observed_usage_totals
from lib.llm_errors import AbortedError
from lib.agent_core.terminal_delivery import TERMINAL_STATUSES


def tokens(usage: dict | None) -> int:
    return observed_usage_totals(usage)[0]


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
        self._settled_tokens = tokens(root.get('_swarmChildUsage'))
        self._tool_calls = int(root.get('_executionToolsAdmitted') or 0)
        self._control_tool_calls = {root['id']:self._tool_calls}
        self._condition = threading.Condition(self._lock)
        self._model_leases = {}
        self._model_ordinal = 0
        self._model_account_restored = False
        self._model_tokens = {root['id']: tokens(root.get('usage'))}
        self._unknown_usage = False
        self._model_attempt_observer = root.get('_executionModelAttemptObserver')
        self._child_observer = root.get('_executionChildObserver')
        self._child_ordinal = 0
        self._child_records = {}
        self._tool_observer = root.get('_executionToolObserver')
        self._tool_ordinal = 0
        self._tool_attempts = {}

    def bind_model_attempt_observer(self, observer, *, child_observer=None, tool_observer=None):
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
            self._model_attempt_observer = observer
            if child_observer is not None:self._child_observer = child_observer
            if tool_observer is not None:self._tool_observer = tool_observer

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
            from lib.agent_core.native_content_contract import SHARED_ACCOUNT_SCHEMA
            import jsonschema
            jsonschema.validate(snapshot, SHARED_ACCOUNT_SCHEMA)
            return snapshot

    def restore_shared_account(self, snapshot: dict, *, owner: int, task: dict, counter_policy: dict) -> None:
        """Restore all spent budget only into a pristine, owned root account.

        The caller must hold the existing journal lease and establish old-writer
        quiescence; this port never grants takeover or infers complete receipts.
        """
        from lib.agent_core.native_content_contract import SHARED_ACCOUNT_SCHEMA
        import jsonschema
        jsonschema.validate(snapshot, SHARED_ACCOUNT_SCHEMA)
        with self._lock:
            limits={key:self.limits[key] for key in ('max_tokens','max_tool_calls','deadline')}
            if (task.get('id') != self.root_id or not self.authorizes_model_owner(task,owner)
                    or snapshot['owner_user_id'] != owner or snapshot['root_task_id'] != self.root_id
                    or snapshot['limits'] != limits or snapshot['counter_policy'] != counter_policy
                    or snapshot['control_tool_calls'] > snapshot['tool_calls']
                    or self._model_account_restored or self._model_ordinal or self._model_leases
                    or self._child_ordinal or self._tool_ordinal or self._tool_calls or self._tool_attempts
                    or self._unknown_usage or set(self._active) != {self.root_id}
                    or self.consumption() != (0,0)):
                raise ValueError('shared account recovery requires identical policy and pristine owned root')
            self._model_tokens[self.root_id] = snapshot['tokens']
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
            self._model_tokens[child['id']] = tokens(child.get('usage'))
            self._control_tool_calls[child['id']] = int(child.get('_executionToolsAdmitted') or 0)
            if callable(self._child_observer):self._child_records[child['id']]=(ordinal,parent_id)
            # Restored usage/tool slots precede this process and charge once.
            self._tool_calls += int(child.get('_executionToolsAdmitted') or 0)
            if child.get('_executionActualsComplete') is False:self._unknown_usage = True

    def close(self, child: dict) -> None:
        with self._lock:
            value = self._active.pop(child['id'], None)
            if value is not None:
                self._control_tool_calls.pop(child['id'],None)
                self._settled_tokens += max(tokens(value.get('usage')),
                                            self._model_tokens.pop(child['id'], 0))
                record=self._child_records.pop(child['id'],None)
                if record:
                    try:self._observe_child(record[0],record[1],child['id'],'detached')
                    except Exception:self._mark_observation_incomplete(child)
            self._condition.notify_all()

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
                    and all(task_id == self.root_id for task_id in self._active))

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
                self._tool_calls += 1
                self._control_tool_calls[control_id]=charged+1
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
                if limits.get('max_tokens') is not None and self._model_actor_usage_incomplete(task):
                    reason = 'execution_token_usage_unknown'
                if reason:
                    raise ModelBudgetExceeded(reason)
                uncapped = (max_output_tokens == 0 and self.limits['max_tokens'] is None
                            and limits.get('max_tokens') is None)
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
                output = min(max_output_tokens, available - input_tokens)
                if output >= minimum_output_tokens or uncapped:
                    self._model_ordinal += 1
                    ordinal = self._model_ordinal
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
            if not row['dispatched'] and callable(self._model_attempt_observer):
                self._observe_model_attempt(ordinal,row,'dispatch_intent',0,False)
                self._validate_model_dispatch(row)
            row['dispatched'] = True

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
        if limits.get('max_tokens') is not None and self._model_actor_usage_incomplete(task):
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
                except Exception:
                    # Missing receipt fails source acceptance closed; it must
                    # not prevent request budget settlement or teardown.
                    pass

    def mark_dispatched(self):
        try:
            self.budget.mark_model_dispatched(self.ordinal)
        except ModelBudgetExceeded as error:
            from lib.llm_errors import AbortedError
            raise AbortedError(error.reason) from error
        self.dispatched = True

    def settle(self, usage=None, *, unsent=False):
        self.budget.settle_model(self.ordinal, usage, unsent=unsent)


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
    """Bind finite input/output authorization to the task's shared tree account."""
    limits = (task.get('config') or {}).get('executionLimits')
    budget = task.get('_executionBudget')
    evolution = (task.get('config') or {}).get('evolution') or {}
    if limits is None and budget is None and not evolution:
        return None
    if isinstance(limits, dict) and limits.get('max_tokens') is None and budget is None and not evolution:
        return None
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
