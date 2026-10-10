"""Exact browser HTTP request ownership; authentication remains middleware-owned.

Legacy rate/concurrency constructor options are accepted but cannot reject
requests. Only live leases are retained and release is idempotent. Credentials
are reduced to keyed fingerprints; no payload or credential enters diagnostics.
Protocol validity belongs to the current authenticated poll frame, never a
previous request's cooldown or a client-supplied header hint.
"""
from __future__ import annotations
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

@dataclass(frozen=True, slots=True)
class BrowserPollAdmissionDecision:
    allowed: bool
    code: str = ''
    retry_after_seconds: int = 0
    client_protocol_version: int = 0

@dataclass(slots=True)
class BrowserPollLease:
    credential_fingerprint: str
    owner_user_id: str = ''
    released: bool = False
    identity: str = field(default_factory=lambda: secrets.token_hex(16))

class BrowserPollAdmission:
    """Account for live requests without admission/rate/TTL quotas."""
    def __init__(self, *, max_inflight=None, max_bucket_entries=None,
                 credential_rpm=120, owner_rpm=None, global_rpm=None,
                 protocol_cooldown_seconds=60, clock: Callable[[], float]=time.monotonic):
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._leases: dict[str, BrowserPollLease] = {}

    def enter(self, *, credential: object, peer: object='',
              reported_protocol_version: object=0):
        material = str(credential or '').encode('utf-8', errors='replace')
        fingerprint = hmac.new(self._salt, material, hashlib.sha256).hexdigest()[:32]
        lease = BrowserPollLease(fingerprint)
        with self._lock:
            self._leases[lease.identity] = lease
        return BrowserPollAdmissionDecision(True), lease

    def admit_owner(self, lease, *, owner_user_id):
        owner = str(owner_user_id or '').strip()
        if not owner.isdigit() or int(owner) < 1:
            raise ValueError('owner_user_id must be a positive integer')
        with self._lock:
            if self._leases.get(lease.identity) is not lease:
                raise RuntimeError('Browser poll lease is released or belongs to another controller')
            if lease.owner_user_id and lease.owner_user_id != owner:
                raise RuntimeError('Browser poll lease owner cannot change')
            lease.owner_user_id = owner
        return BrowserPollAdmissionDecision(True)

    def note_protocol_rejection(self, *, credential, client_protocol_version=0):
        # Compatibility hook: the authenticated frame owns protocol validation.
        return None

    def release(self, lease):
        if lease is None:
            return
        with self._lock:
            if lease.released:
                return
            if self._leases.get(lease.identity) is not lease:
                raise RuntimeError('Browser poll lease belongs to another controller')
            self._leases.pop(lease.identity)
            lease.released = True

    def snapshot(self):
        with self._lock:
            return {
                'active': len(self._leases),
                'activeCredentials': len({value.credential_fingerprint for value in self._leases.values()}),
                'activeOwners': len({value.owner_user_id for value in self._leases.values() if value.owner_user_id}),
                'credentialBuckets': 0, 'ownerBuckets': 0, 'protocolCooldowns': 0,
                'rejections': {},
                'limits': {name: 0 for name in ('maxInflight', 'maxInflightPerCredential',
                    'maxInflightPerOwner', 'credentialRpm', 'ownerRpm', 'globalRpm', 'stateEntries')},
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
