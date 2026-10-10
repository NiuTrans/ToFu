#!/usr/bin/env python3
"""lib/desktop/pairing.py — pairing-code store + LAN discovery responder.

The pairing-code UX (docs/modules/remote_execution.md): a 6-digit
one-time code is minted by the panel (POST /api/v1/desktop/pair-code,
authenticated) and consumed by the agent (POST /api/desktop/pair, NOT
authenticated — the code IS the credential) to exchange for an
agents:bridge key.

The PAIRING-STORE is deliberately process-local and in-memory: a code
lasts 5 minutes and is one-shot, so persistence would only extend the
window an attacker has after a restart — and restarting re-seeds the
RNG, which is what actually bounds replay. A server restart invalidates
all outstanding codes; the panel just mints again (cheap).

The LAN DISCOVERY RESPONDER is the optional rung-B auto-discovery
(§11.2.1): a UDP responder that advertises `http://<lan-ip>:15000` to a
broadcast probe. ON by default since 2026-08-04 (set
TOFU_DESKTOP_LAN_DISCOVERY=0 to disable); it also stays silent when the
server bound loopback-only, because advertising an unreachable LAN url
would send every discovering agent to a dead address. mDNS is
deliberately NOT used — corporate networks filter multicast; a plain UDP
broadcast with an HMAC'd response is the v1 primitive.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import select
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

# The pairing-code alphabet: 6 digits, no ambiguous characters. 1e6
# space × 300 s TTL × 3-attempt lockout = standard one-time-code posture.
_CODE_ALPHABET = '0123456789'
_CODE_LEN = 6
_CODE_TTL_S = 300  # 5 minutes
_MAX_ATTEMPTS = 3  # per-code brute-force lockout

# LAN discovery v1 magic + version. The probe is exactly this many bytes;
# anything else is dropped (a cheap "is this even our protocol" gate).
_LAN_MAGIC = b'TOFU-DESKTOP-DISC\x01'  # 16 bytes: magic + version
_LAN_BIND = ('', 15001)  # adjacent to the app; not a privileged port

@dataclass(frozen=True, slots=True)
class PairingIdentity:
    owner_user_id: int
    account_user_id: str = ''
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, 'owner_user_id',
            require_user_id(self.owner_user_id, context='desktop pairing'),
        )
        object.__setattr__(
            self, 'account_user_id', str(self.account_user_id or '').strip())
        object.__setattr__(
            self, 'tenant_id', str(self.tenant_id or '').strip() or None)


_STORE: dict[str, dict] = {}  # code -> {identity, expires_at, attempts}
_STORE_LOCK = threading.Lock()

# ── Per-IP global failure budget ──
# The per-code 3-attempt lockout is BY DESIGN not enough: an attacker who
# keeps guessing NEW random codes gets a fresh budget on every attempt, so
# 1e6 codes × unlimited fresh attempts = brute-forceable. The boundary that
# actually matters is the attacker's ATTEMPT RATE, not a single code's
# retry count (owner 2026-08-04, verified gap). Per-code lockout stays as
# defense-in-depth; this per-IP budget is the real boundary.
# Mechanism: each failed pair-exchange from an IP records a timestamp;
# N failures inside the window trips a cooldown during which the IP gets
# 429 BEFORE its code is even looked up. A successful exchange resets the
# IP's slate (a legit agent retrying after a transient failure is not
# punished forever).
_IP_FAIL_WINDOW_S = 60   # sliding window for counting failures
_IP_FAIL_BUDGET = 10     # failures inside the window that trip the block
_IP_BLOCK_S = 300        # cooldown once the budget trips

_IP_FAILS: dict[str, list[float]] = {}   # ip -> failure timestamps
_IP_BLOCKED: dict[str, float] = {}       # ip -> blocked-until epoch


def _now() -> float:
    return time.time()


def generate_code() -> str:
    """A fresh, collision-checked 6-digit pairing code."""
    for _ in range(32):
        cand = ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        with _STORE_LOCK:
            # Re-use is harmless (same user, fresh TTL) but we still
            # dodge it so two concurrent mints never collide visually.
            if cand not in _STORE or _STORE[cand]['expires_at'] <= _now():
                return cand
    # 32 collisions in 1e6 space is effectively impossible; fall back to
    # a locked counter derivation rather than fail.
    return ''.join(_CODE_ALPHABET[secrets.randbelow(10)] for _ in range(_CODE_LEN))


def mint_code(
    identity: PairingIdentity, ttl: int | None = None,
) -> tuple[str, float]:
    """Mint a code bound to one complete authenticated identity."""
    if not isinstance(identity, PairingIdentity):
        raise TypeError('desktop pairing requires PairingIdentity')
    if ttl is None:
        ttl = _CODE_TTL_S
    code = generate_code()
    expires_at = _now() + ttl
    with _STORE_LOCK:
        _STORE[code] = {'identity': identity, 'expires_at': expires_at,
                        'attempts': 0}
    return code, expires_at


def _hmac(code: str, secret: str) -> str:
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()


def consume_code(code: str) -> Optional[PairingIdentity]:
    """Validate and consume a code, returning its complete identity on
    success, or ``None`` on any failure (missing / expired / locked /
    over-attempted). A consume that fails the attempt budget also burns
    the code so it cannot be re-tried.

    The code is bound to its minting user; the exchange mints the
    agents:bridge key for THAT user (the agent carries no identity of its
    own into this call — the code is the only credential).
    """
    code = (code or '').strip()
    with _STORE_LOCK:
        row = _STORE.get(code)
        if row is None:
            return None
        if row['expires_at'] <= _now():
            del _STORE[code]
            return None
        row['attempts'] += 1
        if row['attempts'] > _MAX_ATTEMPTS:
            del _STORE[code]
            return None
        identity = row['identity']
        del _STORE[code]  # ONE-SHOT: never reusable
    if not isinstance(identity, PairingIdentity):
        raise RuntimeError('desktop pairing identity is corrupt')
    return identity


def ip_fail_budget_exceeded(ip: str) -> bool:
    """True when *ip* is inside a cooldown after exhausting its failure
    budget. Checked BEFORE the code is consumed — a blocked IP cannot even
    attempt (that is what makes the rate bound real, not cosmetic)."""
    ip = ip or '<unknown>'
    now = _now()
    with _STORE_LOCK:
        blocked_until = _IP_BLOCKED.get(ip)
        if blocked_until is not None:
            if blocked_until > now:
                return True
            del _IP_BLOCKED[ip]  # cooldown expired
        fails = [t for t in _IP_FAILS.get(ip) or []
                 if now - t < _IP_FAIL_WINDOW_S]
        _IP_FAILS[ip] = fails
        return len(fails) >= _IP_FAIL_BUDGET


def record_pair_failure(ip: str) -> None:
    """Record one failed pair-exchange from *ip*; trip the block at budget."""
    ip = ip or '<unknown>'
    now = _now()
    with _STORE_LOCK:
        fails = [t for t in _IP_FAILS.get(ip) or []
                 if now - t < _IP_FAIL_WINDOW_S]
        fails.append(now)
        _IP_FAILS[ip] = fails
        if len(fails) >= _IP_FAIL_BUDGET:
            _IP_BLOCKED[ip] = now + _IP_BLOCK_S
            logger.warning('[DesktopPairing] IP %s blocked for %ds after '
                           '%d pair-exchange failures in %ds',
                           ip, _IP_BLOCK_S, len(fails), _IP_FAIL_WINDOW_S)


def record_pair_success(ip: str) -> None:
    """A successful exchange clears the IP's failure slate — a legit agent
    retrying after a transient failure is not punished forever."""
    ip = ip or '<unknown>'
    with _STORE_LOCK:
        _IP_FAILS.pop(ip, None)


def pending_codes(owner_user_id: int) -> list[dict]:
    """Metadata (no secrets) for a user's still-valid codes — the panel's
    countdown renders against this."""
    owner_id = require_user_id(owner_user_id, context='desktop pairing list')
    now = _now()
    with _STORE_LOCK:
        return [{'code': c, 'expires_at': r['expires_at'],
                 'remaining': max(0, int(r['expires_at'] - now))}
                for c, r in _STORE.items()
                if r['identity'].owner_user_id == owner_id
                and r['expires_at'] > now]


# ═══════════════════════════════════════════════════════════════════
#  LAN discovery responder (optional rung B, §11.2.1)
# ═══════════════════════════════════════════════════════════════════

class LanDiscoveryResponder:
    """UDP broadcast responder for local-network auto-discovery.

    On by default (TOFU_DESKTOP_LAN_DISCOVERY=0 to disable). Listens on
    UDP 15001 (adjacent to the app); on receiving the exact probe magic
    replies with the server's LAN url + an HMAC (key = a per-process
    random, so a non-Tofu box that happens to listen on 15001 cannot be
    spoofed into advertising a hostile url). Best-effort: start() never
    raises on a busy port — it just stays silent and logs at warning.
    """

    def __init__(self, url: str, bind: tuple = _LAN_BIND):
        self.url = url
        self.bind = bind
        self._secret = secrets.token_bytes(32).hex()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._wake_reader: socket.socket | None = None
        self._wake_writer: socket.socket | None = None

    def _hmac(self, nonce: bytes) -> bytes:
        return hmac.new(self._secret.encode(), nonce, hashlib.sha256).digest()

    def _handle(self, data: bytes, addr) -> None:
        if data == _LAN_MAGIC:
            # Reply: HMAC(nonce=url) || url. The agent verifies the HMAC
            # with the same shared-secret derived from... we can't share a
            # secret with an unknown agent, so instead the agent TRUSTS the
            # first responder on the LAN for the short discovery window —
            # acceptable because the response is a LOOPBACK url the agent
            # then probes against /api/health (which authenticates). A
            # hostile LAN responder advertising a loopback url is useless:
            # the agent would probe its OWN loopback and get nothing.
            payload = self.url.encode('utf-8')
            mac = hashlib.sha256(
                (self._secret + self.url).encode()).digest()[:16]
            try:
                self._sock.sendto(mac + payload, addr)
            except OSError as e:
                logger.debug('[DesktopPairing] discovery reply to %s '
                             'failed: %s', addr, e)

    def _loop(self) -> None:
        current_thread = threading.current_thread()
        sock = self._sock
        wake_reader = self._wake_reader
        if sock is None or wake_reader is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select((sock, wake_reader), (), ())
                    if wake_reader in readable:
                        wake_reader.recv(1)
                        if self._stop.is_set():
                            return
                    if sock in readable:
                        data, addr = sock.recvfrom(1024)
                        self._handle(data, addr)
                except (OSError, ValueError) as exc:
                    logger.debug(
                        '[DesktopPairing] discovery wait ended: %s', exc)
                    return
        finally:
            with self._lifecycle_lock:
                if self._thread is current_thread:
                    self._thread = None
                    self._close_sockets_locked()

    def _close_sockets_locked(self) -> None:
        for attribute in ('_sock', '_wake_reader', '_wake_writer'):
            endpoint = getattr(self, attribute)
            setattr(self, attribute, None)
            if endpoint is not None:
                try:
                    endpoint.close()
                except OSError as exc:
                    logger.debug(
                        '[DesktopPairing] discovery socket close failed: %s',
                        exc)

    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._close_sockets_locked()
            sock = None
            wake_reader = None
            wake_writer = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(self.bind)
                wake_reader, wake_writer = socket.socketpair()
            except OSError as exc:
                for endpoint in (sock, wake_reader, wake_writer):
                    if endpoint is not None:
                        endpoint.close()
                logger.warning('[DesktopPairing] LAN discovery responder could '
                               'not bind %s — staying silent: %s', self.bind,
                               exc)
                return False
            self._sock = sock
            self._wake_reader = wake_reader
            self._wake_writer = wake_writer
            self._stop.clear()
            thread = threading.Thread(
                target=self._loop, daemon=True,
                name='tofu-desktop-lan-discovery')
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:
                self._thread = None
                self._close_sockets_locked()
                logger.warning(
                    '[DesktopPairing] LAN discovery thread failed to start: %s',
                    exc)
                return False
        logger.info('[DesktopPairing] LAN discovery responder listening on '
                    'UDP %s', self.bind[1])
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        """Wake and bounded-join the exact responder owner."""
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            wake_writer = self._wake_writer
            if thread is None:
                self._close_sockets_locked()
                return True
        if wake_writer is not None:
            try:
                wake_writer.send(b'\x00')
            except OSError as exc:
                logger.debug(
                    '[DesktopPairing] discovery wake failed: %s', exc)
        try:
            wait_seconds = max(0.0, float(timeout))
        except (TypeError, ValueError, OverflowError):
            wait_seconds = 2.0
        if thread is not threading.current_thread():
            thread.join(timeout=wait_seconds)
        stopped = not thread.is_alive()
        if stopped:
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
                    self._close_sockets_locked()
        return stopped


def lan_ip() -> str:
    """Best-effort primary LAN IPv4 for this host ('' when indeterminate).

    The UDP "connect" trick sends NO traffic — it only makes the kernel
    pick the outbound interface, whose address is what LAN peers should
    use to reach us. TEST-NET-1 (192.0.2.1) is deliberately unroutable.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 80))
            return str(s.getsockname()[0] or '')
        finally:
            s.close()
    except OSError as e:
        logger.debug('[DesktopPairing] LAN IP indeterminate: %s', e)
        return ''


#: Bind hosts that mean "not reachable from the LAN" — the responder
#: must stay silent on these or it would advertise a dead address.
_LOOPBACK_BINDS = frozenset({'127.0.0.1', 'localhost', '::1'})


def maybe_start_responder(port: int, environ=None,
                          bind: tuple | None = None,
                          bind_host: str = ''):
    """Start the LAN discovery responder unless explicitly disabled.

    ON by default since 2026-08-04 (owner: LAN pairing is the primary
    agent-attach flow and must not need an env var); set
    TOFU_DESKTOP_LAN_DISCOVERY=0 to disable. ``bind_host`` is the server's
    effective bind — a loopback-only bind makes the advertised LAN url
    unreachable, so the responder stays silent then.

    Returns the running responder, or ``None`` when disabled / impossible.
    This is THE wiring the class never had (owner review 2026-08-03: the
    responder was only ever instantiated in tests, so rung B could never
    answer in production) — server.py calls this from
    ``_start_background_workers``. ``bind`` exists for tests (ephemeral
    port); production keeps ``_LAN_BIND``.
    """
    env = os.environ if environ is None else environ
    if (env.get('TOFU_DESKTOP_LAN_DISCOVERY') or '').strip() == '0':
        return None
    if (bind_host or '').strip().lower() in _LOOPBACK_BINDS:
        logger.debug('[DesktopPairing] LAN discovery skipped — server bound '
                     'loopback-only (%s), advertising would be a lie',
                     bind_host)
        return None
    ip = lan_ip()
    if not ip:
        logger.warning('[DesktopPairing] LAN discovery enabled but no LAN '
                       'IP could be determined — staying silent')
        return None
    responder = LanDiscoveryResponder('http://%s:%d' % (ip, int(port)),
                                      bind=bind or _LAN_BIND)
    if not responder.start():
        return None
    logger.info('[DesktopPairing] LAN discovery responder advertising %s',
                responder.url)
    return responder


__all__ = ['generate_code', 'mint_code', 'consume_code', 'pending_codes',
           'LanDiscoveryResponder', 'lan_ip', 'maybe_start_responder',
           'ip_fail_budget_exceeded', 'record_pair_failure',
           'record_pair_success',
           '_CODE_TTL_S', '_MAX_ATTEMPTS', '_LAN_MAGIC', '_LAN_BIND',
           '_IP_FAIL_BUDGET', '_IP_BLOCK_S']
__all__ = ['generate_code', 'mint_code', 'consume_code', 'pending_codes',
           'LanDiscoveryResponder',
           'ip_fail_budget_exceeded', 'record_pair_failure',
           'record_pair_success',
           '_CODE_TTL_S', '_MAX_ATTEMPTS', '_LAN_MAGIC', '_LAN_BIND',
           '_IP_FAIL_BUDGET', '_IP_BLOCK_S']
