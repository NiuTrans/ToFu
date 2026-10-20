"""Native gateway evidence composition; Rust owns streaks and stop decisions.

One immutable current batch retains hashes/counts, bounded by the existing native
feedback budget. Tools publish evidence after canonical settlement in their
existing worker. The stdio observer copies this small completed snapshot without
another executor, message history or task/financial state machine.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from lib.agent_core.native_process import ProgressLimits
from lib.agent_core.progress_evidence import (
    counted_result_evidence, nonretryable_failure_signature, tool_result_evidence,
)


@dataclass(frozen=True, slots=True)
class _Batch:
    round: int
    calls: int = 0
    evidence: tuple[tuple[str, int], ...] = ()
    failures: tuple[str, ...] = ()
    all_failed: bool = True
    complete: bool = True
    bytes: int = 0


class NativeGatewayProgress:
    def __init__(self, gateway, limits: ProgressLimits, feedback_bytes: int):
        if (not isinstance(limits, ProgressLimits) or type(feedback_bytes) is not int
                or feedback_bytes <= 0):
            raise ValueError('native gateway progress requires explicit limits and budget')
        self.gateway, self.limits, self.feedback_bytes = gateway, limits, feedback_bytes
        self.batch = None

    def record(self, round_num: int, call: dict, result):
        batch = self.batch if self.batch is not None and self.batch.round == round_num else _Batch(round_num)
        signature = nonretryable_failure_signature(call, result) if self.limits.nonretryable_failure_rounds else None
        all_failed = batch.all_failed and signature is not None
        evidence, failures, size, complete = batch.evidence, batch.failures, batch.bytes, batch.complete
        if complete:
            if self.limits.no_progress_rounds:
                digest = tool_result_evidence(call, result)
                counts = dict(evidence)
                counts[digest] = counts.get(digest, 0) + 1
                evidence = tuple(counts.items())
            failures = tuple(sorted(set(failures) | {signature})) if all_failed else ()
            # Result digests/counts are ASCII; charge their emitted form without
            # sorting or allocating the observation on every tool completion.
            size = sum(len(digest) + len(str(count)) + 1 for digest, count in evidence)
            size += sum(len(value.encode('utf-8')) for value in failures)
            if size > self.feedback_bytes:
                evidence, failures, size, complete = (), (), 0, False
        self.batch = _Batch(round_num, batch.calls + 1, evidence, failures, all_failed, complete, size)

    async def observe(self, request):
        gateway = self.gateway
        if (gateway is None or gateway._closed or request.principal.require_owner(context='native gateway progress') != gateway.owner
                or request.task_id != gateway.task_id or gateway.task.get('id') != gateway.task_id
                or type(gateway.task.get('_userId')) is not int or gateway.task['_userId'] != gateway.owner
                or gateway.task.get('status') not in {'pending', 'running'}):
            raise ValueError('native gateway progress ownership changed')
        stopped = request.cancelled.is_set() or (request.deadline_monotonic_ns is not None
                  and time.monotonic_ns() >= request.deadline_monotonic_ns)
        batch = self.batch
        complete = bool(not stopped and batch is not None and batch.round == request.round
                        and batch.calls == request.tool_calls and batch.complete)
        world = str(gateway.task.get('_worldVersion') or '')
        # Leave room for both always-present strings under the Rust byte rule.
        if len(world.encode('utf-8')) + (batch.bytes if complete else 0) + 2 > self.feedback_bytes:
            world, complete = '', False
        observation = dict(worldVersion=world,
                           evidenceIds=list(counted_result_evidence(dict(batch.evidence))) if complete else [],
                           resultEvidenceComplete=complete, verification='',
                           nonretryableFailureSignatures=list(batch.failures) if complete and batch.all_failed else [])
        self.batch = None  # Completed-batch evidence is reconstructible, not durable state.
        return observation

    def close(self):
        self.batch = None
        self.gateway = None
