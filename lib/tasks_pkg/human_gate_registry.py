"""Bounded, owner-aware registry for blocking human decisions.

The three task-facing human gates differ in wait policy, but their mutable
state machine is identical: register one waiter, accept exactly one answer,
and let the waiter atomically consume or abandon it.  Centralizing that state
machine prevents race fixes and authorization rules from drifting between
stdin, guidance, and write approval.

Entries remain process-local because an Event cannot cross replicas, so the
registry mirrors them onto ``lib/runtime_state_store`` in DISTRIBUTED mode:
a ``human-gate-req`` flag records the pending waiter (owner-scoped) and a
``human-gate-ans`` flag carries the answer (first-writer-wins via
``set_value_nx``). A resolve landing on the wrong replica plants the answer;
the waiting replica adopts it through :meth:`poll_store_resolution`, which
re-validates ownership and delivers through the same entry — so the local
state machine below stays the single authority for who consumed what.
Personal mode is byte-equivalent: no store reads, no new threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
from typing import Generic, TypeVar

from lib.human_gate_contract import require_human_gate_request_id
from lib.identity import require_user_id
from lib.log import get_logger
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)

ResponseT = TypeVar('ResponseT')

_REQ_FLAG_KIND = 'human-gate-req'
_ANS_FLAG_KIND = 'human-gate-ans'
_FLAG_TTL_S = 6 * 3600.0
_MAX_ANSWER_BYTES = 16384


def _distributed_mode() -> bool:
    from runtime_guards import load_deployment_configuration
    try:
        return load_deployment_configuration().mode == 'distributed'
    except Exception as e:
        logger.debug('[HumanGate] deployment probe failed: %s', e)
        return False


def _flag_key(kind: str, request_id: str) -> str:
    return f'{kind}:{request_id}'

GATE_STDIN = 'stdin'
GATE_GUIDANCE = 'guidance'
GATE_WRITE_APPROVAL = 'write_approval'
_VALID_GATE_KINDS = frozenset({
    GATE_STDIN,
    GATE_GUIDANCE,
    GATE_WRITE_APPROVAL,
})


def _default_capacity() -> int:
    """Size waiters from the launch-time active-task budget.

    A task normally owns at most one blocking human gate.  Two slots per
    admitted task leave room for nested orchestration transitions without
    turning abandoned request IDs into an unbounded process-lifetime map.
    """
    active_tasks = resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', minimum=1, maximum=256)
    return max(8, min(512, active_tasks * 2))


@dataclass
class HumanGateEntry(Generic[ResponseT]):
    owner_user_id: int
    # Process-local routing hint, never answer authority or a second store.
    durable_scope: tuple[str, str] | None = None
    event: threading.Event = field(default_factory=threading.Event)
    resolved: bool = False
    response: ResponseT | None = None


@dataclass(frozen=True)
class HumanGateResolution(Generic[ResponseT]):
    found: bool
    resolved: bool
    response: ResponseT | None = None


class OwnedHumanGateRegistry:
    """Thread-safe first-resolution-wins registry with a hard capacity."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or int(capacity) != capacity \
                or int(capacity) <= 0:
            raise ValueError('human gate capacity must be a positive integer')
        self._capacity = int(capacity)
        self._entries: dict[tuple[str, str], HumanGateEntry[object]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(kind: str, request_id: object) -> tuple[str, str]:
        if kind not in _VALID_GATE_KINDS:
            raise ValueError(f'unknown human gate kind: {kind!r}')
        return kind, require_human_gate_request_id(
            request_id, field=f'{kind} request id')

    def register(
        self,
        kind: str,
        request_id: object,
        *,
        owner_user_id: object,
        durable_scope: tuple[str, str] | None = None,
    ) -> HumanGateEntry[object] | None:
        key = self._key(kind, request_id)
        owner = require_user_id(
            owner_user_id, context=f'{kind} request owner')
        entry: HumanGateEntry[object] = HumanGateEntry(
            owner_user_id=owner, durable_scope=durable_scope)
        with self._lock:
            if key in self._entries or len(self._entries) >= self._capacity:
                return None
            self._entries[key] = entry
        # Mirror the pending waiter so a resolve landing on ANOTHER replica
        # can find (and owner-check) it. Overwrite is deliberate: a retried
        # registration after a replica crash re-arms the same flag.
        if _distributed_mode():
            try:
                from lib.runtime.runtime_state_store import get_store
                get_store().set_value(
                    _REQ_FLAG_KIND, _flag_key(*key),
                    json.dumps({'owner': owner, 'conversationId': durable_scope[0], 'turnId': durable_scope[1]})
                    if durable_scope else str(owner), _FLAG_TTL_S)
            except Exception as e:
                logger.debug('[HumanGate] request-flag plant failed: %s', e)
        return entry

    def owned_durable_scope(self, kind: str, request_id: object, *, owner_user_id: object):
        key = self._key(kind, request_id)
        owner = require_user_id(owner_user_id, context='human gate scope owner')
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry.durable_scope if entry.owner_user_id == owner else None
        if _distributed_mode():
            try:
                from lib.runtime.runtime_state_store import get_store
                value = get_store().get_value(_REQ_FLAG_KIND, _flag_key(*key))
                marker = json.loads(value) if isinstance(value, str) else None
                if isinstance(marker, dict) and marker.get('owner') == owner:
                    conv, turn = marker.get('conversationId'), marker.get('turnId')
                    if isinstance(conv, str) and conv and isinstance(turn, str) and turn:
                        return conv, turn
            except (ValueError, TypeError):
                pass  # Legacy unscoped owner marker has no durable route.
            except Exception as error:
                logger.warning('[HumanGate] durable scope lookup failed: %s', error)
                raise
        return None

    def resolve(
        self,
        kind: str,
        request_id: object,
        *,
        owner_user_id: object,
        response: object,
    ) -> bool:
        key = self._key(kind, request_id)
        owner = require_user_id(
            owner_user_id, context=f'{kind} response owner')
        with self._lock:
            entry = self._entries.get(key)
            # An owner mismatch is intentionally indistinguishable from an
            # unknown ID so this registry cannot be used as an existence
            # oracle across tenants.
            if (entry is None or entry.owner_user_id != owner
                    or entry.resolved):
                if entry is None:
                    return self._resolve_cross_replica(
                        key, owner=owner, response=response)
                return False
            entry.resolved = True
            entry.response = response
            entry.event.set()
            return True

    def _resolve_cross_replica(
        self,
        key: tuple[str, str],
        *,
        owner: int,
        response: object,
    ) -> bool:
        """Plant the answer for a waiter registered on ANOTHER replica.

        First-writer-wins via ``set_value_nx``: a concurrent resolve on any
        replica observes the taken claim and returns False, preserving
        first-resolution-wins across the fleet. A pending-request flag owned
        by someone else refuses the plant (foreign), and the waiting replica
        re-checks the value's owner before adopting, so planting can never
        resolve another principal's gate.
        """
        if not _distributed_mode():
            return False
        flag_key = _flag_key(*key)
        try:
            from lib.runtime.runtime_state_store import get_store
            store = get_store()
            pending_owner = store.get_value(_REQ_FLAG_KIND, flag_key)
            if pending_owner is not None and pending_owner != str(owner):
                try:
                    marker = json.loads(pending_owner)
                except (TypeError, ValueError):
                    return False
                if not isinstance(marker, dict) or marker.get('owner') != owner:
                    return False
            payload = json.dumps(
                {'owner': owner, 'response': response}, ensure_ascii=False)
            if len(payload.encode('utf-8')) > _MAX_ANSWER_BYTES:
                logger.warning('[HumanGate] cross-replica answer for %s '
                               'exceeds %d bytes; refused',
                               flag_key, _MAX_ANSWER_BYTES)
                return False
            planted = store.set_value_nx(
                _ANS_FLAG_KIND, flag_key, payload, _FLAG_TTL_S)
        except Exception as e:
            logger.debug('[HumanGate] cross-replica resolve failed for %s: '
                         '%s', flag_key, e)
            return False
        if planted:
            logger.info('[HumanGate] cross-replica resolve planted for %s '
                        '(owner=%s)', flag_key, owner)
        return planted

    def poll_store_resolution(
        self,
        kind: str,
        request_id: object,
        entry: HumanGateEntry[object],
    ) -> bool:
        """Adopt an answer planted by another replica into ``entry``.

        Waiters call this between ``entry.event.wait`` slices. Returns True
        when the entry is now resolved (the event is set either way, so the
        caller's normal take path works unchanged). A local resolve always
        wins the adoption race: both paths resolve under the same lock and
        only the first finds ``resolved`` False.
        """
        if not _distributed_mode():
            return False
        key = self._key(kind, request_id)
        flag_key = _flag_key(*key)
        try:
            from lib.runtime.runtime_state_store import get_store
            store = get_store()
            raw = store.get_value(_ANS_FLAG_KIND, flag_key)
        except Exception as e:
            logger.debug('[HumanGate] answer probe failed for %s: %s',
                         flag_key, e)
            return False
        if raw is None:
            return False
        try:
            payload = json.loads(raw)
            answer_owner = int(payload.get('owner') or 0)
            response = payload.get('response')
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug('[HumanGate] malformed answer for %s: %s',
                         flag_key, e)
            return False
        with self._lock:
            current = self._entries.get(key)
            if current is not entry or current.resolved:
                return current is entry and current.resolved
            if entry.owner_user_id != answer_owner:
                return False
            entry.resolved = True
            entry.response = response
            entry.event.set()
        try:
            store.delete_value(_ANS_FLAG_KIND, flag_key)
        except Exception as e:
            logger.debug('[HumanGate] answer-flag cleanup failed for %s: %s',
                         flag_key, e)
        return True

    def _drop_request_flag(self, key: tuple[str, str]) -> None:
        if not _distributed_mode():
            return
        try:
            from lib.runtime.runtime_state_store import get_store
            get_store().delete_value(_REQ_FLAG_KIND, _flag_key(*key))
        except Exception as e:
            logger.debug('[HumanGate] request-flag cleanup failed: %s', e)

    def take(
        self,
        kind: str,
        request_id: object,
        entry: HumanGateEntry[object],
    ) -> HumanGateResolution[object]:
        """Atomically remove and return the exact registered entry."""
        key = self._key(kind, request_id)
        with self._lock:
            current = self._entries.get(key)
            if current is not entry:
                return HumanGateResolution(found=False, resolved=False)
            self._entries.pop(key, None)
        self._drop_request_flag(key)
        return HumanGateResolution(
            found=True,
            resolved=current.resolved,
            response=current.response,
        )

    def discard_unresolved(
        self,
        kind: str,
        request_id: object,
        entry: HumanGateEntry[object],
    ) -> bool:
        """Remove ``entry`` only if a resolver has not already won."""
        key = self._key(kind, request_id)
        with self._lock:
            current = self._entries.get(key)
            if current is not entry or current.resolved:
                return False
            self._entries.pop(key, None)
        self._drop_request_flag(key)
        return True

    def is_pending(
        self,
        kind: str,
        request_id: object,
        *,
        owner_user_id: object,
    ) -> bool:
        key = self._key(kind, request_id)
        owner = require_user_id(
            owner_user_id, context=f'{kind} pending-query owner')
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.owner_user_id == owner:
                return True
        if not _distributed_mode():
            return False
        try:
            from lib.runtime.runtime_state_store import get_store
            pending_owner = get_store().get_value(
                _REQ_FLAG_KIND, _flag_key(*key))
        except Exception as e:
            logger.debug('[HumanGate] pending probe failed: %s', e)
            return False
        if pending_owner == str(owner):
            return True
        try:
            marker = json.loads(pending_owner) if isinstance(pending_owner, str) else None
            return isinstance(marker, dict) and marker.get('owner') == owner
        except (TypeError, ValueError):
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


human_gate_registry = OwnedHumanGateRegistry(_default_capacity())


__all__ = [
    'GATE_GUIDANCE',
    'GATE_STDIN',
    'GATE_WRITE_APPROVAL',
    'HumanGateEntry',
    'HumanGateResolution',
    'OwnedHumanGateRegistry',
    'human_gate_registry',
]
