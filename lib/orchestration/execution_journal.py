"""Durable effect receipts for replaying a Flow without repeating completed work.

The runtime supplies an awaited durable event sink. Engine adapters journal
only nondeterministic boundaries (agents, human decisions, referenced graphs).
Control flow and its ledgers are reconstructed by the existing interpreter.
An unresolved agent receipt is ambiguous and is never retried automatically.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
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
                 _root=None, suspend_run_id: str = '', worker_context_policy: str = 'handoff'):
        self._root = _root or self
        self._scope = scope
        self._counts = defaultdict(int)
        if worker_context_policy not in {'handoff', 'conversation'}:
            raise ValueError('Unsupported journal worker context policy')
        self.worker_context_policy = (_root.worker_context_policy
                                      if _root is not None else worker_context_policy)
        if _root is None:
            self._emit = emit
            self._records = deepcopy(records or {})
            self._used = set()
            self._lock = threading.RLock()
            self._failed = threading.Event()
            self._suspend_run_id = suspend_run_id
            self._pending_humans = {}

    def revoke(self):
        """Cancel sibling admission after any required publication failure."""
        self._root._failed.set()

    def abort_requested(self):
        return self._root._failed.is_set()

    def _publish(self, event):
        try:
            self._root._emit(event)
        except Exception as exc:
            self._root._failed.set()
            if isinstance(exc, FlowEventDeliveryError):
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
             on_replay=None, prepare_before_admission=False):
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
            root._publish({'type': ('execution_effect_prepared' if prepare_before_admission
                                   else 'execution_effect_started'), 'key': key,
                        'kind': kind, 'input_hash': digest})
        admitted = not prepare_before_admission

        def admit():
            nonlocal admitted
            with root._lock:
                if root._failed.is_set() or admitted:
                    root._failed.set()
                    raise RecoveryRefused('Execution admission is revoked or repeated')
                root._publish({'type': 'execution_effect_started', 'key': key,
                               'kind': kind, 'input_hash': digest})
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
            result = operation(admit) if prepare_before_admission else operation()
            if not admitted:
                raise RecoveryRefused('Agent result has no execution admission')
        except Exception as exc:
            root._failed.set()
            raise RecoveryRefused(f'Execution effect did not settle at {node_id}') from exc
        receipt = {'kind': kind, 'input_hash': digest, 'result': deepcopy(result)}
        with root._lock:
            root._publish({'type': 'execution_effect_completed', 'key': key, **receipt})
        return result

    def runner(self, runner, on_replay=None, *, context_snapshot=None, context_restore=None,
               prepare_before_admission=False):
        if self.worker_context_policy == 'conversation' and (
                context_snapshot is None or context_restore is None):
            raise RecoveryRefused('Conversation journal requires worker context capture and restore')
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
                value = encode_agent_result(
                    runner(node, context, iteration, before_dispatch=admit)
                    if prepare_before_admission else runner(node, context, iteration))
                if self.worker_context_policy == 'conversation':
                    value['worker_context'] = context_snapshot()
                    # Validate before the completion receipt can grant replay.
                    decode_agent_result(value)
                return value
            result = self.call('agent', str(node.get('id')), iteration,
                               [durable_node, context],
                               operation, on_replay=replay,
                               prepare_before_admission=prepare_before_admission)
            if self.worker_context_policy == 'conversation':
                if 'worker_context' not in result:
                    raise RecoveryRefused('Conversation receipt has no worker context')
                if replayed:
                    context_restore(result['worker_context'])
            elif 'worker_context' in result:
                raise RecoveryRefused('Conversation receipt cannot replay with handoff context')
            return decode_agent_result(result)
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


def _read_segment(events: list[dict], *, definition: dict, initial_context: str, prior_records=None):
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
            prepared[key] = (receipt_kind, event.get('input_hash'))
            continue
        if kind == 'execution_effect_started':
            if key in prepared and prepared.pop(key) != (receipt_kind, event.get('input_hash')):
                raise RecoveryRefused('Execution admission changed its preparation')
            if not ready or key in pending or key in records:
                raise RecoveryRefused('Conflicting execution admission')
            pending[key] = (receipt_kind, event.get('input_hash'))
        else:
            if not ready and prior_records is not None:
                raise RecoveryRefused('Same-log continuation cannot replace its seed')
            record = {field: event.get(field) for field in ('kind', 'input_hash', 'result')}
            if key in records:
                raise RecoveryRefused('Duplicate execution result')
            if ready and pending.pop(key, None) != (record['kind'], record['input_hash']):
                raise RecoveryRefused('Execution result has no matching admission')
            if not ready and event.get('inherited') is not True:
                raise RecoveryRefused('Uncommitted recovery seed')
            if record['kind'] == 'agent':
                decode_agent_result(record['result'])
            records[key] = record
    if not ready:
        if prior_records is not None:
            return records, ''
        parent = header.get('parent_run_id')
        if parent == '' and not records and not pending:
            # Versioned admission protocol proves no effect can precede ready.
            return {}, ''
        if not isinstance(parent, str) or not parent:
            raise RecoveryRefused('Execution journal initialization did not commit')
        return {}, parent
    if any(kind == 'agent' for kind, _ in pending.values()):
        raise RecoveryRefused('A model/tool invocation was interrupted; its external effects are unknown')
    return records, ''


def read_journal(events: list[dict], *, definition: dict, initial_context: str):
    """Validate each resumed generation; incomplete seed copies dispatch nothing."""
    starts = [i for i, e in enumerate(events) if e.get('type') == 'execution_journal_start']
    if starts and events[starts[0]].get('continuation') is True:
        raise RecoveryRefused('Continuation has no preceding journal authority')
    if len(starts) <= 1:
        return _read_segment(events, definition=definition, initial_context=initial_context)
    if any(e.get('type') in JOURNAL_EVENTS for e in events[:starts[0]]):
        raise RecoveryRefused('Execution journal precedes its header')
    records = None
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(events)
        segment = events[start:end]
        if index and segment[0].get('continuation') is not True:
            raise RecoveryRefused('Conflicting continuation header')
        records, parent = _read_segment(segment, definition=definition,
            initial_context=initial_context, prior_records=records)
        if parent:
            return records, parent
    return records, ''
