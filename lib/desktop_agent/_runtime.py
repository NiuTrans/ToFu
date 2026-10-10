"""Demand-driven execution, spooled transport and negotiated replay deadlines.

Legacy execution stays demand-driven. Negotiated server delivery uses advisory
CPU/memory slots; finite replay records retire only after expiry and output ACK.
Spool and execution owners join at shutdown before temporary output is reclaimed.
"""
import json
import heapq
import math
import time
from dataclasses import dataclass
import random
import threading

from lib.desktop_wire import limit, protocol_version
from lib.log import get_logger

logger = get_logger(__name__)


class Outbox:
    """Spool pending transport frames; limits describe one poll, never admission.

    Launch-probed memory retains pending frames; overflow creates a private
    temporary directory lazily. Files disappear after acknowledgement or
    runtime teardown. Actual storage errors propagate; nothing is evicted.
    The caller's condition owns publication and prefix acknowledgement.
    """
    def __init__(self, condition, stop, max_items, max_bytes, *, memory_bytes=0):
        from collections import deque
        self.condition, self.stop = condition, stop
        self.max_items, self.max_bytes = max_items, max_bytes
        self._records = deque()
        self._pending_ids = {}
        self._temporary = None
        self._sequence = 0
        self._closed = False
        self.bytes = 0
        self._memory_budget = max(0, int(memory_bytes))
        self._resident_bytes = 0

    def append(self, item, *, cancel=None):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        encoded = json.dumps(item, ensure_ascii=True, allow_nan=False, default=str).encode('utf-8')
        if len(encoded) > self.max_bytes:
            raise ValueError('Desktop transport frame exceeds poll byte budget')
        with self.condition:
            if self._closed or self.stop.is_set():
                raise RuntimeError('Desktop transport stopped before delivery')
            if cancel is not None and cancel.is_set():
                raise RuntimeError('Desktop transfer cancelled before delivery')
            if self._resident_bytes + len(encoded) <= self._memory_budget:
                storage = encoded
            else:
                if self._temporary is None:
                    self._temporary = TemporaryDirectory(prefix='tofu-desktop-outbox-')
                self._sequence += 1
                storage = Path(self._temporary.name) / str(self._sequence)
                try:
                    storage.write_bytes(encoded)
                except BaseException:
                    storage.unlink(missing_ok=True)
                    raise
            try:
                self._records.append((storage, len(encoded), item.get('id') or item.get('cmd_id')))
            except BaseException:
                if not isinstance(storage, bytes):
                    storage.unlink(missing_ok=True)
                raise
            if isinstance(storage, bytes):
                self._resident_bytes += len(encoded)
            self.bytes += len(encoded)
            command_id = item.get('id') or item.get('cmd_id')
            if command_id:
                self._pending_ids[command_id] = self._pending_ids.get(command_id, 0) + 1

    def prefix(self, max_items, max_bytes):
        with self.condition:
            batch, size = [], 0
            for storage, length, _ in self._records:
                if len(batch) >= min(self.max_items, max_items) or size + length > min(self.max_bytes, max_bytes):
                    break
                batch.append(json.loads(storage if isinstance(storage, bytes) else storage.read_bytes()))
                size += length
            return batch

    def __iter__(self):
        return iter(self.prefix(self.max_items, self.max_bytes))

    def pending_ids(self):
        with self.condition:
            return set(self._pending_ids)

    def __delitem__(self, key):
        if (not isinstance(key, slice) or key.start not in (None, 0)
                or key.step not in (None, 1) or key.stop is None or key.stop < 0):
            raise ValueError('Desktop outbox acknowledgements must name a prefix')
        with self.condition:
            for _ in range(min(key.stop, len(self._records))):
                storage, length, command_id = self._records[0]
                if isinstance(storage, bytes):
                    self._resident_bytes -= length
                else:
                    storage.unlink(missing_ok=True)
                self._records.popleft()
                if command_id:
                    self._pending_ids[command_id] -= 1
                    if not self._pending_ids[command_id]:
                        del self._pending_ids[command_id]
                self.bytes -= length
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            self._records.clear()
            self._pending_ids.clear()
            self.bytes = self._resident_bytes = 0
            self.condition.notify_all()


def outbox_memory_budget():
    """Reuse one cached launch probe; overflow spills instead of refusing work."""
    try:
        from runtime_guards import probe_system_resources
        snapshot = probe_system_resources()
        capacity = snapshot.effective_memory_capacity_mb
        headroom = snapshot.effective_memory_available_mb
        if capacity is None or headroom is None:
            return 1024 * 1024
        return max(1, min(capacity, headroom) // 128) * 1024 * 1024
    except Exception as exc:
        logger.debug('Desktop outbox probe failed: %s', type(exc).__name__)
        return 1024 * 1024


@dataclass(frozen=True)
class ExecutionBudget:
    workers: int
    replay_ids: int
    result_items: int
    result_bytes: int
    stream_items: int
    stream_bytes: int


def execution_budget():
    """One cached launch probe; headroom constrains automatic resident budgets."""
    cpu, memory_mb = 1, limit('memory_mb_per_worker')
    try:
        from runtime_guards import probe_system_resources
        snapshot = probe_system_resources()
        cpu = max(1, int(snapshot.effective_cpu_count))
        memory_mb = max(1, int(min(snapshot.effective_memory_capacity_mb or memory_mb,
                                  snapshot.effective_memory_available_mb or memory_mb)))
    except Exception as exc:
        logger.debug('resource probe failed, lean worker fallback: %s', type(exc).__name__)
    workers = min(cpu, max(1, memory_mb // limit('memory_mb_per_worker')))
    resident_bytes = outbox_memory_budget()
    return ExecutionBudget(
        workers=workers,
        replay_ids=max(limit('legacy_execution_ids'), workers * limit('execution_ids_per_worker')),
        result_items=limit('result_items_floor'),
        result_bytes=resident_bytes * 7 // 8,
        stream_items=limit('stream_items_floor'),
        stream_bytes=resident_bytes // 8,
    )


def execution_capacity():
    return execution_budget().workers


class RuntimeState:
    def __init__(self, stop, *, budget=None, clock=time.monotonic):
        self.stop = stop
        self.lock = threading.Condition(threading.RLock())
        self.budget = budget if budget is not None else execution_budget()
        self.results = Outbox(self.lock, stop, limit('result_items_floor'), limit('result_bytes_floor'),
                              memory_bytes=self.budget.result_bytes)
        self.streams = Outbox(self.lock, stop, limit('stream_items_floor'), limit('stream_bytes_floor'),
                              memory_bytes=self.budget.stream_bytes)
        self.seen, self.running = set(), set()
        self._deadlines, self._expiry_heap = {}, []
        self._clock = clock
        self._server_anchor = self._clock_anchor = None

    def _server_now(self):
        if self._server_anchor is None:
            return None
        return self._server_anchor + max(0, self._clock() - self._clock_anchor)

    def observe_server_time(self, server_time, *, request_elapsed=0):
        with self.lock:
            current = self._server_now()
            self._server_anchor = max(server_time + max(0, request_elapsed), current or server_time)
            self._clock_anchor = self._clock()
            self._reclaim()

    def _reclaim(self):
        now = self._server_now()
        if now is None:
            return
        pending = self.results.pending_ids() | self.streams.pending_ids()
        protected = []
        while self._expiry_heap and self._expiry_heap[0][0] <= now:
            deadline, cmd_id = heapq.heappop(self._expiry_heap)
            if cmd_id in self.running or cmd_id in pending:
                protected.append((deadline, cmd_id))
            else:
                self.seen.discard(cmd_id)
                self._deadlines.pop(cmd_id, None)
        for entry in protected:
            heapq.heappush(self._expiry_heap, entry)

    def expired(self, expires_at):
        with self.lock:
            now = self._server_now()
            return expires_at is not None and (now is None or expires_at <= now)

    def available_slots(self):
        with self.lock:
            self._reclaim()
            if self.stop.is_set():
                return 0
            return max(0, min(self.budget.workers - len(self.running),
                              self.budget.replay_ids - len(self._deadlines)))

    def admit(self, cmd_id, *, expires_at=None):
        with self.lock:
            self._reclaim()
            if self.stop.is_set() or cmd_id in self.seen:
                return False
            if self.expired(expires_at):
                self.results.append({'id': cmd_id, 'result': None,
                                     'error': 'Command expired before desktop execution'})
                return False
            self.seen.add(cmd_id)
            if expires_at is not None:
                self._deadlines[cmd_id] = expires_at
                heapq.heappush(self._expiry_heap, (expires_at, cmd_id))
            self.running.add(cmd_id)
            return True

    def finish(self, cmd_id):
        with self.lock:
            self.running.discard(cmd_id)
            self.lock.notify_all()

    def close(self):
        self.stop.set()
        with self.lock:
            self.lock.wait_for(lambda: not self.running)
            self.results.close()
            self.streams.close()
            self.seen.clear()
            self._deadlines.clear()
            self._expiry_heap.clear()


class ProtocolMismatch(ValueError):
    """A reached server requires an incompatible wire version."""


def validate_poll_response(data, *, agent_id=None, require_v3=False):
    """Validate the complete command batch before acknowledging any outbox."""
    if not isinstance(data, dict) or not isinstance(data.get('commands'), list):
        raise ValueError('Invalid desktop poll envelope')
    version = data.get('protocol_version')
    negotiated = version is not None
    if (negotiated and (type(version) is not int or version != protocol_version())) or (require_v3 and not negotiated):
        raise ProtocolMismatch('Unsupported desktop bridge protocol; upgrade client and server')
    if negotiated and not _timestamp(data.get('server_time')):
        raise ValueError('Invalid desktop server clock')
    commands = data['commands']
    if len(commands) > limit('command_batch'):
        raise ValueError('Desktop command batch exceeds admission budget')
    for cmd in commands:
        if (not isinstance(cmd, dict) or not isinstance(cmd.get('id'), str)
                or not cmd['id'] or len(cmd['id']) > 256
                or not isinstance(cmd.get('type'), str) or not cmd['type']
                or not isinstance(cmd.get('params', {}), dict)):
            raise ValueError('Invalid desktop command envelope')
        if cmd.get('target_agent_id') and agent_id is not None and cmd['target_agent_id'] != agent_id:
            raise ValueError('Desktop command addressed to a different device')
        if negotiated and (not _timestamp(cmd.get('expires_at'))
                           or not isinstance(cmd.get('target_agent_id'), str)
                           or not cmd['target_agent_id']):
            raise ValueError('Invalid desktop command lifetime or device')
    return commands


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def retry_delay(interval, failures):
    ceiling = min(max(float(interval), 0.001) * 2 ** min(failures, 8), 60.0)
    return random.uniform(ceiling / 2, ceiling)
