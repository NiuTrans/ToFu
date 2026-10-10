"""Thin native-agent host port: identity, sealed executable, stdio and joined exit.

Rust owns model rounds and task settlement. Composition supplies an authorized
frozen attempt configuration and admitted read-tool callbacks; this module owns
no routing, billing, repository, durable task or fallback Python model loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache

try:
    import fcntl
except ImportError:  # Platform rejection happens before native acquisition.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, BinaryIO, Protocol

from lib.identity import PrincipalContext


class NativeProcessError(RuntimeError):
    """The native transport/installation failed; diagnostics exclude payloads."""


class NativeConfigurationError(NativeProcessError, ValueError):
    pass


class NativeToolError(NativeProcessError):
    pass


class NativePersistenceError(NativeProcessError):
    """Durability callback/acknowledgment failed; no terminal success inferred."""


class NativeRoundError(NativeProcessError):
    """Reservation/accounting callback failed; original cause is retained."""


@dataclass(frozen=True, slots=True)
class Budget:
    max_inflight: int  # Retired field; ignored for run admission.
    binary_bytes: int
    configuration_bytes: int
    frame_bytes: int
    shutdown_seconds: float

    def __post_init__(self) -> None:
        for value in (
            self.binary_bytes,
            self.configuration_bytes,
            self.frame_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise NativeConfigurationError(
                    "native byte/count budgets must be positive integers"
                )
        if type(self.shutdown_seconds) not in (
            int,
            float,
        ) or not 0 < self.shutdown_seconds < float("inf"):
            raise NativeConfigurationError(
                "native shutdown budget must be finite and positive"
            )


@dataclass(frozen=True, slots=True)
class ToolRequest:
    principal: PrincipalContext
    task_id: str
    request_id: str
    name: str
    arguments: Mapping[str, Any]
    call_id: str | None = None
    caller: Any = None
    cancelled: asyncio.Event | None = None
    model_round: int | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    principal: PrincipalContext
    task_id: str
    request_id: str
    call_id: str
    name: str
    epoch: str
    arguments_json: bytes
    caller_json: bytes
    cancelled: asyncio.Event
    remaining_seconds: float

    @property
    def arguments(self):
        return json.loads(self.arguments_json)

    @property
    def caller(self):
        return json.loads(self.caller_json)


class ApprovalPolicy(Protocol):
    async def decide(self, request: ApprovalRequest) -> str: ...


class _ApprovalControl:
    AUTHORITIES = frozenset({'human', 'auto_write_mode', 'agent_adjudicator',
                            'agent_git_guard_ack', 'agent_git_guard_solo', 'git_safe_write'})

    def __init__(self, client, policy, task_id, epoch, tools, profile):
        self.client, self.policy, self.task_id, self.epoch = client, policy, task_id, epoch
        self.tools, self.profile = tools, profile
        self.pending, self.ordinal, self.bytes, self.failed = {}, 0, 0, False

    def proof(self, value):
        return json.dumps([value['name'], value['arguments'], value.get('caller')],
                          sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()

    async def handle(self, value, process):
        owner = self.client.principal.require_owner(context='native approval')
        if (value.get('version') != self.client.protocol_version
                or type(value.get('owner_user_id')) is not int or value['owner_user_id'] != owner
                or value.get('task_id') != self.task_id or value.get('epoch') != self.epoch
                or value.get('request_id') != f'{self.task_id}-approval-{self.ordinal}'
                or not isinstance(value.get('call_id'), str) or not value['call_id']
                or not isinstance(value.get('arguments'), dict)
                or type(value.get('remaining_ms')) is not int or value['remaining_ms'] <= 0
                or value.get('name') not in self.tools
                or self.tools[value['name']]['permission'] == 'read'
                or value['call_id'] in self.pending):
            raise NativeProcessError('native approval identity/authority differs')
        proof = self.proof(value)
        if len(self.pending) >= self.profile['receipts'] or self.bytes + len(proof) > self.profile['identity_bytes']:
            raise NativeProcessError('native approval receipt budget exceeded')
        self.ordinal += 1
        cancelled, failure, decision = False, None, 'denied'
        request = ApprovalRequest(self.client.principal, self.task_id, value['request_id'],
            value['call_id'], value['name'], self.epoch,
            json.dumps(value['arguments'], allow_nan=False).encode(),
            json.dumps(value.get('caller'), allow_nan=False).encode(), asyncio.Event(), value['remaining_ms']/1000)
        callback = None
        timeout = asyncio.timeout(request.remaining_seconds)
        try:
            callback = asyncio.create_task(self.policy.decide(request))
            try:
                async with timeout:
                    decision = await asyncio.shield(callback)
            except TimeoutError:
                if not timeout.expired():
                    raise
                request.cancelled.set()
                await _joined(callback)
                decision = 'denied'
            except asyncio.CancelledError:
                if callback.cancelled() and not asyncio.current_task().cancelling():
                    raise NativeProcessError('native approval policy cancelled itself')
                cancelled = True
                request.cancelled.set()
                async def stop():
                    process.stdin.write(self.client._encode({'version':self.client.protocol_version,
                        'type':'cancel'}, self.client.budget.frame_bytes))
                    await process.stdin.drain()
                await _joined(asyncio.create_task(stop()))
                decision = await _joined(callback)
            if not isinstance(decision, str) or decision not in self.AUTHORITIES | {'denied'}:
                raise TypeError('native approval requires a declared authority or denial')
        except Exception as error:
            failure, self.failed, decision = error, True, 'failed'
        finally:
            if callback is not None and not callback.done():
                request.cancelled.set()
                await _joined(callback)
        if cancelled:
            decision = 'denied'
        if decision in self.AUTHORITIES:
            self.pending[value['call_id']] = proof
            self.bytes += len(proof)
        async def acknowledge():
            async with asyncio.timeout(self.client.budget.shutdown_seconds):
                process.stdin.write(self.client._encode({'version':self.client.protocol_version,
                    'type':'approval_decision','request_id':value['request_id'],'decision':decision},self.client.budget.frame_bytes))
                await process.stdin.drain()
        ack = asyncio.create_task(acknowledge())
        try:
            try:
                await asyncio.shield(ack)
            except asyncio.CancelledError:
                cancelled = True
                await _joined(ack)
        except Exception as error:
            if failure is None:
                raise NativeProcessError('native approval decision delivery failed') from error
        if failure is not None:
            raise NativeProcessError('native approval policy failed') from failure
        if cancelled:
            raise asyncio.CancelledError

    def consume(self, value):
        if self.tools[value['name']]['permission'] == 'read':
            if value.get('call_id') is not None:
                raise NativeProcessError('read tool carries an unexpected approval')
            return
        proof = self.pending.pop(value.get('call_id'), None)
        if proof is not None:
            self.bytes -= len(proof)
        if proof is None or self.proof(value) != proof:
            raise NativeProcessError('native tool has no exact unused host approval')


@dataclass(frozen=True, slots=True)
class RoundRequest:
    principal: PrincipalContext
    task_id: str
    request_id: str
    round: int
    history_messages: int
    messages: list[dict] | None = None
    deadline_monotonic_ns: int | None = None


@dataclass(frozen=True, slots=True)
class RoundSettlement:
    request: RoundRequest
    evidence: Mapping[str, Any]
    cancelled: threading.Event | None = None


class RoundPolicy(Protocol):
    async def reserve(self, request: RoundRequest) -> bool: ...
    async def settle(self, result: RoundSettlement) -> None: ...


@dataclass(frozen=True, slots=True)
class ProgressLimits:
    no_progress_rounds: int = 0
    nonretryable_failure_rounds: int = 0

    def __post_init__(self):
        values = (self.no_progress_rounds, self.nonretryable_failure_rounds)
        if any(type(value) is not int or not 0 <= value <= 18446744073709551615 for value in values) or not any(values):
            raise ValueError('native progress requires explicit enabled limits')


@dataclass(frozen=True, slots=True)
class ProgressRequest:
    principal: PrincipalContext
    task_id: str
    round: int
    tool_calls: int
    cancelled: threading.Event
    deadline_monotonic_ns: int | None = None


class ProgressPolicy(Protocol):
    limits: ProgressLimits
    async def observe(self, request: ProgressRequest) -> Mapping[str, Any]: ...


class _ProgressControl:
    def __init__(self, rounds, policy):
        self.rounds, self.policy = rounds, policy
        self.last_round = -1
        self.failure = None

    async def handle(self, value, process, *, closing=False):
        rounds = self.rounds
        rounds.identity(value)
        if (set(value) != {'version', 'type', 'request_id', 'owner_user_id', 'task_id', 'round', 'tool_calls'}
                or not value['request_id'] or type(value.get('tool_calls')) is not int or value['tool_calls'] <= 0
                or rounds.pending is not None or not rounds.settled_progress_ready
                or value['round'] != rounds.next_round - 1 or value['round'] != rounds.settled_round
                or value['round'] <= self.last_round):
            raise NativeProcessError('native progress preceded settlement or repeated')
        self.last_round = value['round']
        signal = threading.Event()
        if closing:
            signal.set()
        request = ProgressRequest(rounds.client.principal, rounds.task_id, value['round'], value['tool_calls'], signal, rounds.settled_deadline)
        observation = dict(worldVersion='', evidenceIds=[], resultEvidenceComplete=False,
                           verification='', nonretryableFailureSignatures=[])
        accepted, cancelled = False, False
        if request.deadline_monotonic_ns is not None and time.monotonic_ns() >= request.deadline_monotonic_ns:
            return
        try:
            work = asyncio.create_task(self.policy.observe(request))
            try:
                at = (asyncio.get_running_loop().time() + max(0, request.deadline_monotonic_ns - time.monotonic_ns()) / 1e9
                      if request.deadline_monotonic_ns is not None else None)
                async with asyncio.timeout_at(at):
                    result = await asyncio.shield(work)
            except TimeoutError:
                signal.set()
                await _joined(work)
                raise
            except asyncio.CancelledError:
                cancelled = True
                signal.set()
                result = await _joined(work)
            keys = set(observation)
            if (not isinstance(result, Mapping) or set(result) != keys
                    or type(result['resultEvidenceComplete']) is not bool
                    or any(not isinstance(result[key], str) for key in ('worldVersion', 'verification'))
                    or any(not isinstance(result[key], list) or any(not isinstance(item, str) for item in result[key])
                           for key in ('evidenceIds', 'nonretryableFailureSignatures'))):
                raise TypeError('native progress observation shape invalid')
            candidate = dict(result)
            rounds.client._encode(dict(version=rounds.client.protocol_version, type='progress_decision',
                                      request_id=value['request_id'], accepted=not signal.is_set(), observation=candidate),
                                  rounds.client.budget.frame_bytes)
            observation, accepted = candidate, not signal.is_set()
        except Exception as error:
            self.failure = error
        # Like preparation, an expired native deadline can already have moved
        # the one-slot reader to terminal persistence. Never send a stale reply.
        expired = request.deadline_monotonic_ns is not None and time.monotonic_ns() >= request.deadline_monotonic_ns
        if not expired:
            async def send_reply():
                async with asyncio.timeout(rounds.client.budget.shutdown_seconds):
                    await rounds.send(process, 'progress_decision', value['request_id'], accepted=accepted, observation=observation)
            reply = asyncio.create_task(send_reply())
            try:
                await asyncio.shield(reply)
            except asyncio.CancelledError:
                cancelled = True
                signal.set()
                await _joined(reply)
        if cancelled:
            raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    principal: PrincipalContext
    task_id: str
    action: str
    round: int
    epoch: str
    binding: str
    cancelled: threading.Event
    deadline_monotonic_ns: int | None = None
    state: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CheckpointTerminalRequest:
    principal: PrincipalContext
    task_id: str
    epoch: str
    binding: str
    terminal: TerminalRequest
    suspended: bool
    cancelled: threading.Event


class CheckpointPolicy(Protocol):
    binding: str
    async def load(self, request: CheckpointRequest) -> Mapping[str, Any] | None: ...
    async def begin(self, request: CheckpointRequest) -> None: ...
    async def complete(self, request: CheckpointRequest) -> str: ...
    async def seal(self, request: CheckpointTerminalRequest) -> str: ...


class _CheckpointControl:
    def __init__(self, rounds, policy, epoch):
        self.rounds, self.policy, self.epoch = rounds, policy, epoch
        self.model = None
        self.binding = policy.binding
        self.loaded = False
        self.suspended = False
        self.inflight = None
        self.failure = None

    def state_identity(self, state):
        if (not isinstance(state, Mapping) or state.get('format') != 'tofu.native-agent-checkpoint/v1'
                or type(state.get('owner_user_id')) is not int
                or state['owner_user_id'] != self.rounds.client.principal.require_owner(context='native checkpoint')
                or state.get('task_id') != self.rounds.task_id or state.get('epoch') != self.epoch
                or state.get('binding') != self.binding or type(state.get('next_round')) is not int
                or state['next_round'] < 1):
            raise NativeProcessError('native checkpoint state identity differs')

    async def handle(self, value, process, *, closing=False):
        rounds = self.rounds
        rounds.identity(value)
        action, number = value.get('action'), value['round']
        state = value.get('state')
        if value.get('binding') != self.binding or value.get('epoch') != self.epoch or rounds.pending is not None:
            raise NativeProcessError('native checkpoint authority differs')
        if action == 'load':
            if self.loaded or number != 0 or state is not None or rounds.next_round != 0:
                raise NativeProcessError('native checkpoint load order differs')
            self.loaded = True
        elif action == 'begin':
            if not self.loaded or self.inflight is not None or number != rounds.next_round or state is not None:
                raise NativeProcessError('native checkpoint begin order differs')
        elif action == 'complete':
            if (not self.loaded or self.inflight != number or rounds.settled_round != number
                    or not rounds.settled_progress_ready or rounds.next_round != number + 1
                    or (rounds.progress is not None and rounds.progress.last_round != number)):
                raise NativeProcessError('native checkpoint completion preceded settlement')
            self.state_identity(state)
            if state['next_round'] != number + 1:
                raise NativeProcessError('native checkpoint completion round differs')
        else:
            raise NativeProcessError('native checkpoint action invalid')
        deadline = value.get('deadline_monotonic_ns')
        if deadline is not None and (type(deadline) is not int or not 0 <= deadline <= 18446744073709551615):
            raise NativeProcessError('native checkpoint deadline invalid')
        signal = threading.Event()
        if closing:
            signal.set()
        request = CheckpointRequest(rounds.client.principal, rounds.task_id, action, number,
                                    self.epoch, self.binding, signal, deadline, state)
        if deadline is not None and time.monotonic_ns() >= deadline:
            return
        accepted, cancelled, disposition, loaded = False, False, 'continue', None
        try:
            async def invoke():
                candidate = request
                if self.model is not None and action == 'complete':
                    from dataclasses import replace
                    if 'shared_model_account' in request.state or 'model_account' in request.state:
                        raise NativeProcessError('native completion cannot invent shared account state')
                    captured = await self.model.capture_checkpoint(request)
                    candidate = replace(request,state=dict(request.state,shared_model_account=captured))
                result = await getattr(self.policy, action)(candidate)
                if self.model is not None and action == 'load' and result is not None:
                    await self.model.restore_checkpoint(request,result)
                return result
            work = asyncio.create_task(invoke())
            try:
                at = (asyncio.get_running_loop().time() + max(0, deadline - time.monotonic_ns()) / 1e9 if deadline is not None else None)
                async with asyncio.timeout_at(at):
                    result = await asyncio.shield(work)
            except TimeoutError:
                signal.set(); await _joined(work); raise
            except asyncio.CancelledError:
                signal.set(); cancelled = True; result = await _joined(work)
            if action == 'load':
                if result is not None:
                    self.state_identity(result)
                    loaded = dict(result)
                    rounds.next_round = loaded['next_round']
            elif action == 'begin':
                if result is not None: raise TypeError('native checkpoint begin must return None')
                self.inflight = number
            else:
                if result not in {'continue', 'suspend'}: raise TypeError('native checkpoint disposition invalid')
                disposition = result
                self.inflight = None
            reply = dict(version=rounds.client.protocol_version, type='checkpoint_decision',
                         request_id=value['request_id'], accepted=not signal.is_set(), disposition=disposition,
                         **({'state': loaded} if loaded is not None else {}))
            rounds.client._encode(reply, rounds.client.budget.frame_bytes)
            accepted = not signal.is_set()
            if action == "complete" and accepted:
                self.suspended = disposition == "suspend"
        except Exception as error:
            self.failure = error
            loaded = None
            disposition = 'continue'
        if deadline is None or time.monotonic_ns() < deadline:
            async def send_reply():
                async with asyncio.timeout(rounds.client.budget.shutdown_seconds):
                    await rounds.send(process, 'checkpoint_decision', value['request_id'], accepted=accepted,
                                      disposition=disposition, **({'state': loaded} if loaded is not None else {}))
            reply_task = asyncio.create_task(send_reply())
            try: await asyncio.shield(reply_task)
            except asyncio.CancelledError:
                signal.set(); cancelled = True; await _joined(reply_task)
        if cancelled: raise asyncio.CancelledError


class _RoundControl:
    def __init__(self, client, policy, task_id, prepare_history=False, preparation_bytes=None, progress_policy=None, checkpoint_policy=None, epoch=None):
        self.client = client
        self.policy = policy
        self.task_id = task_id
        self.pending = None
        self.attempted = False
        self.next_round = 0
        self.settled_round = None
        self.settled_deadline = None
        self.settled_progress_ready = False
        self.progress = _ProgressControl(self, progress_policy) if progress_policy is not None else None
        self.checkpoint = _CheckpointControl(self, checkpoint_policy, epoch) if checkpoint_policy is not None else None
        self.preparation_failure = None
        self.prepare_history = prepare_history
        self.preparation_bytes = preparation_bytes or client.budget.frame_bytes

    async def acknowledge(self, process, request_id, accepted):
        """Bound transport ACK independently of joined policy callback lifetime."""
        async with asyncio.timeout(self.client.budget.shutdown_seconds):
            await self.send(process, "round_ack", request_id, accepted=accepted)

    async def send(self, process, kind, request_id, **values):
        process.stdin.write(
            self.client._encode(
                {
                    "version": self.client.protocol_version,
                    "type": kind,
                    "request_id": request_id,
                    **values,
                },
                self.client.budget.frame_bytes,
            )
        )
        await process.stdin.drain()

    def identity(self, value):
        owner = self.client.principal.require_owner(context="native round policy")
        if (
            value.get("version") != self.client.protocol_version
            or type(value.get("owner_user_id")) is not int
            or value["owner_user_id"] != owner
            or value.get("task_id") != self.task_id
            or type(value.get("round")) is not int
            or value["round"] < 0
            or not isinstance(value.get("request_id"), str)
        ):
            raise NativeProcessError("native round authority differs")

    async def handle(self, value, process, *, closing=False):
        self.identity(value)
        if value['type'] == 'checkpoint_request':
            if self.checkpoint is None: raise NativeProcessError('native checkpoint policy missing')
            return await self.checkpoint.handle(value, process, closing=closing)
        if value['type'] == 'progress_request':
            if self.progress is None:
                raise NativeProcessError('native progress policy missing')
            return await self.progress.handle(value, process, closing=closing)
        if value["type"] == "round_request":
            if (
                self.pending is not None
                or type(value.get("history_messages")) is not int
                or value["history_messages"] < 0
                or value["round"] != self.next_round
            ):
                raise NativeProcessError("native round reservation order differs")
            deadline = value.get('deadline_monotonic_ns')
            if deadline is not None and (type(deadline) is not int or not 0 <= deadline <= 18446744073709551615):
                raise NativeProcessError('native preparation deadline invalid')
            messages = value.get('messages')
            if (self.prepare_history != ('messages' in value)
                    or (self.prepare_history and (not isinstance(messages, list)
                        or len(messages) != value['history_messages']
                        or any(not isinstance(row, dict) for row in messages)))):
                raise NativeProcessError('native preparation history differs')
            request = RoundRequest(
                self.client.principal,
                self.task_id,
                value["request_id"],
                value["round"],
                value["history_messages"],
                messages,
                deadline,
            )
            self.next_round += 1
            cancelled = False
            approved = False
            prepared = None
            if not closing:
                if self.prepare_history:
                    try:
                        preparation = asyncio.create_task(self.policy.prepare(request))
                        try:
                            prepared = await asyncio.shield(preparation)
                        except asyncio.CancelledError:
                            preparation.cancel()
                            try:
                                await _joined(preparation)
                            except asyncio.CancelledError:
                                pass  # Joined callback acknowledged cancellation.
                            raise
                        if not isinstance(prepared, list) or any(not isinstance(row, dict) for row in prepared):
                            raise TypeError('native preparation must return canonical messages')
                        # Certify the complete reply before acquiring a reservation.
                        self.client._encode({'version': self.client.protocol_version,
                            'type': 'round_decision', 'request_id': value['request_id'],
                            'approved': True, 'messages': prepared}, self.preparation_bytes)
                        request = RoundRequest(request.principal, request.task_id, request.request_id,
                                               request.round, len(prepared), prepared, request.deadline_monotonic_ns)
                    except Exception as error:
                        # Finish the native failure/terminal boundary before raising
                        # the host cause; cleanup Stop must not relabel this as abort.
                        self.preparation_failure = error
                        if deadline is not None and time.monotonic_ns() >= deadline:
                            # Native deadline already owns terminalization; its input
                            # carrier now accepts terminal ACK, not the old decision.
                            return
                        await self.send(process, 'round_decision', value['request_id'],
                                        approved=False, preparation_failed=True)
                        return
                try:
                    reserve = asyncio.create_task(self.policy.reserve(request))
                    try:
                        approved = await asyncio.shield(reserve)
                    except asyncio.CancelledError:
                        cancelled = True
                        approved = await _joined(reserve)
                except Exception as error:
                    raise NativeRoundError(
                        "native round reservation callback failed"
                    ) from error
            if type(approved) is not bool:
                raise NativeRoundError("native round admission must return bool")
            if approved:
                self.pending = request
                self.attempted = False
            if cancelled:
                # No decision was published: native exit produces Abandoned evidence.
                raise asyncio.CancelledError
            await self.send(
                process, "round_decision", value["request_id"], approved=approved,
                **({'messages': prepared} if self.prepare_history and approved else {}),
            )
            return
        request = self.pending
        if (
            request is None
            or value["request_id"] != request.request_id + "-settle"
            or value["round"] != request.round
            or not isinstance(value.get("evidence"), dict)
        ):
            raise NativeProcessError("native round settlement identity differs")
        evidence = value["evidence"]
        if evidence.get("kind") not in {
            "returned",
            "failed",
            "stopped",
            "not_dispatched",
            "abandoned",
        }:
            raise NativeProcessError("native round settlement evidence differs")
        self.attempted = True
        cancelled = False
        settlement_cancelled = threading.Event()
        if closing:
            settlement_cancelled.set()
        try:
            try:
                settle = asyncio.create_task(
                    self.policy.settle(RoundSettlement(request, evidence, settlement_cancelled))
                )
                try:
                    result = await asyncio.shield(settle)
                except asyncio.CancelledError:
                    cancelled = True
                    settlement_cancelled.set()
                    result = await _joined(settle)
                if result is not None:
                    raise TypeError("native settlement callback must return None")
            except Exception as error:
                try:
                    await self.acknowledge(process, value["request_id"], False)
                except (OSError, TimeoutError):
                    pass
                raise NativeRoundError(
                    "native round settlement callback failed"
                ) from error
            ack = asyncio.create_task(
                self.acknowledge(process, value["request_id"], True)
            )
            try:
                await asyncio.shield(ack)
            except asyncio.CancelledError:
                cancelled = True
                await _joined(ack)
            self.settled_progress_ready = evidence.get("kind") == "returned" and evidence.get("provider_state") == "provider_finished"
            self.settled_round = request.round
            self.settled_deadline = request.deadline_monotonic_ns
        finally:
            self.pending = None
        if cancelled:
            raise asyncio.CancelledError

    async def abandon(self):
        if self.pending is not None and not self.attempted:
            request = self.pending
            self.attempted = True
            try:
                result = await self.policy.settle(
                    RoundSettlement(request, {"kind": "abandoned"})
                )
                if result is not None:
                    raise TypeError("native settlement callback must return None")
            except Exception as error:
                raise NativeRoundError(
                    "native abandoned settlement callback failed"
                ) from error
            finally:
                self.pending = None


@dataclass(frozen=True, slots=True)
class TerminalRequest:
    principal: PrincipalContext
    task_id: str
    operation_id: str
    attempt: int
    event_json: bytes
    receipt_json: bytes

    @property
    def event(self) -> dict:
        return json.loads(self.event_json)

    @property
    def receipt(self) -> dict:
        return json.loads(self.receipt_json)


class TerminalPolicy(Protocol):
    async def persist(self, request: TerminalRequest) -> str: ...


@dataclass(frozen=True, slots=True)
class TextDelta:
    principal: PrincipalContext
    task_id: str
    sequence: int
    channel: str
    text: str


class _TextControl:
    def __init__(self, client, callback, task_id, byte_limit):
        self.client, self.callback, self.task_id = client, callback, task_id
        self.limit, self.bytes, self.sequence, self.failed = byte_limit, 0, 0, False

    async def handle(self, value, process):
        owner = self.client.principal.require_owner(context='native text')
        sequence = value.get('sequence')
        text = value.get('text')
        if (value.get('version') != self.client.protocol_version
                or type(value.get('owner_user_id')) is not int
                or value['owner_user_id'] != owner or value.get('task_id') != self.task_id
                or type(sequence) is not int or sequence != self.sequence + 1
                or value.get('request_id') != f'{self.task_id}-text-{sequence}'
                or value.get('channel') not in {'content', 'reasoning'}
                or not isinstance(text, str) or not text):
            raise NativeProcessError('native text identity/order differs')
        self.bytes += len(text.encode('utf-8'))
        if self.bytes > self.limit:
            raise NativeProcessError('native text output exceeds admission')
        self.sequence = sequence
        cancelled, failure = False, None
        if not self.failed:
            try:
                callback = asyncio.create_task(self.callback(TextDelta(
                    self.client.principal, self.task_id, sequence, value['channel'], text)))
                try:
                    result = await asyncio.shield(callback)
                except asyncio.CancelledError:
                    cancelled = True
                    result = await _joined(callback)
                if result is not None:
                    raise TypeError('native text callback must return None')
            except Exception as error:
                failure, self.failed = error, True
        async def acknowledge():
            async with asyncio.timeout(self.client.budget.shutdown_seconds):
                process.stdin.write(self.client._encode({
                    'version': self.client.protocol_version, 'type': 'text_ack',
                    'request_id': value['request_id'], 'accepted': not self.failed,
                }, self.client.budget.frame_bytes))
                await process.stdin.drain()
        ack = asyncio.create_task(acknowledge())
        try:
            try:
                await asyncio.shield(ack)
            except asyncio.CancelledError:
                cancelled = True
                await _joined(ack)
        except Exception as error:
            if failure is None:
                raise NativeProcessError('native text ACK failed') from error
        if failure is not None:
            raise NativeProcessError('native text callback failed') from failure
        if cancelled:
            raise asyncio.CancelledError


class _TerminalControl:
    def __init__(self, client, policy, task_id, profile, checkpoint=None):
        self.client, self.policy, self.task_id = client, policy, task_id
        self.checkpoint = checkpoint
        self.checkpoint_sealed = False
        self.operation = profile["operation_id"]
        self.limit = profile["attempts"]
        self.ordinal = 0
        self.frozen = None
        self.accepted = False

    def encode(self, value):
        return self.client._encode(value, self.client.budget.frame_bytes)

    async def handle(self, value, process):
        owner = self.client.principal.require_owner(context="native persistence")
        if (
            value.get("version") != self.client.protocol_version
            or type(value.get("owner_user_id")) is not int
            or value["owner_user_id"] != owner
            or value.get("task_id") != self.task_id
            or value.get("request_id") != self.operation
            or type(value.get("attempt")) is not int
            or value["attempt"] != self.ordinal + 1
            or value["attempt"] > self.limit
            or self.accepted
            or not isinstance(value.get("event"), dict)
            or not isinstance(value.get("receipt"), dict)
        ):
            raise NativePersistenceError("native persistence identity/order differs")
        suspended = value.get("checkpoint_suspended")
        if self.checkpoint is not None:
            if (type(suspended) is not bool
                    or (suspended and (not self.checkpoint.suspended or value["event"].get("status") != "aborted"))):
                raise NativePersistenceError("native checkpoint terminal disposition differs")
        elif suspended is not None:
            raise NativePersistenceError("native checkpoint terminal fence requires a policy")
        event, receipt = self.encode(value["event"]), self.encode(value["receipt"])
        if self.frozen is not None and self.frozen != (event, receipt, suspended):
            raise NativePersistenceError(
                "native persistence retry changed frozen candidate"
            )
        self.frozen = event, receipt, suspended
        self.ordinal += 1
        request = TerminalRequest(
            self.client.principal,
            self.task_id,
            self.operation,
            self.ordinal,
            event,
            receipt,
        )
        cancelled = False
        decision = "rejected"
        failure = None
        signal = threading.Event()
        async def persist():
            if self.checkpoint is not None and not self.checkpoint_sealed:
                checkpoint_request = CheckpointTerminalRequest(
                    self.client.principal, self.task_id, self.checkpoint.epoch,
                    self.checkpoint.binding, request, suspended, signal,
                )
                sealed = await self.checkpoint.policy.seal(checkpoint_request)
                if type(sealed) is not str or sealed not in {"accepted", "retry", "rejected"}:
                    raise TypeError("native checkpoint seal requires accepted/retry/rejected")
                if sealed != "accepted":
                    return sealed
                self.checkpoint_sealed = True
            return await self.policy.persist(request)
        try:
            callback = asyncio.create_task(persist())
            try:
                decision = await asyncio.shield(callback)
            except asyncio.CancelledError:
                cancelled = True
                signal.set()
                decision = await _joined(callback)
            if type(decision) is not str or decision not in {
                "accepted",
                "retry",
                "rejected",
            }:
                raise TypeError("native persistence requires accepted/retry/rejected")
            if decision == "retry" and self.ordinal >= self.limit:
                raise NativePersistenceError("native persistence retry bound exhausted")
        except Exception as error:
            failure = error

        async def acknowledge():
            async with asyncio.timeout(self.client.budget.shutdown_seconds):
                process.stdin.write(
                    self.encode(
                        dict(
                            version=self.client.protocol_version,
                            type="terminal_ack",
                            request_id=self.operation,
                            attempt=self.ordinal,
                            accepted=decision == "accepted" and failure is None,
                            retry=decision == "retry" and failure is None,
                        )
                    )
                )
                await process.stdin.drain()

        ack = asyncio.create_task(acknowledge())
        try:
            try:
                await asyncio.shield(ack)
            except asyncio.CancelledError:
                cancelled = True
                await _joined(ack)
        except OSError as error:
            raise NativePersistenceError(
                "native persistence ACK transport failed"
            ) from error
        self.accepted = decision == "accepted" and failure is None
        if failure is not None:
            raise NativePersistenceError(
                "native persistence callback failed"
            ) from failure
        if decision == "rejected":
            raise NativePersistenceError("native persistence was rejected")
        if cancelled:
            raise asyncio.CancelledError

    def confirm(self, event):
        if (
            not self.accepted
            or self.frozen is None
            or self.encode(event) != self.frozen[0]
        ):
            raise NativePersistenceError(
                "native terminal preceded or differs from persisted candidate"
            )


@lru_cache(maxsize=1)
def _model_content_validators():
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import MODEL_CONTENT_SCHEMAS
    return {kind:Draft202012Validator(schema) for kind,schema in MODEL_CONTENT_SCHEMAS.items()}


def validate_model_content(content):
    """Validate declared canonical text/image blocks; never fetch/resolve media."""
    if not isinstance(content,list) or not content:
        raise TypeError('native model content requires nonempty blocks')
    validators=_model_content_validators()
    for block in content:
        if not isinstance(block,dict) or block.get('type') not in validators:
            raise TypeError('native model content block unsupported')
        if next(validators[block['type']].iter_errors(block),None) is not None:
            raise TypeError('native model content block invalid')
    return content


ToolCallback = Callable[[ToolRequest], Awaitable[str | list[dict]]]


# Linux UAPI fallbacks for Python built against older libc/kernel headers.
# Authority: linux/v5.10 include/uapi/linux/fcntl.h and syscall tables.
_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_WRITE_SEAL = getattr(fcntl, "F_SEAL_WRITE", 8)
_ALL_SEALS = 1 | 2 | 4 | 8


def _memfd() -> int:
    if hasattr(os, "memfd_create"):
        return os.memfd_create("tofu-native", 1 | 2)
    number = {"x86_64": 319, "aarch64": 279}.get(os.uname().machine)
    if number is None:
        raise NativeConfigurationError("native memfd syscall ABI is unsupported")
    import ctypes

    if ctypes.sizeof(ctypes.c_long) != 8:
        raise NativeConfigurationError(
            "native memfd syscall requires a known 64-bit ABI"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    descriptor = syscall(
        ctypes.c_long(number), ctypes.c_char_p(b"tofu-native"), ctypes.c_uint(1 | 2)
    )
    if descriptor < 0:
        raise NativeProcessError("native sealed execution is unavailable")
    return int(descriptor)


def _seal(binary: Path, expected: str, limit: int) -> BinaryIO:
    """Verify then seal the exact bytes executed, preventing path replacement."""
    image = _memfd()
    try:
        hasher = hashlib.sha256()
        total = 0
        with binary.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                raise NativeConfigurationError(
                    "native executable must be an ELF artifact"
                )
            source.seek(0)
            while chunk := source.read(65536):
                total += len(chunk)
                if total > limit:
                    raise NativeConfigurationError(
                        "native executable byte budget exceeded"
                    )
                hasher.update(chunk)
                offset = 0
                while offset < len(chunk):
                    written = os.write(image, chunk[offset:])
                    if written <= 0:
                        raise NativeProcessError("native executable copy failed")
                    offset += written
        if hasher.hexdigest() != expected:
            raise NativeConfigurationError("native executable checksum differs")
        fcntl.fcntl(image, _ADD_SEALS, _ALL_SEALS)
        return os.fdopen(image, "rb", buffering=0)
    except BaseException:
        os.close(image)
        raise


async def _joined(task: asyncio.Future):
    """Repeated cancellation cannot abandon a started acquisition/cleanup."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class NativeProcessClient:
    """One-loop bounded host adapter for a separately verified native artifact.

    Hash and protocol identity come from a trusted installed archive manifest,
    never model configuration. Tool callbacks must quiesce when cancelled.
    """

    def __init__(
        self,
        *,
        binary: Path,
        sha256: str,
        protocol_version: str,
        principal: PrincipalContext,
        budget: Budget,
    ) -> None:
        if sys.platform != "linux":
            raise NativeConfigurationError(
                "native host adapter requires Linux memfd execution"
            )
        if not isinstance(principal, PrincipalContext):
            raise NativeConfigurationError("native host requires an explicit principal")
        principal.require_owner(context="native process client")
        if not isinstance(binary, Path) or not binary.is_absolute():
            raise NativeConfigurationError("native executable path must be absolute")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise NativeConfigurationError("native executable checksum must be SHA-256")
        if not protocol_version:
            raise NativeConfigurationError("native protocol identity is required")
        if not isinstance(budget, Budget):
            raise NativeConfigurationError("native host requires an explicit budget")
        self.principal = principal
        self.budget = budget
        self.binary = binary
        self.sha256 = sha256
        self.protocol_version = protocol_version
        self._loop = None
        self._runs: dict[asyncio.Task, asyncio.Future] = {}
        self._image: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None
        self._closed = False

    @property
    def active_runs(self) -> int:
        return len(self._runs)

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise NativeProcessError("native host client belongs to another event loop")
        self._loop = loop

    def _encode(self, value: Any, limit: int) -> bytes:
        # Bound structure before the encoder can allocate a huge single string.
        stack = [iter((value,))]
        lower_bytes = 0
        while stack:
            try:
                item = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            lower_bytes += 1
            if isinstance(item, str):
                lower_bytes += len(item)
            elif type(item) is int:
                lower_bytes += max(0, (item.bit_length() - 1) // 4 - 1)
            if lower_bytes > limit or len(stack) > 128:
                raise NativeConfigurationError(
                    "native frame structure exceeded its budget"
                )
            if type(item) is int and item.bit_length() > limit * 4:
                raise NativeConfigurationError(
                    "native frame integer exceeded its budget"
                )
            if isinstance(item, str) and len(item) > limit:
                raise NativeConfigurationError(
                    "native frame string exceeded its budget"
                )
            if isinstance(item, dict):

                def fields(document):
                    for key, child in document.items():
                        yield key
                        yield child

                stack.append(fields(item))
            elif isinstance(item, (list, tuple)):
                stack.append(iter(item))
        try:
            raw = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (ValueError, TypeError, UnicodeError) as error:
            raise NativeConfigurationError("native frame cannot be encoded") from error
        if len(raw) > limit:
            raise NativeConfigurationError("native frame byte budget exceeded")
        return raw + b"\n"

    async def _shutdown(
        self, process: asyncio.subprocess.Process, rounds=None, terminal=None, text=None, approvals=None, tool_failed=False, model=None
    ) -> None:
        async def discard() -> None:
            count = 0
            while True:
                if rounds is None and terminal is None and text is None and approvals is None and model is None:
                    chunk = await process.stdout.read(4096)
                else:
                    chunk = await process.stdout.readline()
                if not chunk:
                    return
                count += len(chunk)
                close_bytes = self.budget.frame_bytes * (1 + (text.limit if text is not None else 0))
                if count > close_bytes:
                    raise NativeProcessError(
                        "native shutdown output exceeded its budget"
                    )
                if rounds is not None or terminal is not None or text is not None or approvals is not None:
                    try:
                        value = json.loads(chunk)
                    except ValueError as error:
                        raise NativeProcessError(
                            "native shutdown protocol failed"
                        ) from error
                    if isinstance(value,dict) and value.get('type') == 'model_request':
                        if model is None:
                            raise NativeProcessError('missing model policy during close')
                        await model.handle(value,process,closing=True)
                    elif isinstance(value, dict) and value.get('type') == 'approval_request':
                        if approvals is None:
                            raise NativeProcessError('missing approval policy during close')
                        await approvals.handle(value, process)
                    elif isinstance(value, dict) and value.get('type') == 'text_delta':
                        if text is None:
                            raise NativeProcessError('missing text consumer during close')
                        await text.handle(value, process)
                    elif isinstance(value, dict) and value.get("type") in {
                        "round_request",
                        "round_settled",
                        "progress_request",
                        "checkpoint_request",
                    }:
                        if rounds is None:
                            raise NativeProcessError(
                                "missing round policy during close"
                            )
                        await rounds.handle(value, process, closing=True)
                    elif (
                        isinstance(value, dict)
                        and value.get("type") == "terminal_request"
                    ):
                        if terminal is None:
                            raise NativePersistenceError(
                                "missing persistence policy during close"
                            )
                        await terminal.handle(value, process)

        drain = asyncio.create_task(discard())
        try:
            async with asyncio.timeout(self.budget.shutdown_seconds):
                if process.returncode is None and process.stdin is not None and not (
                        tool_failed or (text is not None and text.failed) or (approvals is not None and approvals.failed)):
                    try:
                        process.stdin.write(
                            self._encode(
                                {"version": self.protocol_version, "type": "cancel"},
                                self.budget.frame_bytes,
                            )
                        )
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    if rounds is None and terminal is None and text is None and approvals is None and model is None:
                        process.stdin.close()
                await process.wait()
                await drain
        except (TimeoutError, NativeProcessError, OSError, ValueError):
            if process.returncode is None:
                process.kill()
            await process.wait()
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            if process.stdin is not None:
                process.stdin.close()

    async def run(
        self,
        configuration: Mapping[str, Any],
        *,
        callbacks: Mapping[str, ToolCallback],
        working_directory: Path,
        round_policy: RoundPolicy | None = None,
        progress_policy: ProgressPolicy | None = None,
        checkpoint_policy: CheckpointPolicy | None = None,
        terminal_policy: TerminalPolicy | None = None,
        on_text: Callable[[TextDelta], Awaitable[None]] | None = None,
        approval_policy: ApprovalPolicy | None = None,
        model_policy=None,
    ) -> dict:
        self._bind_loop()
        if self._closed:
            raise NativeProcessError("native host client is closed")
        if (
            not isinstance(working_directory, Path)
            or not working_directory.is_absolute()
        ):
            raise NativeConfigurationError("native working directory must be absolute")
        config = dict(configuration)
        owner = self.principal.require_owner(context="native process run")
        if (
            type(config.get("owner_user_id")) is not int
            or config["owner_user_id"] != owner
        ):
            raise NativeConfigurationError(
                "native configuration owner differs from principal"
            )
        if (
            config.get("version") != self.protocol_version
            or config.get("type") != "configure"
        ):
            raise NativeConfigurationError("native configuration protocol differs")
        if config.get('tool_feedback','text') not in {'text','multimodal'}:
            raise NativeConfigurationError('native tool feedback mode invalid')
        tools = config.get("tools")
        handlers = dict(callbacks)
        if (
            not isinstance(tools, dict)
            or set(tools) != set(handlers)
            or any(
                not isinstance(t, dict) or t.get("permission") not in {'read','write','approval'}
                for t in tools.values()
            )
            or any(not callable(handler) for handler in handlers.values())
        ):
            raise NativeConfigurationError("native read-tool catalog/callbacks differ")
        task_id = config.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise NativeConfigurationError("native task identity is required")
        if handlers:
            config['tool_result_join'] = True
        profile = config.get('approval_control')
        if approval_policy is None and (profile is not None or any(t['permission'] != 'read' for t in tools.values())):
            raise NativeConfigurationError('non-read tools require explicit approval policy')
        approvals = None
        if approval_policy is not None:
            if (not callable(getattr(approval_policy,'decide',None)) or not isinstance(profile,dict)
                    or any(type(profile.get(key)) is not int or profile[key] <= 0 for key in ('receipts','identity_bytes'))
                    or not isinstance(config.get('epoch'),str) or not config['epoch']):
                raise NativeConfigurationError('native approval composition is invalid')
            approvals = _ApprovalControl(self, approval_policy, task_id, config['epoch'], tools, profile)
        text = None
        if on_text is not None:
            if (not callable(on_text)
                    or any(type(config.get(name)) is not int or config[name] <= 0
                           for name in ('output_bytes', 'rounds'))):
                raise NativeConfigurationError('native text requires bounded explicit consumer')
            config['text_progress'] = True
            text = _TextControl(self, on_text, task_id, config['output_bytes'] * config['rounds'])
        elif config.get('text_progress') is True:
            raise NativeConfigurationError('native text progress requires a consumer')
        if round_policy is not None:
            if not all(
                callable(getattr(round_policy, name, None))
                for name in ("reserve", "settle")
            ):
                raise NativeConfigurationError(
                    "native round policy requires reserve and settle"
                )
            prepare_history = callable(getattr(round_policy, 'prepare', None))
            if config.get('round_preparation') is True and not prepare_history:
                raise NativeConfigurationError('native preparation requires a prepare callback')
            config['round_preparation'] = prepare_history
            if prepare_history:
                size = config.setdefault('round_preparation_bytes', self.budget.frame_bytes)
                if type(size) is not int or not 0 < size <= self.budget.frame_bytes:
                    raise NativeConfigurationError('native preparation frame bound invalid')
            config["round_control"] = True
        elif config.get("round_control") is True or config.get("round_preparation") is True:
            raise NativeConfigurationError(
                "native round control requires an explicit policy"
            )
        if progress_policy is not None:
            if round_policy is None or not isinstance(getattr(progress_policy, 'limits', None), ProgressLimits) or not callable(getattr(progress_policy, 'observe', None)):
                raise NativeConfigurationError('native progress requires explicit round and observation policies')
            config['progress_control'] = True
            config['no_progress_rounds'] = progress_policy.limits.no_progress_rounds
            config['nonretryable_failure_rounds'] = progress_policy.limits.nonretryable_failure_rounds
        elif config.get('progress_control') is True or any(config.get(key, 0) != 0 for key in ('no_progress_rounds', 'nonretryable_failure_rounds')):
            raise NativeConfigurationError('native progress configuration requires a policy')
        if checkpoint_policy is not None:
            if (round_policy is None or not isinstance(getattr(checkpoint_policy, 'binding', None), str)
                    or not checkpoint_policy.binding or not all(callable(getattr(checkpoint_policy, action, None)) for action in ('load', 'begin', 'complete'))):
                raise NativeConfigurationError('native checkpoint requires explicit round policy and binding')
            config['checkpoint_control'] = True
            config['checkpoint_binding'] = checkpoint_policy.binding
            config['checkpoint_frame_bytes'] = self.budget.frame_bytes
        elif config.get('checkpoint_control') is True or config.get('checkpoint_binding') is not None:
            raise NativeConfigurationError('native checkpoint configuration requires a policy')
        model = None
        if model_policy is not None:
            from lib.agent_core.native_model_control import ModelControl
            if (not all(callable(getattr(model_policy,action,None)) for action in ('reserve','dispatch','settle'))
                    or not isinstance(config.get('model_control'),dict) or not isinstance(config.get('model_counter'),dict)
                    or 'token_admission' in config):
                raise NativeConfigurationError('native model control requires one shared authority/counter and explicit recovery')
            model = ModelControl(self,model_policy,task_id)
        elif 'model_control' in config or 'model_counter' in config:
            raise NativeConfigurationError('native model control requires a policy')
        rounds = (
            _RoundControl(self, round_policy, task_id, config.get("round_preparation") is True,
                          config.get("round_preparation_bytes"), progress_policy, checkpoint_policy, config.get("epoch"))
            if round_policy is not None
            else None
        )
        shared = config.get('checkpoint_shared_account') is True
        if model is not None and checkpoint_policy is not None:
            if (not shared or not all(callable(getattr(model_policy,name,None)) for name in ('capture_checkpoint','restore_checkpoint'))
                    or not isinstance(config.get('template'),dict) or not isinstance(config['template'].get('model'),str)):
                raise NativeConfigurationError('shared checkpoint requires explicit account capture/recovery')
            model.counter_policy = dict(encoding=config['model_counter'].get('encoding'),output_field=config['model_counter'].get('output_field'),wire_model=config['template']['model'])
            rounds.checkpoint.model = model
        elif shared:
            raise NativeConfigurationError('shared checkpoint flag requires both policies')
        profile = config.get("terminal_persistence")
        if terminal_policy is None and profile is not None:
            raise NativeConfigurationError(
                "native terminal persistence requires a policy"
            )
        terminal = None
        if terminal_policy is not None:
            if (
                not callable(getattr(terminal_policy, "persist", None))
                or not isinstance(profile, dict)
                or not isinstance(profile.get("operation_id"), str)
                or not profile["operation_id"]
                or any(
                    type(profile.get(name)) is not int or profile[name] <= 0
                    for name in ("ack_ms", "attempts", "frame_bytes")
                )
                or profile["frame_bytes"] > self.budget.frame_bytes
            ):
                raise NativeConfigurationError(
                    "native persistence profile/policy is invalid"
                )
            checkpoint = rounds.checkpoint if rounds is not None else None
            if checkpoint is not None:
                if not callable(getattr(checkpoint_policy, "seal", None)):
                    raise NativeConfigurationError("native durable checkpoint requires an explicit seal callback")
                config["checkpoint_terminal_fence"] = True
            terminal = _TerminalControl(self, terminal_policy, task_id, profile, checkpoint)
        if config.get("checkpoint_terminal_fence") is True and (terminal is None or terminal.checkpoint is None):
            raise NativeConfigurationError("native checkpoint terminal fence requires both policies")
        raw = self._encode(config, self.budget.configuration_bytes)
        task = asyncio.current_task()
        tool_failed = False
        if task in self._runs:
            raise NativeProcessError("native host runs cannot reenter the same task")
        if task is None:
            raise NativeProcessError("native run requires an owning async task")
        self._runs[task] = asyncio.get_running_loop().create_future()
        process = None
        spawn = None
        try:
            if self._image is None:

                async def load_image():
                    worker = asyncio.get_running_loop().run_in_executor(
                        None, _seal, self.binary, self.sha256, self.budget.binary_bytes
                    )
                    return await _joined(worker)

                self._image = asyncio.create_task(load_image())
            image = await asyncio.shield(self._image)
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    f"/proc/self/fd/{image.fileno()}",
                    "run-http-agent",
                    str(self.budget.configuration_bytes),
                    pass_fds=(image.fileno(),),
                    cwd=working_directory,
                    env={"PATH": ""},
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=self.budget.frame_bytes + 1,
                )
            )
            process = await asyncio.shield(spawn)
            process.stdin.write(raw)
            await process.stdin.drain()
            while True:
                try:
                    line = await process.stdout.readline()
                    if (
                        not line
                        or not line.endswith(b"\n")
                        or len(line) > self.budget.frame_bytes + 1
                    ):
                        raise NativeProcessError(
                            "native output frame is missing or oversized"
                        )
                    value = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    raise NativeProcessError("native output protocol failed") from error
                if not isinstance(value, dict):
                    raise NativeProcessError("native output must be an object")
                if value.get('type') == 'model_request':
                    if model is None:
                        raise NativeProcessError('native model policy was not supplied')
                    await model.handle(value,process)
                    continue
                if value.get("type") == "terminal_request":
                    if model is not None and model.pending is not None:
                        if model.failure is None:
                            raise NativeProcessError('native terminal preceded model settlement')
                        await _joined(asyncio.create_task(model.abandon()))
                    if terminal is None:
                        raise NativePersistenceError(
                            "native persistence policy was not supplied"
                        )
                    await terminal.handle(value, process)
                    continue
                if value.get('type') == 'approval_request':
                    if approvals is None:
                        raise NativeProcessError('native approval policy was not supplied')
                    await approvals.handle(value, process)
                    continue
                if value.get('type') == 'text_delta':
                    if text is None:
                        raise NativeProcessError('native text consumer was not supplied')
                    await text.handle(value, process)
                    continue
                if value.get("type") in {"round_request", "round_settled", "progress_request", "checkpoint_request"}:
                    if rounds is None:
                        raise NativeProcessError("native round policy was not supplied")
                    await rounds.handle(value, process)
                    continue
                if value.get("type") != "tool_request":
                    if model is not None and model.pending is not None and model.failure is None:
                        raise NativeProcessError('native terminal preceded model settlement')
                    if model is not None and model.failure is not None:
                        raise NativeProcessError('native model callback failed') from model.failure
                    if rounds is not None and rounds.pending is not None:
                        raise NativeProcessError(
                            "native terminal preceded round settlement"
                        )
                    status = value.get("status")
                    if (
                        status not in {"done", "error", "aborted"}
                        or value.get("type") != status
                    ):
                        raise NativeProcessError("native terminal event is invalid")
                    if terminal is not None:
                        terminal.confirm(value)
                    code = await process.wait()
                    if (code == 0) != (status in {"done", "aborted"}):
                        raise NativeProcessError(
                            "native terminal and process exit differ"
                        )
                    if rounds is not None and rounds.checkpoint is not None and rounds.checkpoint.failure is not None:
                        raise NativeRoundError('native checkpoint policy failed') from rounds.checkpoint.failure
                    if rounds is not None and rounds.progress is not None and rounds.progress.failure is not None:
                        raise NativeRoundError('native progress observation callback failed') from rounds.progress.failure
                    if rounds is not None and rounds.preparation_failure is not None:
                        raise NativeRoundError('native round preparation callback failed') from rounds.preparation_failure
                    return value
                if (
                    value.get("version") != self.protocol_version
                    or type(value.get("owner_user_id")) is not int
                    or value.get("owner_user_id") != owner
                    or value.get("task_id") != task_id
                    or not isinstance(value.get("request_id"), str)
                    or not isinstance(value.get("arguments"), dict)
                    or not isinstance(value.get("name"), str)
                    or value.get("name") not in handlers
                    or ("model_round" in value and (type(value["model_round"]) is not int
                        or value["model_round"] < 0))
                ):
                    raise NativeProcessError("native tool request authority differs")
                callback = None
                stopped = asyncio.Event()
                callback_cancelled = False
                tool_error = None
                try:
                    if approvals is not None:
                        approvals.consume(value)
                    callback = asyncio.create_task(handlers[value["name"]](
                        ToolRequest(
                            self.principal,
                            task_id,
                            value["request_id"],
                            value["name"],
                            value["arguments"],
                            value.get('call_id'), value.get('caller'),
                            stopped, value.get("model_round"),
                        )
                    ))
                    callback_cancelled = False
                    try:
                        content = await asyncio.shield(callback)
                    except asyncio.CancelledError:
                        callback_cancelled = True
                        stopped.set()
                        async def stop_tool():
                            process.stdin.write(self._encode({'version':self.protocol_version,
                                'type':'cancel'},self.budget.frame_bytes))
                            await process.stdin.drain()
                        await _joined(asyncio.create_task(stop_tool()))
                        content = await _joined(callback)
                except Exception as error:
                    tool_error, tool_failed, content = error, True, ''
                finally:
                    if callback is not None and not callback.done():
                        stopped.set()
                        try:
                            await _joined(callback)
                        except Exception:
                            pass
                if not isinstance(content, str):
                    try:
                        if config.get('tool_feedback','text')!='multimodal':
                            raise TypeError('native tool callback must return text')
                        validate_model_content(content)
                    except (TypeError,ValueError) as error:
                        tool_error,tool_failed,content=error,True,''
                reply = self._encode(
                    {
                        "version": self.protocol_version,
                        "type": "tool_result",
                        "request_id": value["request_id"],
                        "content": content,
                        **({'failed':True} if tool_error is not None else {}),
                    },
                    min(self.budget.frame_bytes, config["tool_frame_bytes"]),
                )
                async def send_result():
                    async with asyncio.timeout(self.budget.shutdown_seconds):
                        process.stdin.write(reply)
                        await process.stdin.drain()
                reply_task = asyncio.create_task(send_result())
                try:
                    await asyncio.shield(reply_task)
                except asyncio.CancelledError:
                    callback_cancelled = True
                    stopped.set()
                    await _joined(reply_task)
                if callback_cancelled:
                    raise asyncio.CancelledError
                if tool_error is not None:
                    raise NativeToolError('native host tool callback failed') from tool_error
        except OSError as error:
            raise NativeProcessError("native process transport failed") from error
        finally:
            try:
                if self._image is not None:
                    try:
                        await _joined(self._image)
                    except Exception:
                        pass
                if process is None and spawn is not None:
                    process = await _joined(spawn)
                if process is not None:
                    await _joined(
                        asyncio.create_task(self._shutdown(process, rounds, terminal, text, approvals, tool_failed, model))
                    )
                if model is not None:
                    import sys
                    primary = sys.exception()
                    try:
                        await _joined(asyncio.create_task(model.abandon()))
                    except BaseException as cleanup_error:
                        if primary is None:
                            raise
                        primary.add_note('native model cleanup failed: ' + type(cleanup_error).__name__)
                if rounds is not None:
                    await _joined(asyncio.create_task(rounds.abandon()))
            finally:
                done = self._runs.pop(task)
                done.set_result(None)

    async def _close_owned(self) -> None:
        runs = list(self._runs.items())
        for task, _ in runs:
            task.cancel()
        if runs:
            await _joined(asyncio.gather(*(done for _, done in runs)))
        if self._image is not None:
            image = self._image
            self._image = None
            if not image.cancelled():
                try:
                    descriptor = await _joined(image)
                except Exception:
                    return
                descriptor.close()

    async def close(self) -> None:
        self._bind_loop()
        if asyncio.current_task() in self._runs:
            raise NativeProcessError("native host callback cannot close its own client")
        self._closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close_owned())
        await _joined(self._closing)
