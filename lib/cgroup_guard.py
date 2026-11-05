"""Shared-cgroup memory-pressure defenses (startup self-check + best-effort relief).

Context (2026-07-20): Tofu often runs in a container whose cgroup memory limit
is the WHOLE machine (e.g. 200 GiB) and is SHARED with sibling processes + a
huge FUSE page/slab cache. When that shared cgroup fills to the ceiling (zero
swap), the kernel OOM killer SIGKILLs the highest-RSS process — which is
usually tofu — with a bare "Killed" and no traceback.

We cannot fix the root cause in code: the cgroup is shared, we lack
``CAP_SYS_RESOURCE`` (so we cannot lower our own ``oom_score_adj`` — the kernel
floors it at 0, verified live), and there is no swap. What we CAN do is hand
back memory this process owns and turn "mystery Killed" into logged evidence.
Two defenses, both env-tunable and neither ever blocking work:

  ① startup_self_check()  — at boot, if the cgroup is already near-full AND
     there is no swap, emit a CRITICAL log + audit record so the operator has
     durable evidence this is an environment squeeze, not a tofu bug. Log only.
  ② start_monitor()       — a low-frequency (>=30s) daemon thread that, when
     reclaim-aware AGGREGATE pressure crosses the relief threshold, drops our
     own reclaimable caches and calls malloc_trim(0) to hand free heap back to
     the OS, shrinking our RSS. Best-effort: passes that reclaim ~nothing back
     off, and no pass refuses, shrinks, recycles or throttles user work.

Evidence (③, on every monitor tick): a rolling pressure journal and a watch of
the cgroup OOM-kill counter, so the next "Killed" is not a mystery.

Hardware never limits work (owner decision 2026-10-24, a fixed policy rather
than a tuning choice): CPU count, memory capacity/headroom, process RSS and
cgroup pressure never refuse, shrink, recycle or throttle user work; memory
exhaustion is left to the OS. That decision removed the large-request
memory-headroom refusal, process-RSS relief and the in-process worker recycle.
They must not return.

Everything degrades to a NO-OP when the cgroup / /proc is unreadable (bare
metal, macOS, restricted sandbox): a reader that cannot see ``memory.current``
returns ``None`` and every defense treats "unknown" as "proceed, do nothing".

Env vars (all optional):
  TOFU_CGROUP_WARN_PCT           default 90  — ① self-check trigger
  TOFU_CGROUP_RELIEF_PCT         default 92  — ② monitor relief trigger on
                                               effective (reclaim-aware) %;
                                               an episode ends 2 points below
  TOFU_CGROUP_POLL_SEC           default 30  — ② poll interval (clamped >=30)
  TOFU_CGROUP_DROP_LOGS          default 1   — relief also fadvise-drops logs/*.log*
  TOFU_CGROUP_LOGDROP_MIN_BYTES  default 1 MiB — size floor for the log drop
  TOFU_CGROUP_MATERIAL_PCT       default 0.1 — % of the cgroup limit a reclaim
                                               must reach to count as effective
                                               relief (below it, the pass defers
                                               the next one and counts towards
                                               the ineffective-relief CRITICAL);
                                               Tofu RSS growth this large
                                               reopens a deferred relief
  TOFU_CGROUP_RELIEF_COOLDOWN_SEC default 600 — deferral after the pass that
                                               escalates repeated no-progress
                                               relief; earlier no-progress
                                               passes defer 1/16..1/2 of it,
                                               later ones double to 1h
  TOFU_CGROUP_JOURNAL            default 1   — ③ rolling pressure journal to
                                               logs/cgroup_pressure.log

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

Measured again 2026-10-23: the shared 220 GiB cgroup read raw 92-96% while
~97 GiB of it was other tenants' inactive page cache (effective ~50%). A raw
trigger ran relief 118 times an hour all day; each pass flushed Tofu's caches
and re-trimmed the same few hundred MB of Tofu heap while usage never moved.
Relief therefore triggers on effective pressure, an episode ends only
``_RELIEF_REARM_HYSTERESIS_PCT`` below the trigger, every no-progress pass
doubles the deferral (Tofu RSS growth past the material floor reopens it), and
repeated no-progress lines are INFO.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from typing import Optional

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def _observed_reclaim_bytes(before: Optional[int],
                            after: Optional[int]) -> Optional[int]:
    """Return a non-negative observed byte drop, or None when unreadable."""
    if before is None or after is None:
        return None
    return max(0, int(before) - int(after))


def _measured_mb(value: Optional[int]) -> str:
    return ('%.1fMB' % (value / 1e6)) if value is not None else 'unknown'


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

    When memory.stat is readable the snapshot ALSO carries a reclaim-aware
    view: ``reclaimable`` (inactive_file — page cache the kernel hands back
    to any allocation without writeback/swap), ``effective_usage`` and
    ``effective_pct``.  Raw ``pct`` counts page cache as used; on a shared
    host whose cgroup fills with OTHER tenants' FUSE/browser cache it reads
    as permanently near-full while real allocations still succeed by
    reclaiming cache.  The self-check alarm and the relief trigger use
    ``effective_pct``: relief can only return Tofu's own heap, caches and
    log pages, so a percentage made of other tenants' reclaimable cache is
    not work it can do.  Raw ``pct`` is reported alongside and is the
    conservative fallback when memory.stat is unreadable.
    """
    limit = mem_limit_bytes()
    usage = mem_usage_bytes()
    if limit is None or usage is None or limit <= 0:
        return None
    snap = {
        'limit': limit,
        'usage': usage,
        'pct': 100.0 * usage / float(limit),
        'swap': swap_total_bytes(),
    }
    reclaimable = reclaimable_file_bytes(_read_memory_stat())
    if reclaimable is not None:
        effective_usage = max(0, usage - reclaimable)
        snap['reclaimable'] = reclaimable
        snap['effective_usage'] = effective_usage
        snap['effective_pct'] = 100.0 * effective_usage / float(limit)
    return snap


def _effective_pct(snap: dict) -> float:
    """Pressure percentage for alarm/relief decisions (reclaim-aware when known)."""
    try:
        return float(snap.get('effective_pct', snap.get('pct', 0)) or 0)
    except (TypeError, ValueError):
        return float(snap.get('pct', 0) or 0)


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
# Process RSS right after the latest relief pass. Growth past the material
# floor since then is Tofu-owned memory a new pass may return, so it reopens a
# deferred relief instead of waiting out the back-off.
_relief_rss_baseline: Optional[int] = None
_INEFFECTIVE_LIMIT = 5
_MAX_RELIEF_COOLDOWN_SECONDS = 3600.0
_MAX_RELIEF_BACKOFF_EXPONENT = 7  # 30s minimum base reaches the 1h cap.
# Percentage points of the cgroup limit below the trigger that end an episode.
# Live 2026-10-24 the percentage moved up to 0.4 points per 5 s; without a
# band, dips to 91.1-91.9% under a 92% trigger re-armed per-tick relief.
_RELIEF_REARM_HYSTERESIS_PCT = 2.0


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


def _no_progress_deferral_seconds(streak: int) -> float:
    """Delay before the next aggregate relief after *streak* no-progress passes.

    One doubling series anchored at the configured cooldown: the pass that
    escalates (``streak == _INEFFECTIVE_LIMIT``) waits exactly that value,
    earlier passes wait 1/16..1/2 of it and later probes double to the
    one-hour ceiling. With the default this is 37.5, 75, 150, 300, 600, 1200,
    2400, 3600s, so a pass that reclaimed ~nothing never repeats on the next
    30-second monitor tick.
    """
    try:
        value = float(os.environ.get(
            'TOFU_CGROUP_RELIEF_COOLDOWN_SEC', '600'))
    except (ValueError, TypeError) as exc:
        logger.debug('relief cooldown: unparseable (%s)', exc)
        value = 600.0
    base_seconds = max(30.0, min(_MAX_RELIEF_COOLDOWN_SECONDS, value))
    exponent = min(_MAX_RELIEF_BACKOFF_EXPONENT,
                   max(1, int(streak)) - _INEFFECTIVE_LIMIT)
    return min(_MAX_RELIEF_COOLDOWN_SECONDS, base_seconds * (2.0 ** exponent))


def _reset_aggregate_relief_backoff() -> bool:
    """Re-arm aggregate relief after effective reclaim or pressure recovery."""
    global _ineffective_reliefs, _ineffective_escalated
    global _relief_suppressed_until
    had_backoff = bool(
        _ineffective_reliefs or _ineffective_escalated
        or _relief_suppressed_until)
    _ineffective_reliefs = 0
    _ineffective_escalated = False
    _relief_suppressed_until = 0.0
    return had_backoff


def _tofu_rss_growth_since_relief() -> Optional[int]:
    """Bytes this process's RSS grew since the latest relief pass, or None."""
    rss_bytes = _self_rss_bytes()
    if rss_bytes is None or _relief_rss_baseline is None:
        return None
    return rss_bytes - _relief_rss_baseline


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

    A pass below the material-reclaim floor defers the next aggregate relief
    (see :func:`_no_progress_deferral_seconds`); only the first no-progress
    pass in a row logs at WARNING, repeats log at INFO, and the escalation is
    one CRITICAL per episode.

    Returns a small stats dict. Safe to call anywhere; never raises.
    """
    global _ineffective_reliefs, _ineffective_escalated
    global _relief_suppressed_until, _relief_rss_baseline
    started_at = time.monotonic()
    before = pressure()
    before_pct = before['pct'] if before else None
    process_rss_before = _self_rss_bytes()
    memory_stat_before = _read_memory_stat()
    dropped = 0
    try:
        from lib.ttl_cache import clear_all_caches
        dropped = clear_all_caches()
    except Exception as e:
        logger.warning('[cgroup] cache clear during relief failed: %s', e)
    # Do not import token counting while under pressure. If its exact-usage
    # tier is already live, its entries are reconstructible after the next
    # provider response and safe to release.
    usage_cache_module = sys.modules.get('lib.token_counter.usage_cache')
    if usage_cache_module is not None:
        try:
            dropped += usage_cache_module.clear_usage_cache()
        except Exception as e:
            logger.warning(
                '[cgroup] usage-cache clear during relief failed: %s', e)
    # Working-tab affinity is also reconstructible: its next miss safely
    # reseeds from list_tabs. Never import the browser stack under pressure.
    work_tab_module = sys.modules.get('lib.browser._resolve')
    if work_tab_module is not None:
        try:
            dropped += work_tab_module.clear_work_tab_cache()
        except Exception as e:
            logger.warning(
                '[cgroup] work-tab clear during relief failed: %s', e)
    # Catalog indexes are reconstructible. Task history and frozen provider
    # declarations are execution state and survive pressure reclamation.
    tool_search_module = sys.modules.get('lib.mcp.tool_search')
    if tool_search_module is not None:
        try:
            dropped += tool_search_module.reclaim_tool_search_indexes()
        except Exception as e:
            logger.warning(
                '[cgroup] tool-search clear during relief failed: %s', e)
    trimmed = malloc_trim()
    # This middle sample brackets only process-owned cache clearing + heap trim.
    # It is still an observed shared-cgroup window, not causal attribution:
    # siblings can allocate or free concurrently. Process RSS is the one signal
    # in this report that belongs unambiguously to Tofu.
    process_rss_after_heap = _self_rss_bytes()
    usage_after_heap = mem_usage_bytes()
    memory_stat_after_heap = _read_memory_stat()
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
    process_rss_after = _self_rss_bytes()
    memory_stat_after = _read_memory_stat()
    duration_seconds = max(0.0, time.monotonic() - started_at)
    # MEASURED reclaim — the only honest number available. Negative means usage
    # grew during the relief (siblings allocating faster than we free), which is
    # reported as 0 reclaimed rather than hidden.
    reclaimed_bytes = _observed_reclaim_bytes(
        before.get('usage') if before else None,
        after.get('usage') if after else None)
    heap_window_reclaimed_bytes = _observed_reclaim_bytes(
        before.get('usage') if before else None, usage_after_heap)
    log_window_reclaimed_bytes = _observed_reclaim_bytes(
        usage_after_heap, after.get('usage') if after else None)
    process_rss_reclaimed_bytes = _observed_reclaim_bytes(
        process_rss_before, process_rss_after)
    cgroup_cache_reclaimed_bytes = _observed_reclaim_bytes(
        memory_stat_before.get('cache'), memory_stat_after.get('cache'))

    material_bytes = _material_reclaim_bytes((before or {}).get('limit'))
    no_progress = (reclaimed_bytes is not None
                   and reclaimed_bytes < material_bytes)
    repeated_no_progress = no_progress and _ineffective_reliefs > 0
    deferral_seconds = None
    escalate = False
    if no_progress:
        _ineffective_reliefs += 1
        deferral_seconds = _no_progress_deferral_seconds(_ineffective_reliefs)
        _relief_suppressed_until = time.monotonic() + deferral_seconds
        if (_ineffective_reliefs >= _INEFFECTIVE_LIMIT
                and not _ineffective_escalated):
            _ineffective_escalated = True
            escalate = True
    elif reclaimed_bytes is not None:
        _reset_aggregate_relief_backoff()
    _relief_rss_baseline = process_rss_after
    progress_note = ''
    if deferral_seconds is not None:
        progress_note = (
            '; below the %.1f MiB material floor (%d in a row), next '
            'aggregate relief deferred %.0fs'
            % (material_bytes / (1 << 20), _ineffective_reliefs,
               deferral_seconds))
    logger.log(logging.INFO if repeated_no_progress else logging.WARNING,
               '[cgroup] relief (%s): dropped %d cache entries, malloc_trim=%s, '
               'advised %d files (%d unchanged/skipped), '
               'RECLAIMED %s, usage %.1f%% -> %.1f%%; self-RSS %s, '
               'cgroup-cache %s, observed heap/log windows %s/%s, %.1fms%s',
               reason, dropped, trimmed,
               logs_dropped.get('files', 0), logs_dropped.get('skipped', 0),
               _measured_mb(reclaimed_bytes),
               before_pct if before_pct is not None else -1.0,
               after_pct if after_pct is not None else -1.0,
               _measured_mb(process_rss_reclaimed_bytes),
               _measured_mb(cgroup_cache_reclaimed_bytes),
               _measured_mb(heap_window_reclaimed_bytes),
               _measured_mb(log_window_reclaimed_bytes),
               duration_seconds * 1000.0, progress_note)

    try:
        from lib.observability import record_cgroup_relief
        record_cgroup_relief(
            duration_s=duration_seconds,
            cgroup_reclaimed_bytes=reclaimed_bytes,
            process_rss_reclaimed_bytes=process_rss_reclaimed_bytes,
            cgroup_cache_reclaimed_bytes=cgroup_cache_reclaimed_bytes,
            heap_window_reclaimed_bytes=heap_window_reclaimed_bytes,
            log_window_reclaimed_bytes=log_window_reclaimed_bytes,
            cache_entries_dropped=dropped,
            log_files_advised=logs_dropped.get('files', 0),
            log_bytes_advised=logs_dropped.get('bytes', 0),
        )
    except Exception as exc:
        logger.debug('[cgroup] relief metrics failed: %s', exc)

    # Escalate a structurally-ineffective relief exactly once per episode; a
    # material reclaim or a recovery below the hysteresis band re-arms it.
    if escalate:
        after_effective_pct = _effective_pct(after) if after else None
        logger.critical(
            '[cgroup] RELIEF IS INEFFECTIVE: %d consecutive reliefs '
            'did not reach the %.1f MiB material-reclaim floor while '
            'effective pressure sits at %.1f%% (raw %.1f%%). This process '
            'cannot relieve this pressure — what we can drop (our heap '
            'caches + our own log page cache) is noise against the total. '
            'On a SHARED cgroup the usage is dominated by sibling '
            'processes and FUSE slab, which we structurally cannot reach. '
            'Treat this as an unmitigated environment squeeze: an OOM '
            'SIGKILL is possible at any time and no in-process action '
            'will prevent it. Aggregate relief is deferred for %.0fs and '
            'keeps doubling to 1h unless Tofu RSS grows; pressure '
            'journaling remains active.',
            _ineffective_reliefs,
            material_bytes / (1 << 20),
            after_effective_pct if after_effective_pct is not None else -1.0,
            after_pct if after_pct is not None else -1.0,
            deferral_seconds)
        audit_log(
            'cgroup_relief_ineffective',
            consecutive=_ineffective_reliefs,
            usage_pct=(round(after_pct, 1)
                       if after_pct is not None else None),
            effective_pct=(round(after_effective_pct, 1)
                           if after_effective_pct is not None else None),
            cooldown_seconds=deferral_seconds)

    return {'reason': reason, 'dropped': dropped, 'trimmed': trimmed,
            'log_pages_bytes': logs_dropped.get('bytes', 0),
            'advised_files': logs_dropped.get('files', 0),
            'skipped_unchanged': logs_dropped.get('skipped', 0),
            'reclaimed_bytes': reclaimed_bytes,
            'heap_window_reclaimed_bytes': heap_window_reclaimed_bytes,
            'log_window_reclaimed_bytes': log_window_reclaimed_bytes,
            'process_rss_before': process_rss_before,
            'process_rss_after_heap': process_rss_after_heap,
            'process_rss_after': process_rss_after,
            'process_rss_reclaimed_bytes': process_rss_reclaimed_bytes,
            'cgroup_cache_before': memory_stat_before.get('cache'),
            'cgroup_cache_after_heap': memory_stat_after_heap.get('cache'),
            'cgroup_cache_after': memory_stat_after.get('cache'),
            'cgroup_cache_reclaimed_bytes': cgroup_cache_reclaimed_bytes,
            'duration_seconds': duration_seconds,
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
    effective_pct = _effective_pct(snap)
    if effective_pct >= warn_pct and no_swap:
        logger.critical(
            '[cgroup] SHARED CGROUP NEAR-FULL: %.1f%% effective pressure; '
            'raw %.1f%% used (%.1f/%.1f GiB), swap=0. '
            'This process can be OOM-SIGKILLed at any time by the kernel when the '
            'shared cgroup hits its ceiling — a bare "Killed" with no traceback is '
            'an ENVIRONMENT squeeze (siblings + FUSE cache), NOT a tofu bug. '
            'Mitigation needs a smaller dedicated cgroup / swap / fewer siblings.',
            effective_pct, snap['pct'], _gib(snap['usage']), _gib(snap['limit']))
        audit_log('cgroup_near_full',
                  effective_usage_pct=round(effective_pct, 1),
                  usage_pct=round(snap['pct'], 1),
                  usage_gib=round(_gib(snap['usage']), 1),
                  limit_gib=round(_gib(snap['limit']), 1),
                  swap_bytes=snap['swap'])
        return snap
    logger.info('[cgroup] self-check OK: %.1f%% effective pressure; '
                'raw %.1f%% used (%.1f/%.1f GiB), swap=%s',
                effective_pct, snap['pct'], _gib(snap['usage']), _gib(snap['limit']),
                'yes' if (snap['swap'] or 0) > 0 else 'no')
    return None


# ── ③ rolling pressure journal + OOM-kill witness ──
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
    """Parse cache/rss/inactive_file from memory.stat + kmem counter.

    Reads the v1 file first, then the v2 one (first readable wins); returns
    an empty dict when neither exists.  v2 names (``file``/``anon``) are
    normalised onto the v1 keys so callers stay version-agnostic.
    ``inactive_file``/``total_inactive_file`` are kept under their own keys:
    they are the kernel's immediately-reclaimable page cache — bytes an
    allocation can take back without writeback or swap, so they must NOT
    count as real pressure (2026-09-18: a cache-dominated shared cgroup read
    as 97%% "full" and self-DoSed admission while ~120 GiB was reclaimable).
    """
    out = {}
    for path in ('/sys/fs/cgroup/memory/memory.stat',   # v1
                 '/sys/fs/cgroup/memory.stat'):           # v2
        try:
            with open(path, 'r') as f:
                for line in f:
                    k, _, v = line.partition(' ')
                    if k in ('cache', 'rss', 'file', 'anon',
                             'inactive_file', 'total_inactive_file'):
                        try:
                            out[k] = int(v)
                        except ValueError as _e:
                            logger.debug('read memory stat: unparseable (%s)', _e)
        except OSError as _e:
            logger.debug('read memory stat: unreadable (%s)', _e)
            continue
        break
    # Normalise v2 names onto the v1 keys existing callers read.
    if 'cache' not in out and 'file' in out:
        out['cache'] = out['file']
    if 'rss' not in out and 'anon' in out:
        out['rss'] = out['anon']
    kmem = _read_first_int(('/sys/fs/cgroup/memory/memory.kmem.usage_in_bytes',))
    if kmem is not None:
        out['kmem'] = kmem
    return out


def reclaimable_file_bytes(stat: dict) -> Optional[int]:
    """Immediately-reclaimable page cache bytes from a memory.stat dict.

    Prefers the v1 hierarchical total (usage_in_bytes is hierarchical too);
    v2 memory.stat is already hierarchical and only has ``inactive_file``.
    Returns None when the stat carried no usable signal — callers must then
    fall back to the raw, conservative measurement.
    """
    if not stat:
        return None
    value = stat.get('total_inactive_file', stat.get('inactive_file'))
    if value is None:
        return None
    return max(0, int(value))


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
    # Raw on purpose, unlike the relief trigger: a burst can turn a cache-heavy
    # cgroup into an OOM within one tick, and this is the post-mortem evidence.
    if snap['pct'] >= relief_pct:
        rec['top'] = [{'comm': r['comm'], 'rss_gib': round(_gib(r['rss']), 2)}
                      for r in _top_rss_processes()]
    try:
        parent = os.path.dirname(_JOURNAL_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = _json.dumps(rec, separators=(',', ':')) + '\n'
        from lib.json_store import locked_path, write_bytes_atomic
        from lib.logging_pkg.log_policy import LOG_FILE_MODE
        from lib.logging_pkg.log_retention import append_bytes_locked
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


def run_monitor_once() -> Optional[dict]:
    """One monitor tick: relieve memory if effective pressure crosses the trigger.

    The trigger is reclaim-aware (:func:`pressure` ``effective_pct``). A pass
    that made no progress defers the next one; Tofu RSS growth past the
    material floor since the latest pass reopens it early, and the episode
    ends once pressure falls ``_RELIEF_REARM_HYSTERESIS_PCT`` below the
    trigger. Relief is best-effort and never refuses or delays work.

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
    pressure_pct = _effective_pct(snap)
    # A continuous high-pressure interval defines one adaptive-backoff episode.
    # Once pressure clearly recovers, stale suppression must not hide a later
    # incident; that later incident starts again at the shortest deferral.
    rearm_pct = relief_pct - _RELIEF_REARM_HYSTERESIS_PCT
    if pressure_pct < rearm_pct and _reset_aggregate_relief_backoff():
        logger.info(
            '[cgroup] aggregate relief re-armed after effective pressure '
            'recovered to %.1f%% (below the %.1f%% trigger minus %.1f '
            'points)', pressure_pct, relief_pct, _RELIEF_REARM_HYSTERESIS_PCT)
    if pressure_pct < relief_pct:
        return None
    if 'effective_pct' in snap:
        reason = 'monitor effective %.1f%% (raw %.1f%%) >= %.0f%%' % (
            pressure_pct, snap['pct'], relief_pct)
    else:
        reason = 'monitor %.1f%% >= %.0f%%' % (pressure_pct, relief_pct)
    if time.monotonic() < _relief_suppressed_until:
        rss_growth = _tofu_rss_growth_since_relief()
        if (rss_growth is None
                or rss_growth < _material_reclaim_bytes(snap['limit'])):
            return None
        reason += '; Tofu RSS +%.1fMiB since the last pass' % (
            rss_growth / (1 << 20))
    return relieve_memory(reason)


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


__all__ = [
    'mem_limit_bytes', 'mem_usage_bytes', 'swap_total_bytes', 'pressure',
    'reclaimable_file_bytes',
    'malloc_trim', 'relieve_memory',
    'fadvise_dontneed', 'drop_files_cache', 'drop_logs_cache',
    'write_pressure_journal', 'check_oom_kill_count',
    'startup_self_check',
    'run_monitor_once', 'start_monitor', 'stop_monitor',
]
