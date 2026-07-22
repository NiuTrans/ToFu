"""Shared-cgroup memory-pressure defenses (self-check + relief + request guard).

Context (2026-07-20): Tofu often runs in a container whose cgroup memory limit
is the WHOLE machine (e.g. 200 GiB) and is SHARED with sibling processes + a
huge FUSE page/slab cache. When that shared cgroup fills to the ceiling (zero
swap), the kernel OOM killer SIGKILLs the highest-RSS process — which is
usually tofu — with a bare "Killed" and no traceback.

We cannot fix the root cause in code: the cgroup is shared, we lack
``CAP_SYS_RESOURCE`` (so we cannot lower our own ``oom_score_adj`` — the kernel
floors it at 0, verified live), and there is no swap. What we CAN do is stop
being the fattest, most-killable process and turn "mystery Killed" into a
logged, controlled degradation. Three defenses, all env-tunable:

  ① startup_self_check()  — at boot, if the cgroup is already near-full AND
     there is no swap, emit a CRITICAL log + audit record so the operator has
     durable evidence this is an environment squeeze, not a tofu bug.
  ② start_monitor()       — a low-frequency (>=30s) daemon thread that, when
     usage crosses the relief threshold, drops our own reclaimable caches and
     calls malloc_trim(0) to hand free heap back to the OS, shrinking our RSS.
  ③ check_request_headroom() — before assembling a LARGE LLM request body,
     if the cgroup is critically full, trim once and, if still critical, fail
     fast with a clear log (conv id + body size + usage%) instead of being
     SIGKILLed mid-assembly.

Everything degrades to a NO-OP when the cgroup / /proc is unreadable (bare
metal, macOS, restricted sandbox): a reader that cannot see ``memory.current``
returns ``None`` and every defense treats "unknown" as "proceed, do nothing".

Env vars (all optional):
  TOFU_CGROUP_WARN_PCT           default 90  — ① self-check trigger
  TOFU_CGROUP_RELIEF_PCT         default 92  — ② monitor relief trigger
  TOFU_CGROUP_REQUEST_PCT        default 95  — ③ request fail-fast trigger
  TOFU_CGROUP_POLL_SEC           default 30  — ② poll interval (clamped >=30)
  TOFU_CGROUP_REQUEST_MIN_BYTES  default 2_000_000 — ③ only guards bodies >= this
  TOFU_CGROUP_REQUEST_GUARD      default 1   — ③ set 0 to log-only (never raise)
"""

from __future__ import annotations

import ctypes
import os
import threading
from typing import Optional

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


class MemoryPressureError(RuntimeError):
    """Raised by ③ when a large request is refused because the cgroup is critically full."""


# ── stdlib readers — every one returns None on ANY failure (graceful no-op) ──

def _read_first_int(paths) -> Optional[int]:
    """Return the int contents of the first readable path, or None."""
    for _p in paths:
        try:
            with open(_p, 'r') as _f:
                _raw = _f.read().strip()
        except OSError:
            continue
        if _raw == 'max':
            return None
        try:
            return int(_raw)
        except ValueError:
            continue
    return None


def mem_limit_bytes() -> Optional[int]:
    """cgroup memory limit in bytes, or None if unlimited/unknown.

    NOTE: mirrors server.py:_tofu_cgroup_mem_limit_bytes() (kernel-ABI paths);
    kept independent so the very-early mlock gate in server.py has no import
    dependency on this later-loaded module.
    """
    _val = _read_first_int(('/sys/fs/cgroup/memory.max',                     # v2
                            '/sys/fs/cgroup/memory/memory.limit_in_bytes'))   # v1
    if _val is None or _val <= 0 or _val >= (1 << 62):  # v1 unlimited sentinel
        return None
    return _val


def mem_usage_bytes() -> Optional[int]:
    """Current cgroup memory usage in bytes (incl. reclaimable cache), or None."""
    _val = _read_first_int(('/sys/fs/cgroup/memory.current',                  # v2
                            '/sys/fs/cgroup/memory/memory.usage_in_bytes'))    # v1
    if _val is None or _val < 0:
        return None
    return _val


def swap_total_bytes() -> Optional[int]:
    """Total swap in bytes from /proc/meminfo, or None if unreadable.

    Zero swap is the aggravating factor: with no swap the kernel cannot page
    out under pressure and must kill instead. Returns 0 when SwapTotal is 0.
    """
    try:
        with open('/proc/meminfo', 'r') as _f:
            for _line in _f:
                if _line.startswith('SwapTotal:'):
                    parts = _line.split()
                    # "SwapTotal:  0 kB"
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def pressure() -> Optional[dict]:
    """Snapshot of cgroup memory pressure, or None if it cannot be computed.

    Returns ``{'limit': int, 'usage': int, 'pct': float, 'swap': int|None}``
    or ``None`` when limit/usage are unreadable (bare metal / restricted env).
    """
    limit = mem_limit_bytes()
    usage = mem_usage_bytes()
    if limit is None or usage is None or limit <= 0:
        return None
    return {
        'limit': limit,
        'usage': usage,
        'pct': 100.0 * usage / float(limit),
        'swap': swap_total_bytes(),
    }


def _env_pct(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _gib(n: int) -> float:
    return n / float(1 << 30)


# ── memory relief primitives ──

def malloc_trim() -> bool:
    """Ask glibc to return free heap arenas to the OS. True on success."""
    try:
        _libc = ctypes.CDLL('libc.so.6', use_errno=True)
        # malloc_trim(0) — release all releasable memory above the trim floor.
        return bool(_libc.malloc_trim(0))
    except Exception as e:  # non-glibc / no libc — harmless
        logger.debug('[cgroup] malloc_trim unavailable: %s', e)
        return False


def relieve_memory(reason: str) -> dict:
    """Drop our own reclaimable caches + trim heap. Logs usage% before/after.

    Returns a small stats dict. Safe to call anywhere; never raises.
    """
    before = pressure()
    before_pct = before['pct'] if before else None
    dropped = 0
    try:
        from lib.ttl_cache import clear_all_caches
        dropped = clear_all_caches()
    except Exception as e:
        logger.warning('[cgroup] cache clear during relief failed: %s', e)
    trimmed = malloc_trim()
    after = pressure()
    after_pct = after['pct'] if after else None
    logger.warning('[cgroup] relief (%s): dropped %d cache entries, malloc_trim=%s, '
                   'usage %.1f%% -> %.1f%%',
                   reason, dropped, trimmed,
                   before_pct if before_pct is not None else -1.0,
                   after_pct if after_pct is not None else -1.0)
    return {'reason': reason, 'dropped': dropped, 'trimmed': trimmed,
            'pct_before': before_pct, 'pct_after': after_pct}


# ── ① startup self-check ──

def startup_self_check() -> Optional[dict]:
    """At boot: if the shared cgroup is already near-full AND has no swap, warn loudly.

    Returns the pressure snapshot when a warning was emitted, else None
    (either headroom is fine or the cgroup is unreadable — both are no-ops).
    """
    snap = pressure()
    if snap is None:
        logger.debug('[cgroup] self-check: cgroup memory unreadable — no-op')
        return None
    warn_pct = _env_pct('TOFU_CGROUP_WARN_PCT', 90.0)
    no_swap = (snap['swap'] == 0)
    if snap['pct'] >= warn_pct and no_swap:
        logger.critical(
            '[cgroup] SHARED CGROUP NEAR-FULL: %.1f%% used (%.1f/%.1f GiB), swap=0. '
            'This process can be OOM-SIGKILLed at any time by the kernel when the '
            'shared cgroup hits its ceiling — a bare "Killed" with no traceback is '
            'an ENVIRONMENT squeeze (siblings + FUSE cache), NOT a tofu bug. '
            'Mitigation needs a smaller dedicated cgroup / swap / fewer siblings.',
            snap['pct'], _gib(snap['usage']), _gib(snap['limit']))
        audit_log('cgroup_near_full',
                  usage_pct=round(snap['pct'], 1),
                  usage_gib=round(_gib(snap['usage']), 1),
                  limit_gib=round(_gib(snap['limit']), 1),
                  swap_bytes=snap['swap'])
        return snap
    logger.info('[cgroup] self-check OK: %.1f%% used (%.1f/%.1f GiB), swap=%s',
                snap['pct'], _gib(snap['usage']), _gib(snap['limit']),
                'yes' if (snap['swap'] or 0) > 0 else 'no')
    return None


# ── ② runtime pressure monitor ──

_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()
_monitor_lock = threading.Lock()


def run_monitor_once() -> Optional[dict]:
    """One monitor tick: relieve memory if usage crosses the relief threshold.

    Returns the relief stats dict when relief ran, else None. Never raises.
    Exposed separately so tests can drive the logic without a thread.
    """
    snap = pressure()
    if snap is None:
        return None
    relief_pct = _env_pct('TOFU_CGROUP_RELIEF_PCT', 92.0)
    if snap['pct'] >= relief_pct:
        return relieve_memory('monitor %.1f%% >= %.0f%%' % (snap['pct'], relief_pct))
    return None


def start_monitor() -> bool:
    """Start the low-frequency background relief monitor (idempotent).

    Returns True if a thread was started, False if unnecessary (cgroup
    unreadable) or already running. Non-blocking: runs on a daemon thread and
    never touches the event loop.
    """
    global _monitor_thread
    if pressure() is None:
        logger.debug('[cgroup] monitor not started — cgroup memory unreadable')
        return False
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return False
        interval = max(30.0, _env_pct('TOFU_CGROUP_POLL_SEC', 30.0))
        _monitor_stop.clear()

        def _loop():
            logger.info('[cgroup] pressure monitor started (interval=%.0fs)', interval)
            while not _monitor_stop.wait(interval):
                try:
                    run_monitor_once()
                except Exception as e:
                    logger.warning('[cgroup] monitor tick failed: %s', e)

        _monitor_thread = threading.Thread(target=_loop, name='cgroup-mem-monitor',
                                           daemon=True)
        _monitor_thread.start()
        return True


def stop_monitor() -> None:
    """Signal the monitor thread to stop (best-effort; for clean shutdown/tests)."""
    _monitor_stop.set()


# ── ③ large-request headroom guard ──

def check_request_headroom(ident: str = '', approx_bytes: int = 0) -> tuple[bool, Optional[str]]:
    """Pre-flight guard for a LARGE outbound request body.

    Returns ``(ok, reason)``:
      - ``(True, None)``  → proceed (headroom fine, body small, or cgroup
        unreadable — the safe default everywhere off-cgroup).
      - ``(False, reason)`` → the cgroup is critically full even after a trim;
        the caller should refuse this request rather than risk a mid-assembly
        SIGKILL. A WARNING with ident + size + usage% is already logged here.

    Only bodies >= TOFU_CGROUP_REQUEST_MIN_BYTES are considered; smaller ones
    always pass (cheap to skip the proc read on the hot path for normal calls).
    """
    min_bytes = 0
    try:
        min_bytes = int(os.environ.get('TOFU_CGROUP_REQUEST_MIN_BYTES', '2000000'))
    except (ValueError, TypeError):
        min_bytes = 2_000_000
    if approx_bytes < min_bytes:
        return True, None
    snap = pressure()
    if snap is None:
        return True, None
    req_pct = _env_pct('TOFU_CGROUP_REQUEST_PCT', 95.0)
    if snap['pct'] < req_pct:
        return True, None
    # Critical: try to make room once, then re-measure.
    relieve_memory('pre-request %s %.1f%%' % (ident or '?', snap['pct']))
    snap2 = pressure()
    pct2 = snap2['pct'] if snap2 else snap['pct']
    if pct2 < req_pct:
        return True, None
    reason = ('cgroup %.1f%% full (%.1f/%.1f GiB) >= %.0f%% after trim — refusing '
              'large request ident=%s body=%.1fMB to avoid OOM SIGKILL'
              % (pct2, _gib((snap2 or snap)['usage']), _gib((snap2 or snap)['limit']),
                 req_pct, ident or '?', approx_bytes / 1e6))
    logger.error('[cgroup] %s', reason)
    audit_log('cgroup_request_refused', ident=ident,
              body_bytes=approx_bytes, usage_pct=round(pct2, 1))
    return False, reason


def approx_body_bytes(body_or_messages) -> int:
    """Cheap upper-ish estimate of a request body's size, in bytes.

    Walks message ``content`` without serialising the whole structure (which
    would itself allocate the very memory we are trying to protect). Returns 0
    on anything unexpected — the guard then treats it as a small body.
    """
    try:
        if isinstance(body_or_messages, dict):
            msgs = body_or_messages.get('messages') or []
        elif isinstance(body_or_messages, list):
            msgs = body_or_messages
        else:
            return 0
        total = 0
        for m in msgs:
            c = m.get('content') if isinstance(m, dict) else None
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        t = part.get('text')
                        if isinstance(t, str):
                            total += len(t)
                        else:
                            total += 512  # image/tool part — rough fixed cost
        return total
    except Exception:
        return 0


__all__ = [
    'MemoryPressureError',
    'mem_limit_bytes', 'mem_usage_bytes', 'swap_total_bytes', 'pressure',
    'malloc_trim', 'relieve_memory',
    'startup_self_check',
    'run_monitor_once', 'start_monitor', 'stop_monitor',
    'check_request_headroom', 'approx_body_bytes',
]
