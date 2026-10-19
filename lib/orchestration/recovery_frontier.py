"""Reconstruct unfinished agent inputs through the existing Flow interpreter.

inspect_pending_inputs replays verified completed receipts and suspends every
branch at its first unfinished effect. It grants no execution or storage rights;
the caller still owns accepted lineage, native takeover and recipient policies.
There is no second traversal, model loop, durable record or worker pool here.
"""
from copy import deepcopy
from dataclasses import dataclass
import json

from lib.orchestration.continuation import FlowSuspended
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused


@dataclass(frozen=True)
class PendingInvocationInput:
    key: str
    input_hash: str
    node: dict
    context: str
    iteration: int
    worker_context: dict | None


def inspect_pending_inputs(definition, *, initial_context, records, pending_agents,
                           maximum_bytes, max_iterations, worker_context_policy='handoff',
                           loop_budget_policy='bounded', abort_check=None,
                           native_flow_library=None, max_parallel=None, parent_task=None):
    """Return the exact reachable pending set, or refuse without starting effects."""
    if (type(maximum_bytes) is not int or maximum_bytes <= 0
            or not isinstance(pending_agents, dict) or not pending_agents
            or set(records) & set(pending_agents)):
        raise RecoveryRefused('Pending input inspection requires disjoint evidence and explicit bounds')
    pending_agents = deepcopy(pending_agents)
    found, remaining = {}, maximum_bytes
    failures = []

    def refuse_effect(*args, **kwargs):
        raise RecoveryRefused('Pending input inspection cannot execute or publish an effect')

    def observe(*, key, kind, inputs, iteration, input_hash, worker_context):
        # ExecutionJournal holds its tree lock; concurrent branches cannot
        # overspend this retained-evidence allowance or replace a matched key.
        nonlocal remaining
        expected = pending_agents.get(key)
        if (kind != 'agent' or not isinstance(expected, dict) or expected.get('kind') != kind
                or expected.get('input_hash') != input_hash or key in found
                or not isinstance(inputs, list) or len(inputs) != 2
                or not isinstance(inputs[0], dict) or not isinstance(inputs[1], str)
                or type(iteration) is not int):
            raise RecoveryRefused('Pending Flow input differs or reaches an unrecorded effect')
        candidate = PendingInvocationInput(key, input_hash, inputs[0], inputs[1], iteration, worker_context)
        # Check before retaining another detached copy; no complete JSON blob.
        value = dict(key=key, input_hash=input_hash, node=inputs[0], context=inputs[1],
                     iteration=iteration, worker_context=worker_context)
        for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':')).iterencode(value):
            remaining -= len(part.encode('utf-8'))
            if remaining < 0:
                raise RecoveryRefused('Pending Flow input evidence exceeds its byte allowance')
        found[key] = deepcopy(candidate)

    journal = ExecutionJournal(refuse_effect, records=records,
        worker_context_policy=worker_context_policy, unsettled_observer=observe)
    options = {} if max_parallel is None else dict(max_parallel=max_parallel)
    executor = FlowExecutor(definition, agent_runner=refuse_effect,
        parent_task=parent_task,
        subflow_resolver=refuse_effect, execution_journal=journal,
        on_event=lambda event: failures.append(event['type']) if event.get('type') == 'error' else None,
        worker_context_policy=worker_context_policy, max_iterations=max_iterations,
        max_agents=len(records)+len(pending_agents)+1,
        loop_budget_policy=loop_budget_policy, abort_check=abort_check,
        native_flow_library=native_flow_library, **options)
    try:
        executor.run(initial_context=initial_context)
    except FlowSuspended:
        pass
    else:
        raise RecoveryRefused('Flow did not suspend at its complete pending input set')
    if failures or journal.abort_requested() or (abort_check is not None and abort_check()):
        raise RecoveryRefused('Pending Flow input inspection was revoked or cancelled')
    journal.verify_consumed()
    if set(found) != set(pending_agents):
        raise RecoveryRefused('Flow did not reach every original pending agent')
    return found
