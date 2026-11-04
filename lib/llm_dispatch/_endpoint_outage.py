"""Endpoint-outage dispatch policy shared by the stream and non-stream loops.

Responsibility: ``EndpointUnreachableError`` is positive connect-phase
evidence (DNS resolution, refused or unreachable host, TCP/TLS connect
timeout) that this process could not open a connection to one network
endpoint; no request reached a provider. Every credential, deployment and
model whose ``base_url`` names that host and port shares the failed path, so
rotating among them cannot repair it, and the failure is not their health.
One ``EndpointOutageRouting`` per dispatch call:

* ``absorb`` holds every slot on the failed endpoint until its next probe, so
  slots on other endpoints serve at once instead of each sibling spending an
  attempt on the same dead path;
* when nothing but held slots can serve, the loop sleeps ``probe_delay_s()``
  abortably and probes again on the fixed schedule shared with no-channel
  route holds; a hold lapses at its probe time, so the call notices recovery.

The failure policy decides what an observation costs: a caller without an
explicit upstream budget waits until the network recovers or its owner stops
it, while a budgeted caller spends one hard attempt per observation.

Entry points: ``EndpointOutageRouting``, ``network_endpoint``,
``log_endpoint_outage``. Dependencies: the dispatcher slot pool, the global
default base URL for slots without their own, and the probe schedule owned by
``lib.llm_dispatch._route_unavailable``.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from lib.log import get_logger
from lib.llm_dispatch._route_unavailable import ROUTE_UNAVAILABLE_BACKOFF_S

logger = get_logger('lib.llm_dispatch.api')

_DEFAULT_PORTS = {'http': 80, 'https': 443, 'ws': 80, 'wss': 443}


def network_endpoint(url) -> tuple[str, int | None] | None:
    """``(host, port)`` that a connection to ``url`` opens; None if unnamed."""
    try:
        parts = urlsplit(str(url or '').strip())
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    if not host:
        return None
    return host, port or _DEFAULT_PORTS.get(parts.scheme.lower())


def _slot_endpoint(slot):
    import lib as _lib
    return network_endpoint(
        getattr(slot, 'base_url', '') or getattr(_lib, 'LLM_BASE_URL', ''))


class EndpointOutageRouting:
    """Request-local holds on unreachable network endpoints and their probes."""

    def __init__(self, dispatcher=None):
        self._dispatcher = dispatcher
        self._held: set[tuple[str, str]] = set()
        self.observations = 0
        self._probe_at = 0.0

    @property
    def pairs(self) -> set[tuple[str, str]]:
        """Held ``(key_name, model)`` pairs; holds lapse at the probe time."""
        if self._held and time.monotonic() >= self._probe_at:
            self._held.clear()
        return set(self._held)

    def absorb(self, slot) -> float:
        """Hold every slot on ``slot``'s endpoint; return seconds to its probe."""
        delay = ROUTE_UNAVAILABLE_BACKOFF_S[
            min(self.observations, len(ROUTE_UNAVAILABLE_BACKOFF_S) - 1)]
        self.observations += 1
        self._probe_at = time.monotonic() + delay
        endpoint = _slot_endpoint(slot)
        if endpoint is not None:
            pool = list(getattr(self._dispatcher, 'slots', None) or ())
            self._held |= {(candidate.key_name, candidate.model)
                           for candidate in pool
                           if _slot_endpoint(candidate) == endpoint}
        self._held.add((slot.key_name, slot.model))
        return delay

    def probe_delay_s(self) -> float:
        """Seconds until held endpoints are due for their next probe."""
        return max(0.0, self._probe_at - time.monotonic())


def log_endpoint_outage(log_prefix, slot, error, *, observations, probe_in_s):
    """Record one observation; the first of a call warns, later ones are info."""
    endpoint = _slot_endpoint(slot)
    log = logger.warning if observations == 1 else logger.info
    log('%s Endpoint unreachable on %s:%s (%s, observation #%d) — network '
        'endpoint %s held %.0fs; slots on other endpoints serve, else it is '
        'probed again: %s',
        log_prefix, slot.key_name, slot.model,
        getattr(error, 'base_url', '') or '?', observations,
        '%s:%s' % endpoint if endpoint else '?', probe_in_s,
        str(error)[:160])
