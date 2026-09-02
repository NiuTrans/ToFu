"""Bounded admission for the owner/device browser long-poll transport.

The browser bridge is reachable through user-controlled networks and a device
credential is not a resource budget.  This module owns the cheap process-local
front door used before storage authentication, plus the owner-aware gate used
after authentication.  It retains only keyed digests and counters; raw bridge
credentials, request bodies, client IDs and results never enter this state.

The current personal deployment has one serving process.  Limits are resolved
from the shared launch-time resource probe and every map has a hard capacity.
The class boundary deliberately accepts explicit owner identity and can be
backed by a shared admission store when distributed serving is enabled later.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from lib.browser.protocol import PROTOCOL_VERSION
from lib.log import get_logger
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BrowserPollAdmissionDecision:
    """One stable route-facing admission result."""

    allowed: bool
    code: str = ''
    retry_after_seconds: int = 0
    client_protocol_version: int = 0


@dataclass(slots=True)
class BrowserPollLease:
    """One admitted HTTP request; release is idempotent."""

    credential_fingerprint: str
    owner_user_id: str = ''
    released: bool = False


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    updated_at: float
    last_seen_at: float


class BrowserPollAdmission:
    """Two-stage browser-poll admission with bounded token-bucket state."""

    def __init__(
        self,
        *,
        max_inflight: int | None = None,
        max_bucket_entries: int | None = None,
        credential_rpm: int = 120,
        owner_rpm: int | None = None,
        global_rpm: int | None = None,
        protocol_cooldown_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved_inflight = (
            resolve_resource_budget(
                'TOFU_BROWSER_POLL_MAX_INFLIGHT', minimum=4, maximum=256)
            if max_inflight is None else int(max_inflight)
        )
        self.max_inflight = max(1, min(256, resolved_inflight))
        # One device normally owns one long-poll. A second slot absorbs a
        # proxy/network retry overlap; more must not let one compromised or
        # broken computer consume the owner's entire bridge budget.
        self.max_inflight_per_credential = min(2, self.max_inflight)
        self.max_inflight_per_owner = max(
            1, min(32, max(4, self.max_inflight // 2)))

        if max_bucket_entries is None:
            registry_capacity = resolve_resource_budget(
                'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
                minimum=16, maximum=8192)
            max_bucket_entries = registry_capacity * 8
        self.max_bucket_entries = max(16, min(65_536, int(max_bucket_entries)))

        self.credential_rpm = max(30, min(3600, int(credential_rpm)))
        self.owner_rpm = max(
            self.credential_rpm,
            min(7200, int(owner_rpm or max(300, self.max_inflight * 30))),
        )
        self.global_rpm = max(
            self.owner_rpm,
            min(30_000, int(global_rpm or max(600, self.max_inflight * 60))),
        )
        self.protocol_cooldown_seconds = max(
            1, min(300, int(protocol_cooldown_seconds)))
        self._clock = clock
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()

        now = self._clock()
        self._global_bucket = _TokenBucket(
            tokens=float(self.global_rpm), updated_at=now, last_seen_at=now)
        self._credential_buckets: OrderedDict[str, _TokenBucket] = OrderedDict()
        self._owner_buckets: OrderedDict[str, _TokenBucket] = OrderedDict()
        self._protocol_cooldowns: OrderedDict[
            str, tuple[float, int]
        ] = OrderedDict()
        self._active_global = 0
        self._active_by_credential: dict[str, int] = {}
        self._active_by_owner: dict[str, int] = {}
        self._rejection_counts: dict[str, int] = {}
        self._last_rejection_log_at: dict[str, float] = {}

    def _fingerprint(self, credential: object, peer: object = '') -> str:
        token = str(credential or '').strip()
        if token:
            material = b'credential\0' + token.encode('utf-8', errors='replace')
        else:
            material = b'missing\0' + str(peer or '').encode(
                'utf-8', errors='replace')
        return hmac.new(self._salt, material, hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _consume(
        bucket: _TokenBucket,
        *,
        now: float,
        rate_per_minute: int,
    ) -> tuple[bool, int]:
        elapsed = max(0.0, now - bucket.updated_at)
        refill_per_second = rate_per_minute / 60.0
        bucket.tokens = min(
            float(rate_per_minute),
            bucket.tokens + elapsed * refill_per_second,
        )
        bucket.updated_at = now
        bucket.last_seen_at = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0
        retry_after = max(1, math.ceil((1.0 - bucket.tokens) / refill_per_second))
        return False, retry_after

    def _bucket_for(
        self,
        buckets: OrderedDict[str, _TokenBucket],
        key: str,
        *,
        now: float,
        initial_tokens: int,
    ) -> _TokenBucket:
        bucket = buckets.get(key)
        if bucket is None:
            while len(buckets) >= self.max_bucket_entries:
                buckets.popitem(last=False)
            bucket = _TokenBucket(
                tokens=float(initial_tokens), updated_at=now, last_seen_at=now)
            buckets[key] = bucket
        else:
            buckets.move_to_end(key)
        return bucket

    def _record_rejection(self, decision: BrowserPollAdmissionDecision) -> None:
        """Emit first/power-of-two/heartbeat checkpoints, never per request."""
        if decision.allowed:
            return
        now = self._clock()
        with self._lock:
            count = self._rejection_counts.get(decision.code, 0) + 1
            self._rejection_counts[decision.code] = count
            last = self._last_rejection_log_at.get(decision.code, 0.0)
            should_log = (
                count <= 2
                or (count & (count - 1)) == 0
                or now - last >= 300.0
            )
            if should_log:
                self._last_rejection_log_at[decision.code] = now
        if should_log:
            logger.warning(
                '[BrowserPollAdmission] rejected code=%s count=%d retry_after=%ds',
                decision.code, count, decision.retry_after_seconds)

    def enter(
        self,
        *,
        credential: object,
        peer: object = '',
        reported_protocol_version: object = 0,
    ) -> tuple[BrowserPollAdmissionDecision, BrowserPollLease | None]:
        """Cheap pre-auth gate; returns a lease only for admitted requests.

        The optional header-level protocol hint exists only to let a freshly
        upgraded extension clear an old-version cooldown while retaining the
        same credential.  It grants no authority: authentication and the
        protocol declaration in the JSON frame remain authoritative.
        """
        fingerprint = self._fingerprint(credential, peer)
        try:
            reported_protocol = int(reported_protocol_version or 0)
        except (TypeError, ValueError, OverflowError):
            reported_protocol = 0
        now = self._clock()
        decision: BrowserPollAdmissionDecision
        lease: BrowserPollLease | None = None
        with self._lock:
            bucket = self._bucket_for(
                self._credential_buckets,
                fingerprint,
                now=now,
                initial_tokens=self.credential_rpm,
            )
            allowed, retry_after = self._consume(
                bucket, now=now, rate_per_minute=self.credential_rpm)
            if not allowed:
                decision = BrowserPollAdmissionDecision(
                    False, 'browser_poll_credential_rate_limited', retry_after)
            else:
                cooldown = self._protocol_cooldowns.get(fingerprint)
                if cooldown is not None and cooldown[0] <= now:
                    self._protocol_cooldowns.pop(fingerprint, None)
                    cooldown = None
                if cooldown is not None and (
                        reported_protocol == PROTOCOL_VERSION):
                    # A normal upgrade keeps its pre-paired credential.
                    # Do not make that healthy client inherit the old
                    # binary's cooldown.  Exact equality avoids treating a
                    # future, incompatible protocol as current.
                    self._protocol_cooldowns.pop(fingerprint, None)
                    cooldown = None
                if cooldown is not None:
                    self._protocol_cooldowns.move_to_end(fingerprint)
                    decision = BrowserPollAdmissionDecision(
                        False,
                        'browser_protocol_upgrade_required',
                        max(1, math.ceil(cooldown[0] - now)),
                        cooldown[1],
                    )
                else:
                    global_allowed, global_retry = self._consume(
                        self._global_bucket,
                        now=now,
                        rate_per_minute=self.global_rpm,
                    )
                    if not global_allowed:
                        decision = BrowserPollAdmissionDecision(
                            False, 'browser_poll_global_rate_limited',
                            global_retry)
                    elif self._active_global >= self.max_inflight:
                        decision = BrowserPollAdmissionDecision(
                            False, 'browser_poll_capacity', 1)
                    elif self._active_by_credential.get(fingerprint, 0) >= (
                            self.max_inflight_per_credential):
                        decision = BrowserPollAdmissionDecision(
                            False, 'browser_poll_credential_capacity', 1)
                    else:
                        self._active_global += 1
                        self._active_by_credential[fingerprint] = (
                            self._active_by_credential.get(fingerprint, 0) + 1)
                        lease = BrowserPollLease(fingerprint)
                        decision = BrowserPollAdmissionDecision(True)
        self._record_rejection(decision)
        return decision, lease

    def admit_owner(
        self,
        lease: BrowserPollLease,
        *,
        owner_user_id: object,
    ) -> BrowserPollAdmissionDecision:
        """Apply the owner-aware rate/concurrency gate after authentication."""
        owner = str(owner_user_id or '').strip()
        if not owner.isdigit() or int(owner) < 1:
            raise ValueError('owner_user_id must be a positive integer')
        now = self._clock()
        with self._lock:
            if lease.released:
                raise RuntimeError('browser poll lease was already released')
            if lease.owner_user_id:
                if lease.owner_user_id != owner:
                    raise RuntimeError('browser poll lease owner cannot change')
                return BrowserPollAdmissionDecision(True)
            bucket = self._bucket_for(
                self._owner_buckets,
                owner,
                now=now,
                initial_tokens=self.owner_rpm,
            )
            allowed, retry_after = self._consume(
                bucket, now=now, rate_per_minute=self.owner_rpm)
            if not allowed:
                decision = BrowserPollAdmissionDecision(
                    False, 'browser_poll_owner_rate_limited', retry_after)
            elif self._active_by_owner.get(owner, 0) >= self.max_inflight_per_owner:
                decision = BrowserPollAdmissionDecision(
                    False, 'browser_poll_owner_capacity', 1)
            else:
                lease.owner_user_id = owner
                self._active_by_owner[owner] = self._active_by_owner.get(owner, 0) + 1
                decision = BrowserPollAdmissionDecision(True)
        self._record_rejection(decision)
        return decision

    def note_protocol_rejection(
        self,
        *,
        credential: object,
        client_protocol_version: object = 0,
    ) -> None:
        """Quarantine one rejected credential briefly before storage auth."""
        fingerprint = self._fingerprint(credential)
        try:
            protocol_version = int(client_protocol_version or 0)
        except (TypeError, ValueError, OverflowError):
            protocol_version = 0
        now = self._clock()
        with self._lock:
            while len(self._protocol_cooldowns) >= self.max_bucket_entries:
                self._protocol_cooldowns.popitem(last=False)
            self._protocol_cooldowns[fingerprint] = (
                now + self.protocol_cooldown_seconds,
                protocol_version,
            )
            self._protocol_cooldowns.move_to_end(fingerprint)

    def release(self, lease: BrowserPollLease | None) -> None:
        """Release one admitted request; safe from after+teardown double calls."""
        if lease is None:
            return
        with self._lock:
            if lease.released:
                return
            lease.released = True
            self._active_global = max(0, self._active_global - 1)
            fingerprint = lease.credential_fingerprint
            remaining = self._active_by_credential.get(fingerprint, 0) - 1
            if remaining > 0:
                self._active_by_credential[fingerprint] = remaining
            else:
                self._active_by_credential.pop(fingerprint, None)
            if lease.owner_user_id:
                owner_remaining = self._active_by_owner.get(
                    lease.owner_user_id, 0) - 1
                if owner_remaining > 0:
                    self._active_by_owner[lease.owner_user_id] = owner_remaining
                else:
                    self._active_by_owner.pop(lease.owner_user_id, None)

    def snapshot(self) -> dict[str, object]:
        """Bounded diagnostics without credential or owner identifiers."""
        with self._lock:
            return {
                'active': self._active_global,
                'activeCredentials': len(self._active_by_credential),
                'activeOwners': len(self._active_by_owner),
                'credentialBuckets': len(self._credential_buckets),
                'ownerBuckets': len(self._owner_buckets),
                'protocolCooldowns': len(self._protocol_cooldowns),
                'rejections': dict(self._rejection_counts),
                'limits': {
                    'maxInflight': self.max_inflight,
                    'maxInflightPerCredential': self.max_inflight_per_credential,
                    'maxInflightPerOwner': self.max_inflight_per_owner,
                    'credentialRpm': self.credential_rpm,
                    'ownerRpm': self.owner_rpm,
                    'globalRpm': self.global_rpm,
                    'stateEntries': self.max_bucket_entries,
                },
            }


_controller_lock = threading.Lock()
_controller: BrowserPollAdmission | None = None


def browser_poll_admission() -> BrowserPollAdmission:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = BrowserPollAdmission()
        return _controller


def reset_browser_poll_admission_for_tests(
    controller: BrowserPollAdmission | None = None,
) -> None:
    """Replace the process singleton; production callers never use this."""
    global _controller
    with _controller_lock:
        _controller = controller


__all__ = [
    'BrowserPollAdmission',
    'BrowserPollAdmissionDecision',
    'BrowserPollLease',
    'browser_poll_admission',
    'reset_browser_poll_admission_for_tests',
]
