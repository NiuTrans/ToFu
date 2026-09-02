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
  ③ check_request_headroom() — before serialising a LARGE LLM request body,
     use the pressure percentage only as a trigger, then compare absolute free
     bytes with a request-sized peak-allocation envelope. Trim once only when
     that concrete envelope does not fit; refuse only if it still does not fit.

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
  TOFU_CGROUP_MATERIAL_PCT       default 0.1 — % of the cgroup limit a reclaim
                                               must reach to count as effective
                                               relief (below it, the streak
                                               towards the ineffective-relief
                                               CRITICAL keeps counting)
  TOFU_CGROUP_RELIEF_COOLDOWN_SEC default 600 — after repeated ineffective
                                               shared-cgroup relief, keep
                                               journaling but stop cache churn
  TOFU_CGROUP_JOURNAL            default 1   — rolling pressure journal to
                                               logs/cgroup_pressure.log
  TOFU_PROCESS_RSS_RELIEF_MB     personal default min(2048, 50% of cgroup MiB)
                                               (distributed: 4096) — also
                                               trim when this process's RSS
                                               alone crosses the ceiling;
                                               0 disables
  TOFU_PROCESS_RSS_COOLDOWN_SEC  default 300 — minimum delay between RSS-only
                                               relief passes
  TOFU_PROCESS_RSS_RECYCLE_MB    personal default min(3072, 70% of cgroup MiB)
                                               (distributed: 8192) — after
                                               relief, request one
                                               graceful worker recycle if RSS
                                               is still above this ceiling;
                                               0 disables

What relief can and cannot do (measured 2026-07-31, do not re-litigate)
----------------------------------------------------------------------
Defense ② can only free what THIS process owns: its heap caches and the page
cache of its own log files. On the shared cgroup that is noise. Over 406 real
reliefs, cgroup usage fell 18.3 GiB in total while 367 of them moved it by
exactly 0.0%, and the breakdown (12037 journal samples) was kmem 45–126 GiB
plus cache 55–156 GiB against a tofu RSS of only 0.16–9.9 GiB. The rest belongs
to sibling processes and FUSE slab, which we cannot reach.

So relief is a best-effort courtesy, NOT a mitigation, and the logging must not
imply otherwise: the relief line reports the MEASURED usage delta, and a run of
immaterial reclaims escalates once to CRITICAL saying the squeeze is external.
The old line printed the summed apparent size of the files advised, which
overstated the reclaim by 234x and made 341 consecutive near-OOM ticks read as
though they were being handled.
"""

from __future__ import annotations

import ctypes
import os
import threading
from typing import Optional

from lib.log import audit_log, get_logger
from runtime_guards import deployment_resource_default

logger = get_logger(__name__)


class MemoryPressureError(RuntimeError):
    """A large request cannot fit its bounded peak-allocation envelope."""


# Serialising/translating an outbound body creates temporary copies in JSON,
# provider adapters and the HTTP stack. Eight times the cheap body estimate is
# intentionally conservative; 64 MiB covers fixed interpreter/transport
# overhead for a body near the 2 MB guard floor. Crucially, neither value
# scales with a *shared* 220 GiB cgroup: a percentage-only rule made a 2.3 MB
# request fail while 1.4 GiB was still free and Tofu itself used <40 MiB.
_REQUEST_HEADROOM_FLOOR_BYTES = 64 * 1024 * 1024
_REQUEST_PEAK_ALLOCATION_MULTIPLIER = 8


def _required_request_headroom(approx_bytes: int) -> int:
    """Return the absolute free-byte envelope needed for one request."""
    return max(
        _REQUEST_HEADROOM_FLOOR_BYTES,
        max(0, int(approx_bytes)) * _REQUEST_PEAK_ALLOCATION_MULTIPLIER,
    )


def _available_headroom(snapshot: dict) -> int:
    """Return non-negative cgroup bytes available in ``snapshot``."""
    return max(0, int(snapshot['limit']) - int(snapshot['usage']))


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


# Files already advised, keyed path -> (mtime_ns, size). Re-advising a file
# whose bytes have not changed is a guaranteed no-op: its clean pages were
# already dropped, so the kernel has nothing left to reclaim. Measured
# 2026-07-31 — re-advising the same 57 logs moved cgroup usage by +0.01 GiB
# (i.e. zero) while costing 57 syscalls every 30s, and, worse, kept reporting
# their full apparent size as though that were a fresh reclaim.
_advised_state: dict = {}
_ADVISED_MAX = 4096


def drop_files_cache(paths, min_bytes: int = 0, skip_unchanged: bool = True) -> dict:
    """fadvise-DONTNEED every file in *paths* at least *min_bytes* large.

    Returns ``{'files': n, 'bytes': b, 'skipped': s}`` — files actually advised,
    their total size, and how many were skipped as unchanged since the last
    advise.

    ``bytes`` is the size ADVISED, which is an upper bound on what the kernel
    may reclaim — NOT a measurement of reclaimed memory. Callers that want to
    report a reclaim figure must measure cgroup usage before/after instead; see
    :func:`relieve_memory`.

    Set ``skip_unchanged=False`` to force a re-advise (useful when the caller
    knows the page cache was repopulated by something other than a write).
    """
    files = 0
    total = 0
    skipped = 0
    for p in paths:
        try:
            st = os.stat(p)
        except OSError as _e:
            logger.debug('drop files cache: unreadable (%s)', _e)
            continue
        if min_bytes and st.st_size < min_bytes:
            continue
        key = (st.st_mtime_ns, st.st_size)
        if skip_unchanged and _advised_state.get(p) == key:
            skipped += 1
            continue
        n = fadvise_dontneed(p)
        if n > 0:
            files += 1
            total += n
            if len(_advised_state) >= _ADVISED_MAX:
                _advised_state.clear()
            _advised_state[p] = key
    return {'files': files, 'bytes': total, 'skipped': skipped}


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


# Consecutive reliefs that failed to move cgroup usage down. Relief works on
# OUR OWN reclaimable bytes; on a SHARED cgroup the usage can be dominated by
# sibling processes and FUSE slab, which we structurally cannot reach. When
# that is the case, repeating the same WARNING every 30s misrepresents an
# unmitigated squeeze as a handled one — so escalate ONCE and say plainly that
# the pressure originates outside this process.
#
# "Ineffective" is a MATERIALITY test, not ``reclaimed > 0``. On a live shared
# cgroup the usage counter jitters constantly, so a strict >0 test sees an
# occasional noise-sized reclaim and resets the streak — measured on the real
# cgroup, reliefs returned 39MB / 344KB / 180KB / 0 / 0 / 180KB…, which kept
# the streak below the limit forever and made the alarm STRUCTURALLY unable to
# fire. That is the same class of defect as the one this epic is about (an
# instrument that cannot report the condition it exists to report), so the bar
# is a fraction of the cgroup limit: a reclaim too small to change even the
# printed usage percentage is not relief.
_ineffective_reliefs = 0
_ineffective_escalated = False
_relief_suppressed_until = 0.0
_INEFFECTIVE_LIMIT = 5


def _material_reclaim_bytes(limit: Optional[int]) -> float:
    """Smallest reclaim worth calling effective, in bytes.

    Defaults to 0.1% of the cgroup limit (≈225 MB on a 220 GiB cgroup) — just
    above the 0.05% that would round away in the ``usage %.1f%%`` we log.
    """
    try:
        pct = float(os.environ.get('TOFU_CGROUP_MATERIAL_PCT', '0.1'))
    except (ValueError, TypeError) as _e:
        logger.debug('material reclaim: unparseable (%s)', _e)
        pct = 0.1
    if not limit or limit <= 0:
        return 0.0
    return limit * pct / 100.0


def _relief_cooldown_seconds() -> float:
    try:
        value = float(os.environ.get(
            'TOFU_CGROUP_RELIEF_COOLDOWN_SEC', '600'))
    except (ValueError, TypeError) as exc:
        logger.debug('relief cooldown: unparseable (%s)', exc)
        value = 600.0
    return max(30.0, min(3600.0, value))


def relieve_memory(reason: str) -> dict:
    """Drop our own reclaimable caches + trim heap. Logs usage% before/after.

    The log line reports the MEASURED reclaim (cgroup usage before minus after),
    never the apparent size of the files advised. Those two numbers differ by
    orders of magnitude: measured live 2026-07-31, 406 reliefs reported a
    cumulative 4272 GB of "log_pages" while cgroup usage fell by 18.3 GiB in
    total — a 234x overstatement, and 367 of those reliefs moved usage by
    exactly 0.0%. An instrument that reports a large reclaim while reclaiming
    nothing is worse than no instrument: it made 341 consecutive near-OOM
    ticks look like they were being handled.

    Returns a small stats dict. Safe to call anywhere; never raises.
    """
    global _ineffective_reliefs, _ineffective_escalated
    global _relief_suppressed_until
    before = pressure()
    before_pct = before['pct'] if before else None
    dropped = 0
    try:
        from lib.ttl_cache import clear_all_caches
        dropped = clear_all_caches()
    except Exception as e:
        logger.warning('[cgroup] cache clear during relief failed: %s', e)
    trimmed = malloc_trim()
    # Page-cache relief: drop OUR one-shot log files' clean pages. Env-off
    # switch for debugging.
    logs_dropped = {'files': 0, 'bytes': 0, 'skipped': 0}
    if os.environ.get('TOFU_CGROUP_DROP_LOGS', '1') != '0':
        try:
            logs_dropped = drop_logs_cache()
        except Exception as e:
            logger.warning('[cgroup] log page-cache drop failed: %s', e)
    after = pressure()
    after_pct = after['pct'] if after else None
    # MEASURED reclaim — the only honest number available. Negative means usage
    # grew during the relief (siblings allocating faster than we free), which is
    # reported as 0 reclaimed rather than hidden.
    reclaimed_bytes = None
    if before is not None and after is not None:
        reclaimed_bytes = max(0, before['usage'] - after['usage'])
    logger.warning('[cgroup] relief (%s): dropped %d cache entries, malloc_trim=%s, '
                   'advised %d files (%d unchanged/skipped), '
                   'RECLAIMED %s, usage %.1f%% -> %.1f%%',
                   reason, dropped, trimmed,
                   logs_dropped.get('files', 0), logs_dropped.get('skipped', 0),
                   ('%.1fMB' % (reclaimed_bytes / 1e6)) if reclaimed_bytes is not None
                   else 'unknown',
                   before_pct if before_pct is not None else -1.0,
                   after_pct if after_pct is not None else -1.0)

    # Escalate a structurally-ineffective relief exactly once, then renew the
    # cooldown after each later probe until a material reclaim resets it.
    if reclaimed_bytes is not None:
        _material = _material_reclaim_bytes((before or {}).get('limit'))
        if reclaimed_bytes < _material:
            _ineffective_reliefs += 1
        else:
            _ineffective_reliefs = 0
            _ineffective_escalated = False
            _relief_suppressed_until = 0.0
        if _ineffective_reliefs >= _INEFFECTIVE_LIMIT:
            import time as _time
            cooldown_seconds = _relief_cooldown_seconds()
            _relief_suppressed_until = (
                _time.monotonic() + cooldown_seconds)
            if not _ineffective_escalated:
                _ineffective_escalated = True
                logger.critical(
                    '[cgroup] RELIEF IS INEFFECTIVE: %d consecutive reliefs '
                    'reclaimed 0 bytes while usage sits at %.1f%%. This process '
                    'cannot relieve this pressure — what we can drop (our heap '
                    'caches + our own log page cache) is noise against the total. '
                    'On a SHARED cgroup the usage is dominated by sibling '
                    'processes and FUSE slab, which we structurally cannot reach. '
                    'Treat this as an unmitigated environment squeeze: an OOM '
                    'SIGKILL is possible at any time and no in-process action '
                    'will prevent it. Aggregate relief is cooling down for %.0fs; '
                    'pressure journaling and process-RSS limits remain active.',
                    _ineffective_reliefs,
                    after_pct if after_pct is not None else -1.0,
                    cooldown_seconds)
                audit_log(
                    'cgroup_relief_ineffective',
                    consecutive=_ineffective_reliefs,
                    usage_pct=(round(after_pct, 1)
                               if after_pct is not None else None))

    return {'reason': reason, 'dropped': dropped, 'trimmed': trimmed,
            'log_pages_bytes': logs_dropped.get('bytes', 0),
            'advised_files': logs_dropped.get('files', 0),
            'skipped_unchanged': logs_dropped.get('skipped', 0),
            'reclaimed_bytes': reclaimed_bytes,
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
        parent = os.path.dirname(_JOURNAL_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = _json.dumps(rec, separators=(',', ':')) + '\n'
        from lib.json_store import locked_path, write_bytes_atomic
        from lib.log_policy import LOG_FILE_MODE
        from lib.log_retention import append_bytes_locked
        # Trimming and appending are one transaction.  Without the stable
        # sidecar lock, two server processes can each replace the journal with
        # an older tail and silently discard the other's newest record.
        with locked_path(_JOURNAL_PATH):
            try:
                if os.path.getsize(_JOURNAL_PATH) > _JOURNAL_MAX_BYTES:
                    keep = max(1, _JOURNAL_MAX_BYTES // 2)
                    with open(_JOURNAL_PATH, 'rb') as f:
                        size = f.seek(0, os.SEEK_END)
                        start = max(0, size - keep)
                        f.seek(start)
                        tail = f.read()
                    # A byte-window can start in the middle of a UTF-8 JSON
                    # record.  Retain only complete newline-delimited records.
                    if start:
                        boundary = tail.find(b'\n')
                        tail = tail[boundary + 1:] if boundary >= 0 else b''
                    write_bytes_atomic(
                        _JOURNAL_PATH, tail, fsync=False, mode=LOG_FILE_MODE)
            except FileNotFoundError as error:
                logger.debug('write pressure journal: no prior journal (%s)',
                             error)
                pass
            except OSError as _e:
                logger.debug('write pressure journal: unreadable (%s)', _e)
            append_bytes_locked(_JOURNAL_PATH, line.encode('utf-8'))
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
_last_process_rss_relief_at = 0.0
_process_rss_recycle_requested = False


def _process_rss_relief_limit_bytes() -> Optional[int]:
    """Return the process-local RSS relief threshold, or None when disabled.

    The shared cgroup can be roomy while this one process has retained many
    GiB of thread-arena/history allocations.  That shape is independently
    dangerous: it raises restart latency and makes Tofu the first OOM victim
    during the next unrelated cgroup spike.  Clamp tiny values to 256 MiB so
    a typo cannot turn the 30-second monitor into a permanent cache flusher.
    """
    raw = os.environ.get('TOFU_PROCESS_RSS_RELIEF_MB', '')
    default_mb = float(deployment_resource_default(
        'TOFU_PROCESS_RSS_RELIEF_MB', os.environ))
    try:
        mb = float(raw) if raw else default_mb
    except (ValueError, TypeError) as e:
        logger.warning(
            '[cgroup] invalid TOFU_PROCESS_RSS_RELIEF_MB; using %.0f: %s',
            default_mb, e)
        mb = default_mb
    if mb <= 0:
        return None
    if not raw:
        cgroup_limit = mem_limit_bytes()
        if cgroup_limit is not None:
            mb = min(mb, (cgroup_limit / (1 << 20)) * 0.50)
    return int(max(256.0, mb) * (1 << 20))


def _process_rss_recycle_limit_bytes() -> Optional[int]:
    """Return the post-relief RSS ceiling that requests a graceful recycle."""
    raw = os.environ.get('TOFU_PROCESS_RSS_RECYCLE_MB', '')
    default_mb = float(deployment_resource_default(
        'TOFU_PROCESS_RSS_RECYCLE_MB', os.environ))
    try:
        mb = float(raw) if raw else default_mb
    except (ValueError, TypeError) as e:
        logger.warning(
            '[cgroup] invalid TOFU_PROCESS_RSS_RECYCLE_MB; using %.0f: %s',
            default_mb, e)
        mb = default_mb
    if mb <= 0:
        return None
    if not raw:
        cgroup_limit = mem_limit_bytes()
        if cgroup_limit is not None:
            mb = min(mb, (cgroup_limit / (1 << 20)) * 0.70)
    return int(max(384.0, mb) * (1 << 20))


def _maybe_relieve_process_rss(now: Optional[float] = None,
                               recycle_callback=None) -> Optional[dict]:
    """Relieve oversized RSS and request one graceful recycle at the hard cap.

    ``malloc_trim`` cannot return live/fragmented native allocations.  If RSS
    remains above the hard ceiling after a relief attempt, letting the process
    continue growing merely delegates recovery to the untrappable kernel OOM
    killer.  A caller-supplied callback converts that into a controlled drain;
    the external lifecycle manager then restores a small fresh worker.
    """
    global _last_process_rss_relief_at, _process_rss_recycle_requested
    relief_limit = _process_rss_relief_limit_bytes()
    recycle_limit = _process_rss_recycle_limit_bytes()
    rss_before = _self_rss_bytes()
    if rss_before is None:
        return None
    needs_relief = relief_limit is not None and rss_before >= relief_limit
    needs_recycle_check = recycle_limit is not None and rss_before >= recycle_limit
    if not needs_relief and not needs_recycle_check:
        return None

    stats = None
    try:
        cooldown = float(
            os.environ.get('TOFU_PROCESS_RSS_COOLDOWN_SEC', '') or '300')
    except (ValueError, TypeError) as e:
        logger.warning('[cgroup] invalid TOFU_PROCESS_RSS_COOLDOWN_SEC; using 300: %s', e)
        cooldown = 300.0
    cooldown = max(30.0, cooldown)
    if now is None:
        import time as _time
        now = _time.monotonic()

    if needs_relief and now - _last_process_rss_relief_at >= cooldown:
        # Latch before doing work: if a defensive action itself raises, the
        # monitor must not retry and log-storm every tick.
        _last_process_rss_relief_at = now
        stats = relieve_memory(
            'process RSS %.1fMiB >= %.1fMiB'
            % (rss_before / (1 << 20), relief_limit / (1 << 20)))
        if not isinstance(stats, dict):
            stats = {}
        rss_after = _self_rss_bytes()
        stats.update({
            'process_rss_before': rss_before,
            'process_rss_after': rss_after,
            'process_rss_limit': relief_limit,
        })
        logger.warning(
            '[cgroup] process-RSS relief: %.1fMiB -> %s (limit %.1fMiB)',
            rss_before / (1 << 20),
            ('%.1fMiB' % (rss_after / (1 << 20))) if rss_after is not None else 'unknown',
            relief_limit / (1 << 20))
    else:
        rss_after = rss_before

    if (recycle_callback is not None and recycle_limit is not None
            and rss_after is not None and rss_after >= recycle_limit
            and not _process_rss_recycle_requested):
        reason = ('process RSS %.1fMiB remains >= %.1fMiB hard ceiling after relief'
                  % (rss_after / (1 << 20), recycle_limit / (1 << 20)))
        # One-shot latch before callback: requesting shutdown repeatedly from a
        # 30-second monitor can turn the second request into a force-quit.
        _process_rss_recycle_requested = True
        logger.critical('[cgroup] %s — requesting graceful worker recycle', reason)
        audit_log('process_rss_recycle_requested',
                  rss_bytes=rss_after, limit_bytes=recycle_limit)
        try:
            recycle_callback(reason)
        except Exception as e:
            # The callback did not accept the request; allow a later tick to
            # retry instead of permanently suppressing the only hard guard.
            _process_rss_recycle_requested = False
            logger.error('[cgroup] graceful worker recycle request failed: %s', e)
        if stats is None:
            stats = {
                'process_rss_before': rss_before,
                'process_rss_after': rss_after,
                'process_rss_limit': relief_limit,
            }
        stats['process_rss_recycle_limit'] = recycle_limit
        stats['process_rss_recycle_requested'] = _process_rss_recycle_requested
    return stats


def run_monitor_once(recycle_callback=None) -> Optional[dict]:
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
    # Process-local high water is independent of aggregate cgroup pressure.
    # Run it first and never double-relieve on the same tick.
    rss_relief = _maybe_relieve_process_rss(recycle_callback=recycle_callback)
    if rss_relief is not None:
        return rss_relief
    relief_pct = _env_pct('TOFU_CGROUP_RELIEF_PCT', 92.0)
    if snap['pct'] >= relief_pct:
        import time as _time
        if _time.monotonic() < _relief_suppressed_until:
            return None
        return relieve_memory('monitor %.1f%% >= %.0f%%' % (snap['pct'], relief_pct))
    return None


def start_monitor(recycle_callback=None) -> bool:
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
                    run_monitor_once(recycle_callback=recycle_callback)
                except Exception as e:
                    logger.warning('[cgroup] monitor tick failed: %s', e)

        _monitor_thread = threading.Thread(target=_loop, name='cgroup-mem-monitor',
                                           daemon=True)
        _monitor_thread.start()
        return True


def stop_monitor(timeout: float = 2.0) -> bool:
    """Signal and bounded-join the pressure monitor."""
    global _monitor_thread
    _monitor_stop.set()
    with _monitor_lock:
        thread = _monitor_thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[cgroup] invalid stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _monitor_lock:
        if _monitor_thread is thread:
            _monitor_thread = None
    return True


# ── ③ large-request headroom guard ──

def check_request_headroom(ident: str = '', approx_bytes: int = 0) -> tuple[bool, Optional[str]]:
    """Pre-flight guard for a LARGE outbound request body.

    Returns ``(ok, reason)``:
      - ``(True, None)``  → proceed (headroom fine, body small, or cgroup
        unreadable — the safe default everywhere off-cgroup).
      - ``(False, reason)`` → pressure is above the trigger and the absolute
        free bytes still cannot cover this request's bounded peak allocation
        after a trim. The caller should shrink the derived payload before it
        gives up. A diagnostic with ident, size and headroom is logged here.

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
    required = _required_request_headroom(approx_bytes)
    if _available_headroom(snap) >= required:
        logger.debug(
            '[cgroup] pressure trigger reached for %s (%.1f%%), but %.1f MiB '
            'absolute headroom covers the %.1f MiB request envelope; allowing',
            ident or '?', snap['pct'], _available_headroom(snap) / (1 << 20),
            required / (1 << 20))
        return True, None

    # The concrete request envelope does not fit: trim once, then re-measure.
    relieve_memory('pre-request %s %.1f%%' % (ident or '?', snap['pct']))
    snap2 = pressure()
    measured = snap2 or snap
    pct2 = measured['pct']
    headroom2 = _available_headroom(measured)
    if pct2 < req_pct or headroom2 >= required:
        return True, None
    reason = ('cgroup %.1f%% full (%.1f/%.1f GiB) after trim; %.1f MiB '
              'headroom < %.1f MiB request envelope — refusing ident=%s '
              'body=%.1fMB until the derived payload is reduced'
              % (pct2, _gib(measured['usage']), _gib(measured['limit']),
                 headroom2 / (1 << 20), required / (1 << 20),
                 ident or '?', approx_bytes / 1e6))
    logger.error('[cgroup] %s', reason)
    audit_log('cgroup_request_refused', ident=ident,
              body_bytes=approx_bytes, usage_pct=round(pct2, 1),
              headroom_bytes=headroom2, required_headroom_bytes=required)
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
