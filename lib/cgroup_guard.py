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
  TOFU_CGROUP_DROP_LOGS          default 1   — relief also fadvise-drops logs/*.log*
  TOFU_CGROUP_LOGDROP_MIN_BYTES  default 1 MiB — size floor for the log drop
  TOFU_CGROUP_JOURNAL            default 1   — rolling pressure journal to
                                               logs/cgroup_pressure.log
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
        except OSError as _e:
            logger.debug('read first int: unreadable (%s)', _e)
            continue
        if _raw == 'max':
            return None
        try:
            return int(_raw)
        except ValueError as _e:
            logger.debug('read first int: unparseable (%s)', _e)
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
    except (OSError, ValueError, IndexError) as _e:
        logger.debug('swap total bytes: unreadable/unparseable/short/malformed (%s)', _e)
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
    except (ValueError, TypeError) as _e:
        logger.debug('env pct: unparseable/unexpected type (%s)', _e)
        return default


def _gib(n: int) -> float:
    return n / float(1 << 30)


# ── page-cache relief (the part that actually moves the needle) ──
#
# relieve_memory() used to only drop tofu's own heap caches (~2 GiB) — futile
# against a cgroup whose usage is dominated by PAGE CACHE charged by our own
# one-shot IO (rotated logs agents grep once, snapshots, render outputs).
# posix_fadvise(POSIX_FADV_DONTNEED) drops a file's CLEAN pages from the page
# cache; on a shared cgroup those bytes stop counting against our limit.
# Measured live on beegfs-fuse 2026-07-27: fadvising a 105 MB rotated log
# freed ~100 MB of cgroup cache instantly.

def fadvise_dontneed(path: str) -> int:
    """Drop *path*'s clean page-cache pages. Returns file size advised, 0 on any failure.

    Never raises: non-Linux, missing file, or a filesystem that rejects the
    hint (ENOSYS/EINVAL) all degrade to a no-op. Only CLEAN pages are dropped
    — dirty pages stay until written back, which is exactly what we want
    (no data-loss semantics, purely a cache-hint).
    """
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return 0
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
        return size
    except (OSError, AttributeError) as e:  # AttributeError: no posix_fadvise (non-Linux)
        logger.debug('[cgroup] fadvise DONTNEED failed for %s: %s', path, e)
        return 0


def drop_files_cache(paths, min_bytes: int = 0) -> dict:
    """fadvise-DONTNEED every file in *paths* at least *min_bytes* large.

    Returns ``{'files': n, 'bytes': b}`` — files touched and total size
    advised (an upper bound on what the kernel may reclaim).
    """
    files = 0
    total = 0
    for p in paths:
        try:
            if min_bytes and os.path.getsize(p) < min_bytes:
                continue
        except OSError as _e:
            logger.debug('drop files cache: unreadable (%s)', _e)
            continue
        n = fadvise_dontneed(p)
        if n > 0:
            files += 1
            total += n
    return {'files': files, 'bytes': total}


def drop_logs_cache(log_dir: str = 'logs') -> dict:
    """fadvise every log file in *log_dir* above the size floor.

    Log files are the canonical write-once/grep-once payload: tofu appends
    them all day, agent run_commands grep 100 MB+ rotated files and leave the
    whole file sitting in our cgroup's page cache. Dropping them is pure win —
    the next grep re-faults from disk (FUSE) at trivial cost.
    """
    import glob
    try:
        min_bytes = int(os.environ.get('TOFU_CGROUP_LOGDROP_MIN_BYTES', str(1 << 20)))
    except (ValueError, TypeError) as _e:
        logger.debug('drop logs cache: unparseable/unexpected type (%s)', _e)
        min_bytes = 1 << 20
    try:
        paths = glob.glob(os.path.join(log_dir, '*.log*'))
    except Exception as e:
        logger.debug('[cgroup] log glob failed: %s', e)
        return {'files': 0, 'bytes': 0}
    return drop_files_cache(paths, min_bytes=min_bytes)


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
    # Page-cache relief: drop OUR one-shot log files' clean pages. This is the
    # lever that actually moves cgroup usage — heap caches are ~2 GiB while
    # cached logs/snapshots can be tens of GiB. Env-off switch for debugging.
    logs_dropped = {'files': 0, 'bytes': 0}
    if os.environ.get('TOFU_CGROUP_DROP_LOGS', '1') != '0':
        try:
            logs_dropped = drop_logs_cache()
        except Exception as e:
            logger.warning('[cgroup] log page-cache drop failed: %s', e)
    after = pressure()
    after_pct = after['pct'] if after else None
    logger.warning('[cgroup] relief (%s): dropped %d cache entries, malloc_trim=%s, '
                   'log_pages=%d files/%.1fMB, usage %.1f%% -> %.1f%%',
                   reason, dropped, trimmed,
                   logs_dropped['files'], logs_dropped['bytes'] / 1e6,
                   before_pct if before_pct is not None else -1.0,
                   after_pct if after_pct is not None else -1.0)
    return {'reason': reason, 'dropped': dropped, 'trimmed': trimmed,
            'log_pages_bytes': logs_dropped['bytes'],
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


# ── ④ rolling pressure journal + OOM-kill witness ──
#
# The next "Killed" must not be a mystery again. Every monitor tick appends a
# one-line JSON snapshot (usage/cache/kmem/tofu-RSS breakdown, plus the top-3
# RSS processes when under pressure) to logs/cgroup_pressure.log, ring-bounded.
# After a SIGKILL, the minute before death is on disk. The oom_kill counter
# watch turns "cgroup OOM fired" from a guess into a CRITICAL log line.

_JOURNAL_PATH = os.path.join('logs', 'cgroup_pressure.log')
_JOURNAL_MAX_BYTES = 4 << 20
_OOM_CONTROL_PATH = '/sys/fs/cgroup/memory/memory.oom_control'
_last_oom_kill_count: Optional[int] = None


def _read_memory_stat() -> dict:
    """Parse cache/rss from memory.stat + kmem counter. Empty dict on failure."""
    out = {}
    try:
        with open('/sys/fs/cgroup/memory/memory.stat', 'r') as f:
            for line in f:
                k, _, v = line.partition(' ')
                if k in ('cache', 'rss'):
                    try:
                        out[k] = int(v)
                    except ValueError as _e:
                        logger.debug('read memory stat: unparseable (%s)', _e)
                        pass
    except OSError as _e:
        logger.debug('read memory stat: unreadable (%s)', _e)
        pass
    kmem = _read_first_int(('/sys/fs/cgroup/memory/memory.kmem.usage_in_bytes',))
    if kmem is not None:
        out['kmem'] = kmem
    return out


def _self_rss_bytes() -> Optional[int]:
    """This process's RSS via /proc/self/statm. None on failure."""
    try:
        with open('/proc/self/statm', 'r') as f:
            fields = f.read().split()
        return int(fields[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError) as _e:
        logger.debug('self rss bytes: unreadable/unparseable/short/malformed (%s)', _e)
        return None


def _top_rss_processes(n: int = 3) -> list:
    """Top-n processes by RSS (same-uid visible), as [{'pid','comm','rss'}].

    Only called under pressure (>= relief threshold) — a /proc scan is a few
    ms and this runs at most every 30s. Best-effort: skips unreadable pids.
    """
    rows = []
    try:
        pids = [d for d in os.listdir('/proc') if d.isdigit()]
    except OSError as _e:
        logger.debug('top rss processes: unreadable (%s)', _e)
        return rows
    for pid in pids:
        try:
            with open('/proc/%s/statm' % pid, 'r') as f:
                rss = int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
            with open('/proc/%s/comm' % pid, 'r') as f:
                comm = f.read().strip()
            rows.append({'pid': int(pid), 'comm': comm, 'rss': rss})
        except (OSError, ValueError, IndexError) as _e:
            logger.debug('top rss processes: unreadable/unparseable/short/malformed (%s)', _e)
            continue
    rows.sort(key=lambda r: -r['rss'])
    return rows[:n]


def write_pressure_journal(snap: dict) -> bool:
    """Append one JSON snapshot line to the ring-bounded pressure journal."""
    if os.environ.get('TOFU_CGROUP_JOURNAL', '1') == '0':
        return False
    import json as _json
    import time as _time
    stat = _read_memory_stat()
    rec = {
        'ts': round(_time.time(), 1),
        'pct': round(snap['pct'], 1),
        'usage_gib': round(_gib(snap['usage']), 2),
        'cache_gib': round(_gib(stat.get('cache', 0)), 2) if stat else None,
        'kmem_gib': round(_gib(stat.get('kmem', 0)), 2) if stat else None,
        'self_rss_gib': round(_gib(_self_rss_bytes() or 0), 2),
    }
    relief_pct = _env_pct('TOFU_CGROUP_RELIEF_PCT', 92.0)
    if snap['pct'] >= relief_pct:
        rec['top'] = [{'comm': r['comm'], 'rss_gib': round(_gib(r['rss']), 2)}
                      for r in _top_rss_processes()]
    try:
        os.makedirs(os.path.dirname(_JOURNAL_PATH), exist_ok=True)
        line = _json.dumps(rec, separators=(',', ':')) + '\n'
        # Ring bound: when over budget, keep the newest half.
        try:
            if os.path.getsize(_JOURNAL_PATH) > _JOURNAL_MAX_BYTES:
                with open(_JOURNAL_PATH, 'r', encoding='utf-8', errors='replace') as f:
                    tail = f.read()[-_JOURNAL_MAX_BYTES // 2:]
                tmp = _JOURNAL_PATH + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(tail)
                os.replace(tmp, _JOURNAL_PATH)
        except OSError as _e:
            logger.debug('write pressure journal: unreadable (%s)', _e)
            pass
        with open(_JOURNAL_PATH, 'a', encoding='utf-8') as f:
            f.write(line)
        return True
    except OSError as e:
        logger.debug('[cgroup] pressure journal write failed: %s', e)
        return False


def check_oom_kill_count() -> bool:
    """Watch the cgroup oom_kill counter; CRITICAL + audit when it increments.

    Returns True on the tick that detects a NEW OOM kill. This is the only
    in-process signal that proves the memcg OOM killer fired — dmesg is
    unreachable from inside the container.
    """
    global _last_oom_kill_count
    count = None
    try:
        with open(_OOM_CONTROL_PATH, 'r') as f:
            for line in f:
                if line.startswith('oom_kill '):
                    count = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError) as _e:
        logger.debug('check oom kill count: unreadable/unparseable/short/malformed (%s)', _e)
        return False
    if count is None:
        return False
    prev = _last_oom_kill_count
    _last_oom_kill_count = count
    if prev is not None and count > prev:
        logger.critical(
            '[cgroup] OOM KILL CONFIRMED: memory.oom_control oom_kill %d -> %d — '
            'the kernel memcg OOM killer fired inside our cgroup. See %s for the '
            'pressure curve leading up to it.', prev, count, _JOURNAL_PATH)
        audit_log('cgroup_oom_kill_confirmed', prev=prev, count=count)
        return True
    return False


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
    try:
        write_pressure_journal(snap)
        check_oom_kill_count()
    except Exception as e:  # journaling must never break the relief path
        logger.debug('[cgroup] journal/oom-watch tick failed: %s', e)
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
    except (ValueError, TypeError) as _e:
        logger.debug('check request headroom: unparseable/unexpected type (%s)', _e)
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
    except Exception as _e:
        logger.debug('approx body bytes: failed (%s)', _e)
        return 0


__all__ = [
    'MemoryPressureError',
    'mem_limit_bytes', 'mem_usage_bytes', 'swap_total_bytes', 'pressure',
    'malloc_trim', 'relieve_memory',
    'fadvise_dontneed', 'drop_files_cache', 'drop_logs_cache',
    'write_pressure_journal', 'check_oom_kill_count',
    'startup_self_check',
    'run_monitor_once', 'start_monitor', 'stop_monitor',
    'check_request_headroom', 'approx_body_bytes',
]
