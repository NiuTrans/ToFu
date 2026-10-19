"""Durable effect receipts for replaying a Flow without repeating completed work.

The runtime supplies an awaited durable event sink. Engine adapters journal
only nondeterministic boundaries (agents, human decisions, referenced graphs).
Control flow and its ledgers are reconstructed by the existing interpreter.
An unresolved agent receipt is ambiguous and is never retried automatically.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import threading

from lib.orchestration.graph import FlowEventDeliveryError
from lib.orchestration.events import EXECUTION_JOURNAL_EVENT_TYPES
from lib.orchestration.runner_result import (
    OrchestrationAgentResult, OrchestrationModelRoute,
    normalize_orchestration_agent_result,
)
from lib.orchestration.tool_usage import OrchestrationToolUsage

JOURNAL_VERSION = 1
JOURNAL_EVENTS = EXECUTION_JOURNAL_EVENT_TYPES


class RecoveryDeferred(FlowEventDeliveryError):
    """An accepted executor still owns preparation; preserve pending election."""


class RecoveryRefused(FlowEventDeliveryError):
    """Missing/ambiguous evidence prevents a safe execution replay."""


@dataclass(frozen=True)
class AgentExecutionAdmission:
    """One journal occurrence and its ordinary awaited dispatch admission."""
    key: str
    input_hash: str
    journal: object = field(repr=False, compare=False)
    _admit: object = field(repr=False, compare=False)

    def __call__(self, **kwargs):
        return self._admit(**kwargs)


@dataclass(frozen=True)
class JournalRecovery:
    """Validated observations, with unfinished admissions separate from results.

    Pending admissions are evidence only. Consumers must use settled() before
    ordinary replay; a source without a completion must never become a receipt.
    """
    records: dict
    pending_agents: dict
    parent_run_id: str = ''
    prepared_agents: dict = field(default_factory=dict)

    def settled(self):
        if self.pending_agents:
            raise RecoveryRefused('A model/tool invocation was interrupted; its external effects are unknown')
        return deepcopy(self.records), self.parent_run_id


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def encode_agent_result(value) -> dict:
    return asdict(normalize_orchestration_agent_result(value))


def decode_agent_result(value: dict) -> OrchestrationAgentResult:
    try:
        row = deepcopy(value)
        if 'worker_context' in row:
            from lib.goal_runs.worker_context import GoalWorkerContext
            GoalWorkerContext.validate_snapshot(row.pop('worker_context'))
        row['model_route'] = OrchestrationModelRoute(**row['model_route'])
        usage = row['tool_usage']
        row['tool_usage'] = OrchestrationToolUsage(
            state_changing_tools=tuple(usage['state_changing_tools']),
            exploratory_tools=tuple(usage['exploratory_tools']),
            reported=usage['reported'])
        row['tool_log'] = tuple(row['tool_log'])
        return OrchestrationAgentResult(**row)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryRefused('Invalid recorded agent result') from exc


class ExecutionJournal:
    """One execution-tree receipt owner; scoped views share its lock and sink."""

    def __init__(self, emit, *, records: dict | None = None, scope: str = 'root',
                 _root=None, suspend_run_id: str = '', worker_context_policy: str = 'handoff',
                 account_handoff_commit=None, unsettled_observer=None):
        self._root = _root or self
        self._scope = scope
        self._counts = defaultdict(int)
        if worker_context_policy not in {'handoff', 'conversation'}:
            raise ValueError('Unsupported journal worker context policy')
        self.worker_context_policy = (_root.worker_context_policy
                                      if _root is not None else worker_context_policy)
        if _root is None:
            if unsettled_observer is not None and not callable(unsettled_observer):
                raise ValueError('Unsettled inspection requires its explicit observer')
            self._emit = emit
            self._unsettled_observer = unsettled_observer
            self._records = deepcopy(records or {})
            self._used = set()
            self._lock = threading.RLock()
            self._failed = threading.Event()
            self._suspend_run_id = suspend_run_id
            self._pending_humans = {}
            self._account_handoff_commit = account_handoff_commit
            self._external_commit = False
            self._agent_dispatch = None
            self._agent_preparations = {}

    def bind_agent_dispatch(self, dispatch, *, preparations=None):
        """Bind one prepared-recipient policy before any graph occurrence starts."""
        root = self._root
        with root._lock:
            if (self is not root or root._agent_dispatch is not None or root._counts
                    or root.abort_requested() or not callable(dispatch)):
                raise RecoveryRefused('Prepared agent dispatch requires an unused root journal')
            from lib.orchestration.invocation_recovery import completion_binding
            if preparations is not None and not isinstance(preparations, dict):
                raise RecoveryRefused('Prepared agent correlation requires an exact map')
            copied = deepcopy(preparations or {})
            for key, event in copied.items():
                if (not isinstance(event, dict) or set(event) != {'type', 'key', 'kind',
                        'input_hash', 'invocation_id', 'invocation_completion'}
                        or not isinstance(key, str) or not key or event.get('key') != key
                        or event.get('type') != 'execution_effect_prepared' or event.get('kind') != 'agent'
                        or not isinstance(event.get('input_hash'), str) or len(event['input_hash']) != 64
                        or any(c not in '0123456789abcdef' for c in event['input_hash'])
                        or _invocation_locator(event) is None or completion_binding(event) is None):
                    raise RecoveryRefused('Prepared agent correlation is incomplete')
            root._agent_preparations = copied
            root._agent_dispatch = dispatch

    def revoke(self):
        """Cancel sibling admission after any required publication failure."""
        self._root._failed.set()

    def publish_account_frontier(self, frontier, *, launch_id):
        """Serialize budget observations on the existing durable event stream.

        Account callbacks hold the budget lock before entering this journal.
        Do not acquire that budget lock from a locked journal callback.
        """
        with self._root._lock:
            if self.abort_requested():
                raise RecoveryRefused('Execution journal publication failed')
            self._root._publish(dict(type='execution_account_frontier',
                launch_id=launch_id, frontier=frontier))

    def abort_requested(self):
        return self._root._failed.is_set()

    def commit_account_handoff(self, frontiers, commit, *, tenant_id=''):
        """Serialize an acknowledged account transaction with ordinary effects.

        The budget lock precedes this lock, as for ordinary account cuts. The
        callback must not acquire that budget lock from another thread. Reentry,
        cancellation and unknown ACKs revoke further journal publication.
        """
        root = self._root
        with root._lock:
            if root._failed.is_set() or root._external_commit or not callable(root._account_handoff_commit):
                root._failed.set()
                raise RecoveryRefused('Execution journal cannot commit this account handoff')
            root._external_commit = True
            try:
                root._account_handoff_commit(frontiers, commit, tenant_id=tenant_id)
                if root._failed.is_set():
                    raise RecoveryRefused('Execution journal was revoked during account handoff')
            except BaseException:
                root._failed.set()
                raise
            finally:
                root._external_commit = False

    def _publish(self, event):
        if self._root._unsettled_observer is not None:
            self.revoke()
            raise RecoveryRefused('Recovery inspection cannot publish execution events')
        if self._root._external_commit:
            self._root._failed.set()
            raise RecoveryRefused('Execution journal publication reentered an account handoff')
        try:
            self._root._emit(event)
        except BaseException as exc:
            self._root._failed.set()
            if not isinstance(exc, Exception) or isinstance(exc, FlowEventDeliveryError):
                raise
            raise RecoveryRefused('Execution journal publication failed') from exc

    def begin(self, definition: dict, initial_context: str, *, parent_run_id='', continuation=False,
              launch_metadata: dict | None = None):
        root = self._root
        metadata = ({} if launch_metadata is None else {
            'launch_metadata': deepcopy(launch_metadata),
            'launch_metadata_hash': fingerprint(launch_metadata),
        })
        root._publish({'type': 'execution_journal_start', 'version': JOURNAL_VERSION,
                    'definition_hash': fingerprint(definition),
                    'input_hash': fingerprint(initial_context),
                    'parent_run_id': parent_run_id, 'continuation': continuation,
                    **metadata})
        # Cross-run recovery copies receipts before dispatch. Same-log
        # continuations prove the prior records by hash/count without copying.
        for key, record in (() if continuation else root._records.items()):
            root._publish({'type': 'execution_effect_completed', 'key': key,
                        **deepcopy(record), 'inherited': True})
        root._publish({'type': 'execution_journal_ready',
                    'seed_hash': fingerprint(root._records),
                    'seed_count': len(root._records)})

    def identity(self, kind: str, node_id: str, iteration: int = 0) -> str:
        with self._root._lock:
            slot = (kind, str(node_id), iteration)
            self._counts[slot] += 1
            # JSON tuple framing avoids collisions from IDs containing ':' or '/'.
            return json.dumps([self._scope, *slot, self._counts[slot]],
                              ensure_ascii=False, separators=(',', ':'))

    def child(self, node_id: str):
        return ExecutionJournal(None, scope=self.identity('subflow', node_id),
                                _root=self._root)

    def call(self, kind: str, node_id: str, iteration: int, inputs, operation,
             on_replay=None, prepare_before_admission=False, recovery_context=None):
        """Fence the entire effect boundary, including replay and receipt preparation."""
        from lib.orchestration.continuation import FlowSuspended
        try:
            return self._call(kind, node_id, iteration, inputs, operation,
                              on_replay, prepare_before_admission, recovery_context)
        except FlowSuspended:
            # A durable human gate transfers control to the continuation owner.
            raise
        except BaseException:
            # Cancellation and local receipt failures are just as ambiguous
            # as a failed write. Preserve the original failure and stop new
            # sibling effects; already admitted completions may still settle.
            self._root._failed.set()
            raise

    def _call(self, kind, node_id, iteration, inputs, operation,
              on_replay, prepare_before_admission, recovery_context):
        """Journal an opaque call, or prepare an agent whose operation admits dispatch.

        Deferred operations receive an awaited admission callback. Their setup
        must not execute models/tools; the ordinary child owner calls it at the
        execution boundary. Completed replay never enters either path.
        """
        if self.abort_requested():
            raise RecoveryRefused('Execution journal publication failed')
        if prepare_before_admission and kind != 'agent':
            raise ValueError('Only an agent can prepare before execution admission')
        key = self.identity(kind, node_id, iteration)
        digest = fingerprint(inputs)
        root = self._root
        with root._lock:
            if root._failed.is_set():
                raise RecoveryRefused('Execution journal publication failed')
            existing = root._records.get(key)
            if existing is not None:
                if existing['input_hash'] != digest or existing['kind'] != kind:
                    root._failed.set()
                    raise RecoveryRefused(f'Flow replay input changed at {node_id}')
                root._used.add(key)
                if on_replay is not None:
                    on_replay()
                return deepcopy(existing['result'])
            if root._unsettled_observer is not None:
                from lib.orchestration.continuation import FlowSuspended
                root._unsettled_observer(key=key, kind=kind, inputs=inputs,
                    iteration=iteration, input_hash=digest,
                    worker_context=recovery_context() if recovery_context is not None else None)
                # Reuse the graph's joined control transfer. No effect callback,
                # admission, invented result or durable event is entered.
                raise FlowSuspended()
            preparation = root._agent_preparations.get(key)
            if preparation is not None and (not prepare_before_admission
                    or preparation['kind'] != kind or preparation['input_hash'] != digest):
                root._failed.set()
                raise RecoveryRefused('Agent occurrence differs from its reserved preparation')
            root._publish(deepcopy(preparation) if preparation is not None else {
                'type': ('execution_effect_prepared' if prepare_before_admission else 'execution_effect_started'),
                'key': key, 'kind': kind, 'input_hash': digest})
        admitted = not prepare_before_admission
        admitted_invocation_id = None
        completion_writer = None
        completion_binding = None

        def admit(*, invocation_id=None, completion=None):
            nonlocal admitted, admitted_invocation_id, completion_writer, completion_binding
            with root._lock:
                if root._failed.is_set() or admitted:
                    root._failed.set()
                    raise RecoveryRefused('Execution admission is revoked or repeated')
                event = {'type': 'execution_effect_started', 'key': key,
                         'kind': kind, 'input_hash': digest}
                if invocation_id is not None:
                    event['invocation_id'] = invocation_id
                admitted_invocation_id = _invocation_locator(event)
                if completion is not None:
                    completion_binding = completion.bind(event)
                    event['invocation_completion'] = completion_binding
                    completion_writer = completion
                if preparation is not None and event != {**preparation, 'type': 'execution_effect_started'}:
                    root._failed.set()
                    raise RecoveryRefused('Execution admission changed its reserved preparation')
                root._publish(event)
                root._agent_preparations.pop(key, None)
                admitted = True
        if kind == 'human' and root._suspend_run_id:
            from lib.orchestration._runtime_params import resolve_node_runtime_param
            if resolve_node_runtime_param(inputs[0], 'mode') != 'notify':
                from lib.orchestration.continuation import FlowSuspended, human_request
                request = human_request(root._suspend_run_id, key, inputs, digest)
                with root._lock:
                    root._pending_humans[key] = request
                raise FlowSuspended()
        # No lock across model calls, tools or human waits.
        try:
            result = operation(AgentExecutionAdmission(key, digest, root, admit)) if prepare_before_admission else operation()
            if not admitted:
                raise RecoveryRefused('Agent result has no execution admission')
        except Exception as exc:
            root._failed.set()
            raise RecoveryRefused(f'Execution effect did not settle at {node_id}') from exc
        receipt = {'kind': kind, 'input_hash': digest, 'result': deepcopy(result)}
        if admitted_invocation_id is not None:
            receipt['invocation_id'] = admitted_invocation_id
        if completion_writer is not None:
            receipt['invocation_completion'] = completion_binding
            try:
                receipt['execution_account'] = completion_writer.capture_account()
                completion_writer.settle(receipt)
            except BaseException:
                root._failed.set()
                raise
        with root._lock:
            root._publish({'type': 'execution_effect_completed', 'key': key, **receipt})
        return result

    def runner(self, runner, on_replay=None, *, context_snapshot=None, context_restore=None,
               prepare_before_admission=False, runner_owner=None):
        if self.worker_context_policy == 'conversation' and (
                context_snapshot is None or context_restore is None):
            raise RecoveryRefused('Conversation journal requires worker context capture and restore')
        if self._root._agent_dispatch is not None and (not prepare_before_admission or runner_owner is None):
            raise RecoveryRefused('Prepared recipients require the ordinary registered runner')
        def invoke(node, context, iteration):
            # Runtime cancellation closures are process-local, never input data.
            durable_node = {key: value for key, value in node.items()
                            if key != '_runtimeAbortCheck'}
            replayed = False
            def replay():
                nonlocal replayed
                replayed = True
                if on_replay is not None:
                    on_replay()
            def operation(admit=None):
                dispatch = self._root._agent_dispatch
                if dispatch is not None:
                    value = dispatch(admission=admit, runner=runner_owner,
                        node=node, context=context, iteration=iteration)
                else:
                    value = (runner(node, context, iteration, before_dispatch=admit)
                             if prepare_before_admission else runner(node, context, iteration))
                value = encode_agent_result(value)
                if self.worker_context_policy == 'conversation':
                    value['worker_context'] = context_snapshot()
                    # Validate before the completion receipt can grant replay.
                    decode_agent_result(value)
                return value
            result = self.call('agent', str(node.get('id')), iteration,
                               [durable_node, context],
                               operation, on_replay=replay,
                               prepare_before_admission=prepare_before_admission,
                               recovery_context=(context_snapshot if self.worker_context_policy == 'conversation' else None))
            try:
                if self.worker_context_policy == 'conversation':
                    if 'worker_context' not in result:
                        raise RecoveryRefused('Conversation receipt has no worker context')
                    if replayed:
                        context_restore(result['worker_context'])
                elif 'worker_context' in result:
                    raise RecoveryRefused('Conversation receipt cannot replay with handoff context')
                return decode_agent_result(result)
            except BaseException:
                self.revoke()
                raise
        return invoke

    def resolver(self, resolver):
        if resolver is None:
            return None
        return lambda ref: self.call('definition', ref, 0, ref, lambda: resolver(ref))

    def pending_humans(self):
        with self._root._lock:
            return deepcopy(list(self._root._pending_humans.values()))

    def verify_consumed(self):
        root = self._root
        with root._lock:
            if set(root._records) != root._used:
                raise RecoveryRefused('Flow replay did not consume every recorded result')


def _invocation_locator(event):
    """A stored locator is evidence to query, never authorization to rerun a child."""
    if 'invocation_id' not in event:
        return None
    value = event['invocation_id']
    if event.get('kind') != 'agent' or not isinstance(value, str) or not value or len(value) > 512:
        raise RecoveryRefused('Execution invocation locator is invalid')
    return value


def _read_segment(events: list[dict], *, definition: dict, initial_context: str,
                  prior_records=None, reconcile_agent=None):
    """Validate a complete journal, returning receipts and an incomplete seed's parent.

    Event sequences belong to the store. Receipt identities belong here; never
    infer completion from UI step previews or successful tool admission.
    """
    starts = [event for event in events if event.get('type') == 'execution_journal_start']
    if len(starts) != 1:
        raise RecoveryRefused('Run has no unambiguous execution journal')
    header = starts[0]
    if ('launch_metadata' in header or 'launch_metadata_hash' in header) and (
            not isinstance(header.get('launch_metadata'), dict)
            or header.get('launch_metadata_hash') != fingerprint(header['launch_metadata'])):
        raise RecoveryRefused('Execution launch metadata does not match its witness')
    if (header.get('version') != JOURNAL_VERSION
            or header.get('definition_hash') != fingerprint(definition)
            or header.get('input_hash') != fingerprint(initial_context)):
        raise RecoveryRefused('Execution journal version or definition/input does not match')
    records, pending, prepared = deepcopy(prior_records or {}), {}, {}
    ready = False
    seen_header = False
    for event in events:
        kind = event.get('type')
        if kind not in JOURNAL_EVENTS:
            continue
        if kind == 'execution_journal_start':
            seen_header = True
            continue
        if not seen_header:
            raise RecoveryRefused('Execution journal precedes its header')
        if kind == 'execution_journal_ready':
            if ready or event.get('seed_count') != len(records) or event.get('seed_hash') != fingerprint(records):
                raise RecoveryRefused('Incomplete or conflicting recovery seed')
            ready = True
            continue
        if kind == 'execution_account_frontier':
            if not ready:
                raise RecoveryRefused('Execution account precedes ready admission')
            # The domain launch owner validates its binding and complete stream.
            from lib.agent_core.account_frontier import validate_frontier
            try: validate_frontier(event.get('frontier'))
            except ValueError as exc:
                raise RecoveryRefused('Execution account frontier is invalid') from exc
            continue
        key = event.get('key')
        if not isinstance(key, str) or not key:
            raise RecoveryRefused('Execution receipt is missing its identity')
        receipt_kind = event.get('kind')
        if receipt_kind not in ('agent', 'human', 'definition'):
            raise RecoveryRefused('Unknown execution receipt kind')
        if kind == 'execution_effect_prepared':
            if (not ready or receipt_kind != 'agent' or key in prepared
                    or key in pending or key in records):
                raise RecoveryRefused('Conflicting execution preparation')
            from lib.orchestration.invocation_recovery import completion_binding
            locator, binding = _invocation_locator(event), completion_binding(event)
            if (locator is None) != (binding is None):
                raise RecoveryRefused('Execution preparation has incomplete invocation identity')
            prepared[key] = {field: deepcopy(event[field]) for field in (
                'type', 'key', 'kind', 'input_hash', 'invocation_id', 'invocation_completion')
                if field in event}
            continue
        if kind == 'execution_effect_started':
            preparation = prepared.pop(key, None)
            if preparation is not None and any(event.get(field) != value for field, value in preparation.items()
                                               if field != 'type'):
                raise RecoveryRefused('Execution admission changed its preparation')
            if not ready or key in pending or key in records:
                raise RecoveryRefused('Conflicting execution admission')
            from lib.orchestration.invocation_recovery import completion_binding
            _invocation_locator(event)
            completion_binding(event)
            pending[key] = {field: deepcopy(event[field]) for field in (
                'type', 'key', 'kind', 'input_hash', 'invocation_id', 'invocation_completion')
                if field in event}
        else:
            if not ready and prior_records is not None:
                raise RecoveryRefused('Same-log continuation cannot replace its seed')
            record = {field: event.get(field) for field in ('kind', 'input_hash', 'result')}
            invocation_id = _invocation_locator(event)
            if invocation_id is not None:
                record['invocation_id'] = invocation_id
            from lib.orchestration.invocation_recovery import completion_binding
            binding = completion_binding(event)
            if binding is not None:
                record['invocation_completion'] = binding
            from lib.orchestration.recovery_account import receipt_account
            account = receipt_account(event)
            if account is not None:
                record['execution_account'] = account
            if key in records:
                raise RecoveryRefused('Duplicate execution result')
            if ready:
                admission = pending.pop(key, None)
                expected = {field: value for field, value in record.items()
                            if field not in ('result', 'execution_account')}
                if admission is None or {
                        field: value for field, value in admission.items() if field not in ('type', 'key')
                        } != expected:
                    raise RecoveryRefused('Execution result has no matching admission')
            if not ready and event.get('inherited') is not True:
                raise RecoveryRefused('Uncommitted recovery seed')
            if record['kind'] == 'agent':
                decode_agent_result(record['result'])
            records[key] = record
    if not ready:
        if prior_records is not None:
            return JournalRecovery(records, {})
        parent = header.get('parent_run_id')
        if parent == '' and not records and not pending:
            # Versioned admission protocol proves no effect can precede ready.
            return JournalRecovery({}, {})
        if not isinstance(parent, str) or not parent:
            raise RecoveryRefused('Execution journal initialization did not commit')
        return JournalRecovery({}, {}, parent)
    unresolved = {}
    for key, admission in pending.items():
        if admission['kind'] != 'agent':
            continue
        record = (reconcile_agent(deepcopy(admission))
                  if reconcile_agent is not None and 'invocation_completion' in admission else None)
        if record is None:
            unresolved[key] = deepcopy(admission)
            continue
        expected = {field: value for field, value in admission.items() if field not in ('type', 'key')}
        if (not isinstance(record, dict)
                or set(record) not in ({*expected, 'result'}, {*expected, 'result', 'execution_account'})
                or any(record.get(field) != value for field, value in expected.items())):
            raise RecoveryRefused('Reconciled invocation result changed its admission')
        from lib.orchestration.recovery_account import receipt_account
        receipt_account(record)
        decode_agent_result(record['result'])
        records[key] = deepcopy(record)
    return JournalRecovery(records, unresolved, prepared_agents={key: value for key, value in prepared.items()
        if 'invocation_completion' in value})


def inspect_journal(events: list[dict], *, definition: dict, initial_context: str,
                 reconcile_agent=None):
    """Validate history without turning an absent completion into replay authority.

    An optional resolver returns None only for an absent completion. Invalid or
    inaccessible evidence raises; inspection must not convert failures to pending.
    """
    starts = [i for i, e in enumerate(events) if e.get('type') == 'execution_journal_start']
    if starts and events[starts[0]].get('continuation') is True:
        raise RecoveryRefused('Continuation has no preceding journal authority')
    if len(starts) <= 1:
        return _read_segment(events, definition=definition, initial_context=initial_context,
                             reconcile_agent=reconcile_agent)
    if any(e.get('type') in JOURNAL_EVENTS for e in events[:starts[0]]):
        raise RecoveryRefused('Execution journal precedes its header')
    records = None
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(events)
        segment = events[start:end]
        if index and segment[0].get('continuation') is not True:
            raise RecoveryRefused('Conflicting continuation header')
        evidence = _read_segment(segment, definition=definition,
            initial_context=initial_context, prior_records=records, reconcile_agent=reconcile_agent)
        if evidence.parent_run_id or index == len(starts) - 1:
            return evidence
        # An unresolved previous generation cannot be hidden by a later header.
        records, _ = evidence.settled()
    raise RecoveryRefused('Run has no execution journal')


def read_journal(events: list[dict], *, definition: dict, initial_context: str,
                 reconcile_agent=None):
    """Read replayable results only; pending observations never authorize retry."""
    return inspect_journal(events, definition=definition, initial_context=initial_context,
                           reconcile_agent=reconcile_agent).settled()
