"""Application-owned readiness supervision for the local Storage Sidecar."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

from lib.storage.client import StorageClient
from lib.storage.connection_file import (
    read_connection_file,
    resolve_connection_file,
)
from lib.storage.protocol import PROTOCOL_VERSION
from lib.storage.startup_budget import (
    storage_startup_timeout,
)
from lib.storage.startup_control import (
    MAX_STARTUP_CONTROL_LINE_CHARS,
    STARTUP_ERROR_TYPE,
    STARTUP_PROGRESS_TYPE,
    STARTUP_READY_TYPE,
    StartupProgress,
    parse_startup_progress,
)
from lib.log import get_logger
from runtime_guards import install_process_resource_defaults


logger = get_logger('tofu.storage.supervisor')
# The managed engine/sidecar child's stderr is drained and mirrored here so
# engine error/integrity evidence lands in the standard app pipeline
# (app.log + error.log + incident index) instead of only the inherited
# console stream. Routing depends on this name staying inside the 'tofu.'
# business prefix admitted by lib/server/server_logging._BIZ_PREFIXES.
engine_logger = get_logger('tofu.storage.engine')

MAX_PINNED_TOFUDB_BINARY_BYTES = 512 * 1024 * 1024
# Same sha256 over the same bytes, ~16x fewer FUSE round trips than the old
# 64 KiB stride when the pinned binary sits on a network mount.
TOFUDB_HASH_READ_BYTES = 1024 * 1024
TOFUDB_PIN_SIDECAR_SUFFIX = '.sha256'
MAX_TOFUDB_PIN_SIDECAR_BYTES = 4096
MAX_TOFUDB_PROVENANCE_BYTES = 64 * 1024

_DEFAULT_RPC_DEADLINE_S = 5.0
_MAX_RPC_DEADLINE_S = 120.0

# ── Managed-child stderr draining (engine log mirroring) ────────────────
# One physical stderr line is mirrored at most once; a pathological child
# cannot allocate unbounded memory through an unterminated line.
_MAX_ENGINE_STDERR_LINE_CHARS = 16 * 1024
_ENGINE_LINE_TRUNCATED_MARKER = '…[truncated]'
# Engine JSON line contract (packages/tofu-db/src/engine_log.rs):
# {"source":"tofu-db","level":...,"target":...,"message":...}.
_ENGINE_JSON_SOURCE = 'tofu-db'
# Integrity-fence evidence is mirrored at any engine level; ordinary
# diagnostics only cross into the app pipeline at error/critical.
_ENGINE_ALWAYS_MIRROR_TARGET = 'integrity_fence'
# Human-oriented opt-in dumps (TOFU_DB_FSYNC_STATS shutdown report and
# friends) are debug-plane diagnostics, never warnings.
_ENGINE_DEBUG_LINE_PREFIXES = (
    'fsync_stats:', 'stage_stats:', 'listener_metrics:', 'wal_stats:')
_ENGINE_LEVEL_TO_LOG = {
    'critical': 50,  # logging.CRITICAL
    'error': 40,     # logging.ERROR
    'warn': 30,      # logging.WARNING
    'warning': 30,
    'info': 20,      # logging.INFO
}


def _forward_raw_stderr_line(raw_line: str) -> None:
    """Best-effort forward of one child stderr line to the inherited stderr.

    server-console.log keeps the exact content it captured before the
    drainer existed. The forward targets OS file descriptor 2 rather than
    the replaceable Python-level ``sys.stderr`` alias: the console capture
    is an fd-level contract, and writing through ``sys.stderr`` would leak
    forwarded child lines into any embedding that swaps the alias (test
    harnesses capturing CLI output included). Forwarding must never block
    or fail the drainer: a wedged console writer drops the forward (the
    app pipeline copy is the durable one), it never stalls the child's
    pipe.
    """
    data = raw_line if raw_line.endswith('\n') else raw_line + '\n'
    payload = data.encode('utf-8', errors='replace')
    try:
        view = memoryview(payload)
        while view:
            written = os.write(2, view)
            view = view[written:]
    except (OSError, ValueError):
        pass


def _mirror_engine_stderr_line(line: str, *, pid: int) -> None:
    """Route one bounded child stderr line into the app log pipeline.

    Engine JSON lines map onto tofu.storage.engine at matching severity:
    error/critical always cross; the integrity_fence target crosses at any
    level because fence evidence is exactly what an operator needs when the
    authority refuses writes. Human/stat lines stay at DEBUG so an opt-in
    stats dump never reads as an incident.
    """
    text = line.rstrip('\r\n')
    if not text:
        return
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get('source') == _ENGINE_JSON_SOURCE:
        level = str(parsed.get('level') or '').lower()
        target = str(parsed.get('target') or '')[:128]
        message = str(parsed.get('message') or '')
        log_level = _ENGINE_LEVEL_TO_LOG.get(level, 20)
        if log_level >= 40 or target == _ENGINE_ALWAYS_MIRROR_TARGET:
            engine_logger.log(
                log_level, 'tofu-db engine pid=%d [%s] %s',
                pid, target, message)
        else:
            engine_logger.debug(
                'tofu-db engine pid=%d [%s] %s', pid, target, message)
        return
    if text.startswith(_ENGINE_DEBUG_LINE_PREFIXES):
        engine_logger.debug('storage child pid=%d stderr: %s', pid, text)
        return
    # Non-JSON lines (the Python sidecar's own stderr, engine panics,
    # tracebacks) carry no reliable severity; forward at INFO so they are
    # visible in app.log without crying wolf in error.log.
    engine_logger.info('storage child pid=%d stderr: %s', pid, text)


def _drain_child_stderr(stream, *, pid: int) -> None:
    """Continuously drain one managed child's stderr pipe until EOF.

    The pipe must always drain: a full pipe would wedge the child on its
    next stderr write. Lines are capped; an over-long line is consumed to
    its newline in bounded chunks and mirrored with a truncation marker, so
    a protocol-violating child cannot grow resident memory. The drainer's
    lifecycle binds to the child: EOF (child exit, or the abort/stop path
    closing the parent's ends) is the only exit.
    """
    while True:
        try:
            line = stream.readline(_MAX_ENGINE_STDERR_LINE_CHARS + 2)
        except (OSError, ValueError):
            return
        if not line:
            return
        if len(line) > _MAX_ENGINE_STDERR_LINE_CHARS:
            truncated = line[:_MAX_ENGINE_STDERR_LINE_CHARS]
            if not line.endswith('\n'):
                # Drain the remainder of the over-long line in bounded
                # chunks so the child is never blocked on a full pipe.
                while True:
                    try:
                        rest = stream.readline(
                            _MAX_ENGINE_STDERR_LINE_CHARS + 2)
                    except (OSError, ValueError):
                        rest = ''
                    if not rest or rest.endswith('\n'):
                        break
            line = truncated + _ENGINE_LINE_TRUNCATED_MARKER + '\n'
        _forward_raw_stderr_line(line)
        _mirror_engine_stderr_line(line, pid=pid)


def rpc_deadline_seconds() -> float:
    """Default per-attempt storage RPC deadline in seconds (env-tunable).

    The 5 s product default is generous on a healthy local volume and keeps
    a wedged authority failing fast. On a network-mounted data root (FUSE),
    fsync storms legitimately exceed it — every request then 503s behind the
    single authority mutex (2026-09-18 incident). ``TOFU_STORAGE_RPC_DEADLINE_S``
    raises the floor for such deployments; it is clamped to [1, 120] because
    a larger value only delays the failure signal, it cannot revive a dead
    authority. Explicit per-call deadlines (health, maintenance) are
    unaffected.
    """
    raw = os.environ.get('TOFU_STORAGE_RPC_DEADLINE_S', '').strip()
    if not raw:
        return _DEFAULT_RPC_DEADLINE_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            '[Storage] invalid TOFU_STORAGE_RPC_DEADLINE_S=%r; using %.1f',
            raw, _DEFAULT_RPC_DEADLINE_S)
        return _DEFAULT_RPC_DEADLINE_S
    if not 1.0 <= value <= _MAX_RPC_DEADLINE_S:
        logger.warning(
            '[Storage] TOFU_STORAGE_RPC_DEADLINE_S=%r outside [1, %.0f]; '
            'clamping', raw, _MAX_RPC_DEADLINE_S)
    return max(1.0, min(_MAX_RPC_DEADLINE_S, value))


_DEFAULT_LIVENESS_INTERVAL_S = 1.0
_DEFAULT_LIVENESS_PROBE_DEADLINE_S = 1.5
_DEFAULT_LIVENESS_FAILURE_THRESHOLD = 3
# Host-overload gate defaults: cgroup memory at/above 92% of its limit (the
# same watermark the cgroup guard relieves at) or a 1-minute load average at
# or above 2x the effective CPU count marks the host as overloaded.
_DEFAULT_LIVENESS_OVERLOAD_MEM_RATIO = 0.92
_DEFAULT_LIVENESS_OVERLOAD_LOAD_FACTOR = 2.0
# Bound on how long a failing probe may be written off as host starvation.
# Without it the overload gate is a latch on any host whose *shared* cgroup
# sits near its limit (siblings + page cache), and a genuinely wedged
# authority is withheld forever (2026-09-22: 1715 suppressed probes, hours of
# database_unavailable behind cgroup memory pinned at 98-99%). After this many
# consecutive suppressed probes the monitor forces exactly one bounded
# recycle; the recycle is then rate-limited by an exponentially escalating
# cooldown so a truly starved child cannot be cold-started into a storm.
_DEFAULT_LIVENESS_OVERLOAD_MAX_SUPPRESS = 60
_DEFAULT_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_S = 300.0
_MAX_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_S = 1800.0
# Overload stickiness: once the host reads overloaded, keep treating it as
# overloaded for this decay window even if a single instantaneous probe dips
# below the watermark. On cgroup v1 ``usage_in_bytes`` counts page cache, so
# the ratio oscillates around the 92% watermark as the kernel reclaims cache
# (measured 2026-09-24: median 92.1%, below-watermark in ~50% of samples). A
# point-in-time gate therefore flickers to "healthy" for exactly the 3-probe
# streak it takes to kill a starved-but-alive child — 29 self-inflicted
# SIGTERMs in one day. Stickiness makes the gate reflect the recent regime,
# not one lucky reclaim. It does NOT extend the suppression window: the
# ``max_suppress`` forced-recycle bound still applies, so a genuinely wedged
# authority is recovered on the same bounded delay as before.
_DEFAULT_LIVENESS_OVERLOAD_STICKY_S = 30.0
# Bounded recycle stages for a child that answers neither RPCs nor exit.
# Owner-channel EOF is the authority-ownership release and gives a merely
# busy child one graceful window; SIGTERM/SIGKILL then bound the teardown so
# the monitor's exit path always runs. These stay hardcoded like the stop()
# and _abort_failed_start budgets — they are recovery deadlines, not policy.
_LIVENESS_EOF_GRACE_SECONDS = 2.0
_LIVENESS_TERMINATE_GRACE_SECONDS = 3.0
_LIVENESS_KILL_GRACE_SECONDS = 3.0
# Embedded-monitor wedge defense (see _probe_embedded_health): extra join
# budget beyond the probe deadline before an unreturned FFI probe is
# declared wedged, and the bounded budget for closing that unknown-state
# engine handle. Recovery deadlines, not policy — hardcoded like the
# liveness recycle stages above.
_EMBEDDED_PROBE_JOIN_GRACE_SECONDS = 2.0
_EMBEDDED_WEDGED_CLOSE_BUDGET_SECONDS = 5.0


def _liveness_probe_config(env) -> tuple[float, float, int]:
    """Managed-child liveness probe cadence (env-tunable, hard-bounded).

    Defaults mirror the external co-container monitor (1 s loop, 1.5 s probe
    deadline) and require three consecutive failures before recycling a
    child, so one slow probe under a momentarily saturated session pool can
    never kill a busy-but-healthy daemon. Bounds keep overrides inside the
    fail-fast intent: a longer cadence only delays recovery, it cannot
    revive a dead child.
    """
    def _bounded(name: str, default: float, lo: float, hi: float) -> float:
        raw = env.get(name, '').strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                '[Storage] invalid %s=%r; using %s', name, raw, default)
            return default
        if not lo <= value <= hi:
            logger.warning(
                '[Storage] %s=%r outside [%s, %s]; clamping',
                name, raw, lo, hi)
        return max(lo, min(hi, value))

    interval = _bounded(
        'TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS',
        _DEFAULT_LIVENESS_INTERVAL_S, 0.2, 10.0)
    deadline = _bounded(
        'TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS',
        _DEFAULT_LIVENESS_PROBE_DEADLINE_S, 0.2, 10.0)
    threshold = int(_bounded(
        'TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD',
        _DEFAULT_LIVENESS_FAILURE_THRESHOLD, 1, 10))
    return interval, deadline, threshold


def _liveness_overload_config(env) -> tuple[float, float]:
    """Host-overload gate for liveness recycles (env-tunable, hard-bounded).

    A starved-but-healthy child fails probes exactly like a wedged one, and
    recycling it converts host pressure into a crash loop (2026-09-22: ~490
    SIGTERM recycles in three hours behind cgroup memory pinned at 98-99%
    and load far above the CPU count; each replacement cold-started on a
    FUSE volume and failed its next probes within seconds). While the host
    is observably overloaded the monitor withholds the recycle and keeps
    probing; the consecutive-failure termination still applies the moment
    pressure clears, so a genuinely wedged child is still recycled and the
    2026-09-21 wedged-daemon coverage is preserved. A shared cgroup pinned
    near its limit by siblings never "clears", so withholding is additionally
    bounded by ``_liveness_overload_suppression_config`` — a wedged child is
    still recovered, just at a bounded rate rather than forever. Bounds keep
    overrides inside that intent: a mem ratio below 50% or a load factor
    below 1x would suppress recycles on a healthy host.
    """
    def _bounded(name: str, default: float, lo: float, hi: float) -> float:
        raw = env.get(name, '').strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                '[Storage] invalid %s=%r; using %s', name, raw, default)
            return default
        if not lo <= value <= hi:
            logger.warning(
                '[Storage] %s=%r outside [%s, %s]; clamping',
                name, raw, lo, hi)
        return max(lo, min(hi, value))

    mem_ratio = _bounded(
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MEM_RATIO',
        _DEFAULT_LIVENESS_OVERLOAD_MEM_RATIO, 0.5, 1.0)
    load_factor = _bounded(
        'TOFU_STORAGE_LIVENESS_OVERLOAD_LOAD_FACTOR',
        _DEFAULT_LIVENESS_OVERLOAD_LOAD_FACTOR, 1.0, 32.0)
    return mem_ratio, load_factor


def _liveness_overload_suppression_config(env) -> tuple[int, float]:
    """Bound the host-overload suppression window (env-tunable, hard-bounded).

    ``max_suppress`` is the number of consecutive failed probes that may be
    attributed to host starvation before the monitor forces exactly one
    bounded recycle; ``recycle_cooldown`` is the floor between two forced
    recycles, doubled on every escalation (with no healthy probe between) up
    to the hard ceiling. Together they replace what was an unbounded latch:
    a wedged authority is recovered within a bounded delay, while a starved
    child cannot be cold-started into the 2026-09-22 recycle storm. Bounds
    keep overrides inside that intent; a non-positive cap or cooldown would
    restore either the latch or the storm.
    """
    def _bounded(name: str, default: float, lo: float, hi: float) -> float:
        raw = env.get(name, '').strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                '[Storage] invalid %s=%r; using %s', name, raw, default)
            return default
        if not lo <= value <= hi:
            logger.warning(
                '[Storage] %s=%r outside [%s, %s]; clamping',
                name, raw, lo, hi)
        return max(lo, min(hi, value))

    max_suppress = int(_bounded(
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MAX_SUPPRESS',
        float(_DEFAULT_LIVENESS_OVERLOAD_MAX_SUPPRESS), 1.0, 100000.0))
    recycle_cooldown = _bounded(
        'TOFU_STORAGE_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_SECONDS',
        _DEFAULT_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_S, 1.0, 86400.0)
    sticky = _bounded(
        'TOFU_STORAGE_LIVENESS_OVERLOAD_STICKY_SECONDS',
        _DEFAULT_LIVENESS_OVERLOAD_STICKY_S, 0.0, 3600.0)
    return max_suppress, recycle_cooldown, sticky


def _read_cgroup_int(paths: tuple[str, ...]) -> int | None:
    """First positive int across kernel-ABI paths, or ``None`` when unreadable."""
    for path in paths:
        try:
            raw = Path(path).read_text(encoding='ascii').strip()
        except (OSError, ValueError):
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _host_overload_detail(
        mem_ratio_limit: float, load_factor_limit: float) -> str | None:
    """Return why the host is overloaded right now, or ``None`` when it is not.

    Two independent signals, either sufficient: cgroup memory usage at or
    above ``mem_ratio_limit`` of the limit, or the 1-minute load average at
    or above ``load_factor_limit`` times the effective CPU count. Every
    observation failure falls back to ``None`` (host presumed healthy) so
    non-Linux hosts and restricted sandboxes keep the original fail-fast
    recycle behavior. Kernel-ABI paths mirror lib/cgroup_guard.py and
    lib/server/server_mlock.py; kept independent so this monitor carries no import
    dependency on later-loaded modules.
    """
    try:
        usage = _read_cgroup_int((
            '/sys/fs/cgroup/memory.current',                    # v2
            '/sys/fs/cgroup/memory/memory.usage_in_bytes'))     # v1
        limit = _read_cgroup_int((
            '/sys/fs/cgroup/memory.max',                        # v2
            '/sys/fs/cgroup/memory/memory.limit_in_bytes'))     # v1
        if usage is not None and limit is not None and limit < (1 << 62):
            ratio = usage / limit
            if ratio >= mem_ratio_limit:
                return (f'cgroup memory {ratio * 100:.1f}% >= '
                        f'{mem_ratio_limit * 100:.0f}% of limit')
        load1 = os.getloadavg()[0]
        try:
            cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cpus = os.cpu_count() or 1
        cpus = max(1, cpus)
        if load1 / cpus >= load_factor_limit:
            return (f'loadavg1 {load1:.1f} >= '
                    f'{load_factor_limit:.1f}x{cpus} effective CPUs')
    except Exception as _e:
        logger.debug('host overload probe failed: %s', _e)
        return None
    return None


def _exit_signal_note(return_code: int | None) -> str:
    """Decode a subprocess return code into a signal annotation, if any."""
    if return_code is None or return_code >= 0:
        return ''
    return f' signal={-return_code}'


def _tofudb_env_pin() -> str | None:
    """The explicit operator/test content pin, or ``None`` when unconfigured."""
    expected = os.environ.get('TOFUDB_EXPECTED_SHA256', '').strip().lower()
    if not expected:
        return None
    if len(expected) != 64 or any(ch not in '0123456789abcdef' for ch in expected):
        raise RuntimeError('TOFUDB_EXPECTED_SHA256 must contain exactly 64 hexadecimal characters')
    return expected


def _tofudb_sidecar_pin(path: str) -> str | None:
    """The artifact-traveling pin in ``<binary>.sha256``, or ``None`` when absent.

    ``scripts/deploy_tofudb_engine.py`` writes the sidecar atomically with the
    artifact it pins, so an upgrade cannot desynchronize the two the way a
    hand-maintained pin stored elsewhere can. A present but unreadable or
    malformed sidecar is drift evidence and therefore fails closed.
    """
    sidecar = path + TOFUDB_PIN_SIDECAR_SUFFIX
    try:
        with open(sidecar, 'rb') as handle:
            raw = handle.read(MAX_TOFUDB_PIN_SIDECAR_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f'unable to read tofu-db pin sidecar {sidecar}') from exc
    if len(raw) > MAX_TOFUDB_PIN_SIDECAR_BYTES:
        raise RuntimeError(f'tofu-db pin sidecar {sidecar} exceeds the 4 KiB budget')
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f'tofu-db pin sidecar {sidecar} must be ASCII sha256sum-format text') from exc
    fields = text.split()
    expected = fields[0].lower() if fields else ''
    if len(expected) != 64 or any(ch not in '0123456789abcdef' for ch in expected):
        raise RuntimeError(
            f'tofu-db pin sidecar {sidecar} must start with exactly 64 hexadecimal characters')
    return expected


def _tofudb_provenance_note(path: str) -> str:
    """Best-effort deployment provenance recorded beside the binary, for remedies."""
    record = Path(path).parent / 'build.json'
    try:
        raw = record.read_bytes()
        if len(raw) > MAX_TOFUDB_PROVENANCE_BYTES:
            return ''
        fields = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError):
        return ''
    if not isinstance(fields, dict):
        return ''
    parts = [f'{key}={fields[key]}'
             for key in ('source_head', 'deployment_utc', 'certification')
             if isinstance(fields.get(key), str)]
    if not parts:
        return ''
    return f'; provenance ({record}): ' + ', '.join(parts)


TOFUDB_ENGINE_SOURCE_SUBPATH = 'packages/tofu-db'
# Bound the staleness probe so a wedged network-mount git can never stall
# storage startup; advisory only, so a probe failure stays silent.
TOFUDB_STALENESS_PROBE_TIMEOUT_S = 5.0
TOFUDB_STALENESS_COMMIT_CAP = 50


def _tofudb_checkout_root() -> Path:
    """This checkout's root; a function so tests can substitute a fixture."""
    return Path(__file__).resolve().parents[2]


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _git_probe(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    if shutil.which('git') is None or not (root / '.git').exists():
        return None
    try:
        return subprocess.run(
            ['git', '-C', str(root), *args],
            capture_output=True, text=True,
            timeout=TOFUDB_STALENESS_PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tofudb_staleness_advisory(path: str) -> str | None:
    """Drift note when the deployed engine predates this checkout, else None.

    The deployment provenance (``build.json`` written beside the binary by
    ``scripts/deploy_tofudb_engine.py``) records the source HEAD the engine
    was built from. When this checkout has engine-affecting commits the
    binary cannot contain — or a provenance that is not an ancestor at all —
    a restart would silently keep serving yesterday's engine semantics, the
    exact drift behind the 2026-09-21 stale-engine incident. Advisory only:
    a release install has no ``.git`` and missing provenance is not evidence.
    """
    record = Path(path).parent / 'build.json'
    try:
        raw = record.read_bytes()
        if len(raw) > MAX_TOFUDB_PROVENANCE_BYTES:
            return None
        fields = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(fields, dict):
        return None
    source_head = fields.get('source_head')
    if not isinstance(source_head, str) or not source_head.strip():
        return None
    source_head = source_head.strip()
    dirty = source_head.endswith('-dirty')
    revision = source_head[:-len('-dirty')] if dirty else source_head

    root = _tofudb_checkout_root()
    ancestor = _git_probe(root, 'merge-base', '--is-ancestor', revision, 'HEAD')
    if ancestor is None:
        return None
    origin = f'{revision} (built from a dirty tree)' if dirty else revision
    if ancestor.returncode != 0:
        return (f'tofu-db engine {path} was built from {origin}, which is not an '
                f'ancestor of this checkout\'s HEAD')
    log = _git_probe(
        root, 'log', '--format=%h', f'--max-count={TOFUDB_STALENESS_COMMIT_CAP}',
        f'{revision}..HEAD', '--', TOFUDB_ENGINE_SOURCE_SUBPATH)
    if log is None or log.returncode != 0:
        return None
    landed = len(log.stdout.split())
    if landed == 0:
        return None
    count = f'{landed}+' if landed >= TOFUDB_STALENESS_COMMIT_CAP else str(landed)
    return (f'tofu-db engine {path} was built from {origin}, but {count} commit(s) '
            f'touching {TOFUDB_ENGINE_SOURCE_SUBPATH} landed since')


def _advise_tofudb_engine_staleness(path: str) -> None:
    """Warn (or, opted-in, refuse) when the resolved engine lags the checkout."""
    root = _tofudb_checkout_root()
    if (root / 'packages/tofu-db/build.rs').is_file():
        from serverctl_pkg.native_engine import verify_current
        verify_current(Path(path), root)
        return
    advisory = _tofudb_staleness_advisory(path)
    if advisory is None:
        return
    remedy = ('redeploy the engine with `python3 scripts/deploy_tofudb_engine.py` '
              'after engine changes (docs/STORAGE_TOFUDB.md)')
    if _truthy_env('TOFUDB_REQUIRE_FRESH_ENGINE'):
        raise RuntimeError(f'{advisory}; {remedy}')
    logger.warning('%s; %s', advisory, remedy)


def _tofudb_binary_name() -> str:
    """The platform's executable filename for the tofu-db engine."""
    return 'tofu-db.exe' if sys.platform.startswith('win') else 'tofu-db'


def _executable(path: Path) -> str | None:
    """``str(path)`` when it names an executable file, else ``None``."""
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.resolve())
    return None


def _bundled_tofudb_candidates() -> list[Path]:
    """Where a PRE-BUILT tofu-db ships inside a packaged install.

    Order encodes preference, and the search exists so a frozen desktop
    install works with ZERO extra dependencies: the engine is bundled next to
    the app rather than built or downloaded by the user.

      * a PyInstaller bundle — ``sys._MEIPASS`` is the ``_internal`` dir that
        ``tofu.spec`` copies the engine into;
      * beside the executable — a portable/unzipped layout where the engine
        sits next to the launcher.

    In a plain source checkout neither applies; the caller falls through to
    this checkout's release build or a PATH-installed binary.
    """
    name = _tofudb_binary_name()
    candidates: list[Path] = []
    bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root:
        candidates.append(Path(bundle_root) / name)
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).resolve().parent / name)
    return candidates


def _verify_tofudb_binary(path: str) -> str:
    """Check the selected engine against its declared content pin, if any.

    Pin authority, highest first:

      1. ``TOFUDB_EXPECTED_SHA256`` — an explicit operator/test override;
      2. ``<binary>.sha256`` — the sidecar deployed atomically with the
         artifact by ``scripts/deploy_tofudb_engine.py``, so the pin travels
         with the binary through upgrades;
      3. neither declared — development resolution with no drift check.

    This detects accidental artifact drift, not an adversarial filesystem
    replacement after this check. No cached digest can outlive the selected
    file; every actual launch resolves and checks its candidate again.
    """
    expected = _tofudb_env_pin()
    pin_source = 'TOFUDB_EXPECTED_SHA256'
    if expected is None:
        expected = _tofudb_sidecar_pin(path)
        pin_source = path + TOFUDB_PIN_SIDECAR_SUFFIX
    if expected is None:
        _advise_tofudb_engine_staleness(path)
        return path
    try:
        with open(path, 'rb') as binary:
            before = os.fstat(binary.fileno())
            if before.st_size > MAX_PINNED_TOFUDB_BINARY_BYTES:
                raise RuntimeError('Pinned tofu-db binary exceeds the 512 MiB verification budget')
            digest = hashlib.sha256()
            scanned_bytes = 0
            for chunk in iter(lambda: binary.read(TOFUDB_HASH_READ_BYTES), b''):
                scanned_bytes += len(chunk)
                if scanned_bytes > MAX_PINNED_TOFUDB_BINARY_BYTES:
                    raise RuntimeError('Pinned tofu-db binary exceeds the 512 MiB verification budget')
                digest.update(chunk)
            after = os.fstat(binary.fileno())
            selected = os.stat(path)
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(selected):
            raise RuntimeError('Pinned tofu-db binary changed during verification')
    except OSError as exc:
        raise RuntimeError('Unable to verify pinned tofu-db binary') from exc
    actual = digest.hexdigest()
    if actual != expected:
        remedy = (f'restore the verified artifact, or re-pin the deployed one with '
                  f'`python3 scripts/deploy_tofudb_engine.py --binary {path} --dest {path}`')
        raise RuntimeError(f'tofu-db SHA-256 mismatch for {path}: expected {expected} '
                           f'(pin: {pin_source}), got {actual}; {remedy}'
                           f'{_tofudb_provenance_note(path)}')
    _advise_tofudb_engine_staleness(path)
    return path


def _resolve_tofudb_binary() -> str:
    """Resolve the tofu-db engine binary.

    Resolution order, first hit wins:
      1. ``TOFUDB_BIN`` — an explicit operator/test override (authoritative:
         when set, nothing else is consulted and a bad path fails loudly);
      2. a prebuilt engine bundled inside a packaged install (frozen bundle);
      3. this checkout's own release build
         (``packages/tofu-db/target/release/tofu-db``);
      4. a ``tofu-db`` on ``PATH`` (a separately-installed engine).

    The bundled path is what lets a pre-built release package run tofu-db
    with no toolchain and no extra install step.
    """
    override = os.environ.get('TOFUDB_BIN', '').strip()
    if override:
        resolved = _executable(Path(override).expanduser())
        if resolved:
            return _verify_tofudb_binary(resolved)
        raise RuntimeError('TOFUDB_BIN must name an executable tofu-db binary')
    for bundled in _bundled_tofudb_candidates():
        resolved = _executable(bundled)
        if resolved:
            return _verify_tofudb_binary(resolved)
    source_build = (Path(__file__).resolve().parents[2]
                    / 'packages/tofu-db/target/release' / _tofudb_binary_name())
    resolved = _executable(source_build)
    if resolved:
        return _verify_tofudb_binary(resolved)
    installed = shutil.which(_tofudb_binary_name())
    if installed:
        return _verify_tofudb_binary(installed)
    raise RuntimeError('TOFUDB_BIN must name an executable tofu-db binary')


def _tofudb_scope_ids() -> tuple[int, int | None]:
    """Resolve personal composition identity; scopes remain explicit on RPCs."""
    from lib.identity import PERSONAL_USER_ID

    owner = int(os.environ.get('TOFUDB_OWNER_ID', str(PERSONAL_USER_ID)))
    tenant = int(os.environ.get('TOFUDB_TENANT_ID', '1'))
    if owner <= 0 or tenant < 0:
        raise ValueError('TOFUDB owner must be positive and tenant non-negative')
    return owner, tenant or None


def _resolve_tofudb_embedded_engine() -> tuple[str, str]:
    """Resolve the embedded engine pair: ``(daemon binary, shared library)``.

    The shared library IS the engine in embedded mode and ships from the same
    cargo build as the daemon executable, so the daemon's resolution order
    (``TOFUDB_BIN`` override, bundled, checkout build, PATH) and its content
    pin stay authoritative; the library is the selected binary's sibling. A
    missing sibling fails closed with the build remedy — never a silent
    fallback to another engine snapshot.
    """
    binary = _resolve_tofudb_binary()
    library = Path(binary).parent / 'libtofu_db.so'
    if library.is_file():
        if (_tofudb_checkout_root() / 'packages/tofu-db/build.rs').is_file():
            from serverctl_pkg.native_engine import verify_library
            verify_library(Path(binary))
        return binary, str(library.resolve())
    raise RuntimeError(
        'embedded transport requires libtofu_db.so next to the selected '
        'tofu-db binary; build with `cargo build --release` (cdylib target)')


class _StartupDeadline:
    """A renewable stall deadline inside one immutable hard boot budget."""

    def __init__(
        self,
        *,
        started_at: float,
        stall_timeout: float,
        hard_timeout: float,
    ) -> None:
        self._stall_timeout = max(0.001, float(stall_timeout))
        self._hard_deadline = started_at + max(
            self._stall_timeout, float(hard_timeout))
        self._stall_deadline = started_at + self._stall_timeout
        self._phase = ''
        self._completed_bytes = 0
        self._total_bytes = 0

    def remaining(self, now: float) -> float:
        return max(
            0.0,
            min(self._stall_deadline, self._hard_deadline) - now,
        )

    def observe(self, progress: StartupProgress, *, now: float) -> bool:
        """Record monotonic work; return whether the stall deadline renewed."""
        phase_changed = progress.phase != self._phase
        if not phase_changed:
            if progress.total_bytes != self._total_bytes:
                raise ValueError(
                    'storage startup progress total changed within one phase')
            if progress.completed_bytes < self._completed_bytes:
                raise ValueError('storage startup progress moved backwards')
        advanced = bool(
            phase_changed
            or progress.completed_bytes > self._completed_bytes
            or progress.heartbeat
        )
        self._phase = progress.phase
        self._completed_bytes = progress.completed_bytes
        self._total_bytes = progress.total_bytes
        if advanced:
            self._stall_deadline = now + self._stall_timeout
        return advanced

    def timeout_error(self, *, now: float) -> RuntimeError:
        detail = (
            f' phase={self._phase} '
            f'progress={self._completed_bytes}/{self._total_bytes}'
            if self._phase else '')
        if now >= self._hard_deadline:
            return RuntimeError(
                'storage sidecar startup exceeded its hard timeout' + detail)
        return RuntimeError(
            'storage sidecar startup stalled without progress' + detail)


class StorageSupervisor:
    """Authenticate and monitor one child or co-container Sidecar.

    Personal mode launches a child and receives its token/port through the
    environment and stdout control channel. Kubernetes mode attaches to the
    independently managed co-container through a private connection file; it
    never owns or terminates that container. Both modes revoke readiness and
    notify crash callbacks when the authenticated health channel disappears.
    """

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str] | None = None,
        backend: str | None = None,
        connection_file: str | os.PathLike[str] | None = None,
        startup_timeout: float | None = None,
        startup_stall_timeout: float | None = None,
        sqlite_fastpath_mode: str | None = None,
        on_crash: Callable[[int], None] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve() if project_root else None
        self._backend = backend
        # Scratch authorities may pin their volume without temporarily changing
        # the application's process-wide configuration during a background job.
        if sqlite_fastpath_mode not in (None, 'off', 'auto', 'required'):
            raise ValueError('sqlite_fastpath_mode must be off, auto, or required')
        self._sqlite_fastpath_mode = sqlite_fastpath_mode
        raw_connection_file = connection_file
        if raw_connection_file is None:
            raw_connection_file = os.environ.get(
                'TOFU_STORAGE_CONNECTION_FILE', '').strip() or None
        self._connection_file = (
            resolve_connection_file(raw_connection_file)
            # Explicit empty string selects a managed child even when the
            # application itself attaches through an ambient connection file.
            if raw_connection_file else None
        )
        self._startup_timeout = (
            storage_startup_timeout(os.environ)
            if startup_timeout is None
            else max(1.0, float(startup_timeout))
        )
        self._startup_stall_timeout = min(
            self._startup_timeout,
            max(
                0.001,
                float(startup_stall_timeout)
                if startup_stall_timeout is not None
                # A WAL replay on a slow volume emits no progress lines for
                # minutes; the silence budget follows the configured startup
                # budget (TOFU_STORAGE_STARTUP_TIMEOUT_S) instead of the bare
                # 30s ordinary default, which killed every opening child
                # mid-replay and crash-looped the boot. Unchanged at the
                # default: both budgets stay ORDINARY_STORAGE_STARTUP_TIMEOUT_S.
                else self._startup_timeout,
            ),
        )
        self._crash_callbacks: list[Callable[[int], None]] = []
        if on_crash is not None:
            self._crash_callbacks.append(on_crash)
        self._process: subprocess.Popen[str] | None = None
        self._process_started_at: float | None = None
        self._stderr_drainer: threading.Thread | None = None
        self._client: StorageClient | None = None
        self._token = ''
        self._intentional_stop = False
        self._starting = False
        self._ready_backend: str | None = None
        self._native_fastpath: dict | None = None
        self._endpoint_port: int | None = None
        self._last_exit_code: int | None = None
        self._monitor: threading.Thread | None = None
        self._external_attached = False
        self._external_monitor_stop = threading.Event()
        self._embedded_runtime: object | None = None
        self._embedded_lease: object | None = None
        self._embedded_monitor_stop = threading.Event()
        (self._liveness_interval_seconds,
         self._liveness_probe_deadline_seconds,
         self._liveness_failure_threshold) = _liveness_probe_config(os.environ)
        (self._liveness_overload_mem_ratio,
         self._liveness_overload_load_factor) = _liveness_overload_config(
             os.environ)
        (self._liveness_overload_max_suppress,
         self._liveness_overload_recycle_cooldown,
         self._liveness_overload_sticky_seconds
         ) = _liveness_overload_suppression_config(os.environ)
        # Escalating rate limit for overload-forced recycles, shared across
        # monitor generations (a replacement child gets a fresh monitor).
        self._overload_recycle_not_before = 0.0
        self._overload_recycle_backoff = 0.0
        # Overload stickiness: monotonic deadline until which the host is
        # still treated as overloaded after its last overloaded reading, and
        # the detail string captured then (for logging). See the constant.
        self._overload_sticky_until = 0.0
        self._overload_sticky_detail: str | None = None
        self._lock = threading.RLock()
        # Actual launch selection, never recomputed from mutable settings.
        self._search_projection_directory = ''

    @property
    def client(self) -> StorageClient:
        with self._lock:
            if self._client is None or not self.ready:
                raise RuntimeError('storage sidecar is not ready')
            return self._client

    @property
    def ready(self) -> bool:
        if self._connection_file is not None:
            return bool(self._external_attached and self._client is not None)
        if self._embedded_runtime is not None:
            return self._client is not None
        process = self._process
        return bool(process is not None and process.poll() is None and self._client)

    def status(self) -> dict[str, object]:
        """Return an in-memory snapshot; never performs an RPC or disk read."""
        with self._lock:
            process = self._process
            child_alive = process is not None and process.poll() is None
            if self._embedded_runtime is not None:
                alive = True
            else:
                alive = (
                    self._external_attached
                    if self._connection_file is not None
                    else child_alive
                )
            ready = bool(alive and self._client is not None)
            if ready:
                state = 'ready'
            elif self._starting:
                state = 'starting'
            elif alive:
                state = 'unready'
            elif self._last_exit_code is not None and not self._intentional_stop:
                state = 'exited'
            else:
                state = 'stopped'
            if self._backend is not None:
                configured_backend = self._backend
            else:
                from runtime_guards import load_deployment_configuration
                configured_backend = load_deployment_configuration(
                    allow_test_backend_override=(
                        os.environ.get(
                            'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1')
                ).storage_backend
            snapshot: dict[str, object] = {
                'ready': ready,
                'state': state,
                'backend': self._ready_backend or configured_backend,
                'pid': process.pid if child_alive else None,
                'port': self._endpoint_port if ready else None,
                'last_exit_code': self._last_exit_code,
            }
            if snapshot['backend'] == 'tofudb':
                snapshot['search_projection_directory'] = (
                    self._search_projection_directory if alive else '')
                snapshot['native_fastpath'] = self._native_fastpath if ready and child_alive else None
            if self._embedded_runtime is not None:
                snapshot['transport'] = 'embedded'
            return snapshot

    def start(self) -> StorageClient:
        if self._connection_file is not None:
            return self._attach_external_sidecar()
        expected_backend = self._configured_backend()
        native_scope = _tofudb_scope_ids() if expected_backend == 'tofudb' else None
        if native_scope is not None and self._resolve_transport() == 'embedded':
            return self._start_embedded(native_scope)
        expected_protocol = PROTOCOL_VERSION
        launch_command = [sys.executable, '-m', 'lib.storage_authority_sql']
        if native_scope is not None:
            from tofu_db.wire import PROTOCOL_VERSION as V2_PROTOCOL_VERSION

            expected_protocol = V2_PROTOCOL_VERSION
            launch_command = [
                sys.executable, '-m', 'lib.storage_authority_sql.native_launch',
                _resolve_tofudb_binary(), str(native_scope[0]),
                str(native_scope[1] or 0),
            ]
        with self._lock:
            if self.ready:
                return self._client  # type: ignore[return-value]
            if self._starting:
                raise RuntimeError('storage sidecar startup is already in progress')
            self._intentional_stop = False
            self._starting = True
            self._ready_backend = None
            self._endpoint_port = None
            self._last_exit_code = None
            self._token = secrets.token_urlsafe(48)
            env = os.environ.copy()
            # This branch owns a managed child; never inherit the application's
            # external-sidecar publishing/attachment channel into that child.
            env.pop('TOFU_STORAGE_CONNECTION_FILE', None)
            if self._sqlite_fastpath_mode is not None:
                env['TOFU_STORAGE_FASTPATH'] = self._sqlite_fastpath_mode
            if self._project_root is not None:
                env['TOFU_PROJECT_PATH'] = str(self._project_root)
            install_process_resource_defaults(env)
            env['TOFU_STORAGE_TOKEN'] = self._token
            # The sidecar is a child authority, never an independent daemon.
            # Give it an explicit owner identity so it can release the project
            # lease if this worker is SIGKILLed or otherwise disappears before
            # the normal stop() path runs.
            env['TOFU_STORAGE_PARENT_PID'] = str(os.getpid())
            if self._backend is not None:
                env['TOFU_STORAGE_TEST_BACKEND'] = self._backend
            if self._project_root is not None:
                env['TOFU_STORAGE_PROJECT_ROOT'] = str(self._project_root)
                env['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
            creationflags = 0
            if os.name == 'nt':  # pragma: no cover - exercised on Windows CI
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            try:
                projection_directory = ''
                if native_scope is not None:
                    from lib.storage_authority_sql.projection_paths import resolve_search_projection_paths

                    if self._project_root is not None:
                        data_dir = self._project_root / 'data'
                    else:
                        from lib.runtime.runtime_paths import data_root

                        data_dir = Path(data_root())
                    projection_directory = str(resolve_search_projection_paths(
                        data_dir, environ=env).directory)
                    launch_command.append(projection_directory)
                process = subprocess.Popen(
                    launch_command,
                    # Import the installed/source package from its own root.  A
                    # test project-root override controls persistent paths only;
                    # it is not necessarily a Python checkout.
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=env,
                    # The parent keeps this write end open for exactly one
                    # server-process image.  Python creates it close-on-exec, so
                    # parent death *or an in-place execv with the same PID*
                    # delivers EOF to the Sidecar ownership watcher.
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    # The child's stderr (the exec'd tofu-db engine's
                    # structured diagnostics, or the Python sidecar's own)
                    # is piped to a dedicated drainer that mirrors it into
                    # the app log pipeline AND the inherited stderr. The old
                    # stderr=None inheritance lost every respawned child's
                    # output and kept engine errors out of app.log/incident.
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    close_fds=(os.name != 'nt'),
                    creationflags=creationflags,
                )
            except BaseException:
                self._starting = False
                self._token = ''
                raise
            self._process = process
            self._process_started_at = time.monotonic()
            self._search_projection_directory = projection_directory
            self._start_stderr_drainer(process)
            logger.info(
                'Storage sidecar spawned pid=%d backend=%s transport=%s',
                process.pid, expected_backend,
                'daemon' if native_scope is not None else 'sidecar')
            try:
                owner_channel_is_close_on_exec = bool(
                    process.stdin is not None
                    and not os.get_inheritable(process.stdin.fileno())
                )
            except (OSError, ValueError):
                owner_channel_is_close_on_exec = False
            if not owner_channel_is_close_on_exec:
                self._abort_failed_start(process)
                raise RuntimeError(
                    'storage sidecar owner channel is not close-on-exec')
        try:
            ready = self._read_startup_envelope(process)
            if isinstance(ready, dict) and ready.get('type') == STARTUP_ERROR_TYPE:
                code = str(ready.get('code') or 'database_unavailable')[:80]
                diagnostic = str(
                    ready.get('diagnostic') or 'startup was refused')[:500]
                if isinstance(ready.get('retryable'), bool):
                    from lib.storage.errors import StorageError
                    raise StorageError(
                        code, f'storage sidecar startup refused ({code}): {diagnostic}',
                        retryable=ready['retryable'])
                raise RuntimeError(
                    f'storage sidecar startup refused ({code}): {diagnostic}')
            if not isinstance(ready, dict):
                raise ValueError('invalid ready envelope')
            if (ready.get('type') != STARTUP_READY_TYPE
                    or ready.get('protocol') != expected_protocol
                    or not isinstance(ready.get('port'), int)
                    or not 0 < ready['port'] < 65536
                    or ready.get('backend') not in {'sqlite', 'postgres', 'tofudb'}):
                raise ValueError('invalid ready envelope')
            if self._backend is not None:
                expected_backend = self._backend
            else:
                from runtime_guards import load_deployment_configuration
                expected_backend = load_deployment_configuration(
                    env,
                    allow_test_backend_override=(
                        env.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1'),
                ).storage_backend
            if ready['backend'] != expected_backend:
                raise RuntimeError('storage sidecar started an unexpected backend')
            if native_scope is not None:
                from lib.storage.v2_client import StorageV2Client

                client = StorageV2Client(
                    '127.0.0.1', ready['port'], self._token.encode('ascii'),
                    owner_id=native_scope[0], tenant_id=native_scope[1],
                    timeout=rpc_deadline_seconds())
                client.set_readiness_budget(ready.get('resourceBudget'))
            else:
                client = StorageClient(
                    '127.0.0.1', ready['port'], self._token,
                    timeout=rpc_deadline_seconds())
            health = client.health(deadline=2.0)
            if (not health.get('ready')
                    or health.get('backend') != ready['backend']):
                raise RuntimeError('sidecar health preflight did not pass')
        except Exception:
            self._abort_failed_start(process)
            raise
        with self._lock:
            self._client = client
            self._starting = False
            self._ready_backend = ready['backend']
            self._endpoint_port = ready['port']
            self._native_fastpath = ready.get('nativeFastpath')
            self._monitor = threading.Thread(
                target=self._monitor_process,
                args=(process,),
                name='storage-supervisor',
                daemon=True,
            )
            self._monitor.start()
            logger.info(
                'Storage sidecar ready pid=%d backend=%s endpoint=127.0.0.1:%d',
                process.pid, self._ready_backend, self._endpoint_port)
            return client

    def _read_startup_envelope(
        self,
        process: subprocess.Popen[str],
    ) -> object:
        """Consume bounded progress frames until the final startup envelope."""
        messages: queue.Queue[str] = queue.Queue(maxsize=8)
        # Terminal flag set on EVERY exit path of the validating loop below
        # (success, timeout, protocol violation). Without it, an early raise
        # while a chatty child keeps the bounded queue full wedged the reader
        # on put() forever — leaking the thread and the pipe fd even after
        # the child was reaped (2026-09-21 hardening review).
        reader_done = threading.Event()

        def read_messages() -> None:
            assert process.stdout is not None
            while not reader_done.is_set():
                line = process.stdout.readline(
                    MAX_STARTUP_CONTROL_LINE_CHARS + 2)
                # Bounded handoff: a full queue is transient while the owner
                # validates; once the owner has left, drop instead of wedge.
                while True:
                    if reader_done.is_set():
                        return
                    try:
                        messages.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not line:
                    return
                # Progress frames are the only non-terminal control messages.
                # Stop reading as soon as any final/invalid envelope has been
                # handed to the validating owner below; otherwise this daemon
                # remains blocked on the live child's stdout for the entire
                # server lifetime after readiness.
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    return
                if (not isinstance(message, dict)
                        or message.get('type') != STARTUP_PROGRESS_TYPE):
                    return

        reader = threading.Thread(
            target=read_messages,
            name='storage-startup-control',
            daemon=True,
        )
        reader.start()
        try:
            return self._validate_startup_envelope(process, messages)
        finally:
            reader_done.set()
            # The reader can still sit inside a blocking readline of a live
            # but silent child; the abort/stop path reaps the child and its
            # EOF lets the daemon thread finish. Bound the join so startup
            # failure paths never hang on the control channel.
            reader.join(timeout=1.0)

    def _validate_startup_envelope(
        self,
        process: subprocess.Popen[str],
        messages: 'queue.Queue[str]',
    ) -> object:
        started_at = time.monotonic()
        deadline = _StartupDeadline(
            started_at=started_at,
            stall_timeout=self._startup_stall_timeout,
            hard_timeout=self._startup_timeout,
        )
        last_log_at = 0.0
        last_logged_phase = ''
        while True:
            now = time.monotonic()
            remaining = deadline.remaining(now)
            if remaining <= 0:
                raise deadline.timeout_error(now=now)
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise deadline.timeout_error(now=time.monotonic()) from exc
            if not line.strip():
                # EOF is not JSON. Reporting json.loads('') used to hide the
                # child failure behind JSONDecodeError. Give it a brief chance
                # to publish an exit code for one stable diagnostic.
                try:
                    code = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    code = process.poll()
                suffix = f' (exit {code})' if code is not None else ''
                raise RuntimeError(
                    'storage sidecar closed its readiness channel before startup'
                    + suffix)
            if len(line) > MAX_STARTUP_CONTROL_LINE_CHARS + 1:
                raise RuntimeError(
                    'storage sidecar returned an oversized startup response')
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(
                    'storage sidecar returned an invalid startup response') from exc
            if isinstance(message, dict):
                try:
                    progress = parse_startup_progress(message)
                    if progress is not None:
                        deadline.observe(progress, now=time.monotonic())
                except ValueError as exc:
                    raise RuntimeError(
                        'storage sidecar returned invalid startup progress') from exc
                if progress is not None:
                    now = time.monotonic()
                    if (progress.phase != last_logged_phase
                            or progress.completed_bytes == progress.total_bytes
                            or now - last_log_at >= 10.0):
                        logger.info(
                            'Storage sidecar startup progress phase=%s '
                            'bytes=%d/%d heartbeat=%s',
                            progress.phase,
                            progress.completed_bytes,
                            progress.total_bytes,
                            progress.heartbeat,
                        )
                        last_log_at = now
                        last_logged_phase = progress.phase
                    continue
            return message

    def _configured_backend(self) -> str:
        if self._backend is not None:
            return self._backend
        from runtime_guards import load_deployment_configuration

        return load_deployment_configuration(
            allow_test_backend_override=(
                os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1')
        ).storage_backend

    def _configured_transport(self) -> str:
        """Auto, daemon or embedded transport for the native authority.

        The boot gate (``runtime_guards.load_deployment_configuration``)
        validates this knob for the configured path; the supervisor reads the
        same env value directly so fixtures with a test backend override do
        not need a full deployment configuration. Validation is identical and
        fail-closed.
        """
        transport = (
            os.environ.get('TOFUDB_TRANSPORT', '').strip().lower() or 'auto')
        if transport not in {'auto', 'daemon', 'embedded'}:
            raise RuntimeError(
                'TOFUDB_TRANSPORT must be auto, daemon or embedded')
        return transport

    def _resolve_transport(self) -> str:
        """Resolve the effective native transport for this launch.

        An explicit ``daemon``/``embedded`` always wins. The ``auto``
        default picks embedded (no socket, no child process, the lowest
        per-op latency) unless the authority volume classifies as a network
        mount: there the engine's automatic durability tier is Normal
        (bounded-RPO background flush), which the strict-only embedded
        runtime refuses — and per-commit network fsyncs under Strict would
        be the latency storm the tier exists to avoid. The classifier
        mirrors the engine's own (``lib/storage/volume_class.py``); an
        unobservable volume resolves to embedded, matching the engine's
        always-safe Unknown→Strict tiering (never a correctness risk, only
        a possible latency one).
        """
        transport = self._configured_transport()
        if os.environ.get('TOFU_DB_FASTPATH_DIR', '').strip():
            if transport == 'embedded':
                raise RuntimeError('native fastpath requires daemon transport for snapshot replication')
            return 'daemon'
        if os.environ.get('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', '').strip():
            raise RuntimeError('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS requires TOFU_DB_FASTPATH_DIR')
        if transport != 'auto':
            return transport
        if self._project_root is not None:
            data_dir = self._project_root / 'data'
        else:
            from lib.runtime.runtime_paths import data_root

            data_dir = Path(data_root()).resolve()
        from lib.storage.volume_class import NETWORK, volume_class

        classification = volume_class(data_dir)
        resolved = 'daemon' if classification == NETWORK else 'embedded'
        if resolved == 'embedded':
            binary = Path(_resolve_tofudb_binary())
            if not (binary.parent / 'libtofu_db.so').is_file():
                resolved = 'daemon'
        logger.info(
            'Native storage transport auto-resolved to %s '
            '(authority volume class: %s)', resolved, classification)
        return resolved

    def _start_embedded(
        self,
        native_scope: tuple[int, int | None],
    ) -> StorageClient:
        """Open the native authority in-process (no daemon, no socket).

        Mirrors the child-daemon launch contract: the same engine resolution
        and content pin, the same fail-closed bootstrap (init only when the
        authority is entirely absent), the same project lease and locator
        (this PID owns the authority lifecycle), and the same health
        preflight. Readiness budgets come from the embedded runtime's
        authenticated ``system.health`` control operation instead of a
        readiness line. An embedded crash IS a process crash; the health
        monitor fences the degraded case (driver failure, closed handle)
        through the same crash callbacks a daemon exit fires.
        """
        from lib.storage_authority_sql.native_launch import (
            _bootstrap_fresh_authority,
            _durability_args,
        )
        from lib.storage_authority_sql.preflight import ProjectLease

        if _durability_args():
            raise RuntimeError(
                'embedded transport serves strict durability only; '
                'TOFU_DB_DURABILITY=normal requires the daemon transport')
        binary, library = _resolve_tofudb_embedded_engine()
        if self._project_root is not None:
            data_dir = self._project_root / 'data'
        else:
            from lib.runtime.runtime_paths import data_root

            data_dir = Path(data_root()).resolve()
        authority = data_dir / 'tofudb-authority'
        if not authority.is_dir():
            _bootstrap_fresh_authority(binary, authority)
        from lib.storage_authority_sql.projection_paths import (
            resolve_search_projection_paths,
        )

        with self._lock:
            if self.ready:
                return self._client  # type: ignore[return-value]
            if self._starting:
                raise RuntimeError(
                    'storage sidecar startup is already in progress')
            self._intentional_stop = False
            self._starting = True
            self._ready_backend = None
            self._endpoint_port = None
            self._last_exit_code = None
            self._token = secrets.token_urlsafe(48)
            self._embedded_monitor_stop.clear()
        lease = ProjectLease(
            data_dir,
            expected_parent_pid=os.getpid(),
            owner_kind='tofudb',
            owner_label='Tofu-DB',
        )
        runtime = None
        try:
            lease.acquire()
            lease.publish_storage_locator({
                'format': 'tofu.storage-locator/v1', 'backend': 'tofudb',
                'authority_path': str(authority), 'fastpath_active': False,
            })
            from tofu_db.embedded import EmbeddedRuntime

            projection = resolve_search_projection_paths(data_dir)
            runtime = EmbeddedRuntime(
                Path(library),
                authority,
                owner_id=native_scope[0],
                tenant_id=native_scope[1],
                projection_dir=projection.directory,
                secret=self._token.encode('ascii'),
            )
            from lib.storage.v2_embedded_client import (
                EmbeddedStorageV2Client,
            )

            client = EmbeddedStorageV2Client(
                runtime,
                self._token.encode('ascii'),
                owner_id=native_scope[0],
                tenant_id=native_scope[1],
                timeout=rpc_deadline_seconds(),
            )
            health = client.health(deadline=2.0)
            if (not health.get('ready')
                    or health.get('backend') != 'tofudb'):
                raise RuntimeError(
                    'embedded storage health preflight did not pass')
            # The embedded control operation reports the same probe-derived
            # launch budgets the daemon's readiness line carries; the pool
            # sizes itself from the advertised connection cap identically.
            client.set_readiness_budget(health.get('resourceBudget'))
        except Exception:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:
                    logger.exception('Embedded Tofu-DB abort close failed')
            lease.release()
            with self._lock:
                self._client = None
                self._token = ''
                self._starting = False
                self._ready_backend = None
                self._endpoint_port = None
            raise
        with self._lock:
            self._client = client
            self._starting = False
            self._ready_backend = 'tofudb'
            self._embedded_runtime = runtime
            self._embedded_lease = lease
            self._search_projection_directory = str(projection.directory)
            self._monitor = threading.Thread(
                target=self._monitor_embedded,
                args=(client,),
                name='storage-embedded-monitor',
                daemon=True,
            )
            self._monitor.start()
        logger.info(
            'Embedded Tofu-DB authority ready (in-process, no listener)')
        return client

    def _probe_embedded_health(self, client: StorageClient) -> str:
        """One embedded health probe in a bounded worker; never wedges here.

        The embedded FFI call has no client-side timeout: an old
        ``libtofu_db.so`` without an engine-side watchdog can block inside
        the engine forever, which would silently kill this monitor — the
        process's only watchdog for the in-process authority. The probe
        therefore runs in a single-use daemon worker joined with a bounded
        budget (probe deadline + one grace). A wedged worker is reported as
        ``'wedged'`` and left behind: at most one outstanding probe worker
        exists per monitor (the monitor fences and returns on the first
        wedge), so the leak is bounded to one thread per wedged engine.
        """
        outcome: dict[str, object] = {}

        def _run() -> None:
            try:
                outcome['health'] = client.health(
                    deadline=self._liveness_probe_deadline_seconds)
            except Exception as exc:
                # Ferried to the monitor thread for classification — the
                # probe worker itself must never raise across threads.
                logger.debug('embedded storage health probe failed: %s', exc)
                outcome['probe_exception'] = exc

        worker = threading.Thread(
            target=_run, name='storage-embedded-probe', daemon=True)
        worker.start()
        budget = (self._liveness_probe_deadline_seconds
                  + _EMBEDDED_PROBE_JOIN_GRACE_SECONDS)
        worker.join(timeout=budget)
        if worker.is_alive():
            return 'wedged'
        if outcome.get('probe_exception') is not None:
            return 'failed'
        health = outcome.get('health')
        if isinstance(health, dict) and health.get('ready'):
            return 'ready'
        return 'failed'

    def _close_embedded_runtime_bounded(self, runtime: object) -> None:
        """Close a possibly-wedged engine handle without wedging the monitor.

        Engine state is unknown after a probe wedge, so the graceful close
        can itself block inside the engine; run it in a daemon worker and
        bound the join. A handle that still has not closed is leaked exactly
        once (bounded) and reported — never silently retried here, because
        the in-process restart path re-opens the authority from scratch.
        """
        def _close() -> None:
            try:
                runtime.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception('Embedded Tofu-DB fence close failed')

        worker = threading.Thread(
            target=_close, name='storage-embedded-close', daemon=True)
        worker.start()
        worker.join(timeout=_EMBEDDED_WEDGED_CLOSE_BUDGET_SECONDS)
        if worker.is_alive():
            logger.critical(
                'Embedded Tofu-DB engine close did not return within %.1fs '
                'after a probe wedge; leaking exactly one engine handle '
                '(bounded) so the restart path can proceed',
                _EMBEDDED_WEDGED_CLOSE_BUDGET_SECONDS)

    def _fence_embedded(self, client: StorageClient, *, wedged: bool) -> None:
        """Revoke readiness and fire the daemon-exit crash path, in-process.

        A wedged or failed embedded engine leaves the authority state
        unknown, so fencing always means the full in-process restart path:
        drop the client, close the engine (bounded when wedged), release
        the project lease, and fire the same crash callbacks a daemon exit
        would. Intentional stops and a replaced client never fence.
        """
        with self._lock:
            if client is not self._client or self._intentional_stop:
                return
            self._last_exit_code = -1
            self._client = None
            self._ready_backend = None
            runtime, self._embedded_runtime = self._embedded_runtime, None
            lease, self._embedded_lease = self._embedded_lease, None
        self._close_client_quietly(client)
        if runtime is not None:
            if wedged:
                self._close_embedded_runtime_bounded(runtime)
            else:
                try:
                    runtime.close()  # type: ignore[attr-defined]
                except Exception:
                    logger.exception('Embedded Tofu-DB fence close failed')
        if lease is not None:
            lease.release()  # type: ignore[attr-defined]
        logger.critical('Embedded Tofu-DB authority became unavailable')
        for callback in tuple(self._crash_callbacks):
            try:
                callback(-1)
            except Exception:
                logger.exception('Storage crash callback failed')

    def _monitor_embedded(self, client: StorageClient) -> None:
        """Fence the application if the in-process authority goes unhealthy.

        A fatal embedded crash ends this process, so the monitor owns the
        degraded case: engine-reported unhealth (a failed driver), a raised
        transport error, or a wedged FFI probe revokes readiness and fires
        the same crash callbacks a daemon exit would. Fencing requires the
        same run of consecutive probe failures the managed-daemon monitor
        requires (``_liveness_probe_config``), so one transiently slow probe
        never restarts a healthy in-process authority; a probe WEDGE is
        stronger evidence (the engine stopped answering at the FFI
        boundary) and fences immediately.
        """
        failures = 0
        while not self._embedded_monitor_stop.wait(
                self._liveness_interval_seconds):
            verdict = self._probe_embedded_health(client)
            if verdict == 'ready':
                if failures:
                    logger.info(
                        'Embedded Tofu-DB liveness recovered after %d '
                        'failed probe(s)', failures)
                failures = 0
                continue
            if verdict == 'wedged':
                logger.critical(
                    'Embedded Tofu-DB health probe wedged past %.1fs; the '
                    'engine state is unknown — fencing for a full '
                    'in-process restart',
                    self._liveness_probe_deadline_seconds
                    + _EMBEDDED_PROBE_JOIN_GRACE_SECONDS)
                self._fence_embedded(client, wedged=True)
                return
            failures += 1
            if failures < self._liveness_failure_threshold:
                logger.warning(
                    'Embedded Tofu-DB liveness probe failed (%d/%d)',
                    failures, self._liveness_failure_threshold)
                continue
            logger.critical(
                'Embedded Tofu-DB failed %d consecutive liveness probes; '
                'fencing for a full in-process restart', failures)
            self._fence_embedded(client, wedged=False)
            return

    def _attach_external_sidecar(self) -> StorageClient:
        """Attach to a co-located container through its private handoff file."""
        assert self._connection_file is not None
        with self._lock:
            if self.ready:
                return self._client  # type: ignore[return-value]
            if self._starting:
                raise RuntimeError('storage sidecar startup is already in progress')
            self._intentional_stop = False
            self._starting = True
            self._ready_backend = None
            self._endpoint_port = None
            self._last_exit_code = None
            self._external_attached = False
            self._external_monitor_stop.clear()

        deadline = time.monotonic() + self._startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                connection = read_connection_file(self._connection_file)
                expected_backend = self._configured_backend()
                if connection['backend'] != expected_backend:
                    raise RuntimeError(
                        'storage connection file selected an unexpected backend')
                client = StorageClient(
                    connection['host'], connection['port'], connection['token'],
                    timeout=rpc_deadline_seconds())
                health = client.health(deadline=2.0)
                if (not health.get('ready')
                        or health.get('backend') != expected_backend):
                    raise RuntimeError(
                        'external storage sidecar failed its health handshake')
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            with self._lock:
                self._starting = False
                self._client = None
                self._external_attached = False
            raise RuntimeError(
                'external storage sidecar startup timed out') from last_error

        with self._lock:
            self._client = client
            self._token = connection['token']
            self._starting = False
            self._external_attached = True
            self._ready_backend = connection['backend']
            self._endpoint_port = connection['port']
            self._monitor = threading.Thread(
                target=self._monitor_external_sidecar,
                args=(client,),
                name='storage-external-monitor',
                daemon=True,
            )
            self._monitor.start()
        logger.info(
            'Attached external Storage Sidecar backend=%s endpoint=%s:%d',
            connection['backend'], connection['host'], connection['port'])
        return client

    def _monitor_external_sidecar(self, client: StorageClient) -> None:
        """Fence the application after a co-container health handshake fails.

        Uses the same consecutive-failure threshold as the managed-daemon
        monitor (``_liveness_probe_config``): one transiently slow probe
        must never detach a healthy co-container; a sustained run of
        failures keeps the established detach/reattach recovery action.
        """
        failures = 0
        while not self._external_monitor_stop.wait(
                self._liveness_interval_seconds):
            try:
                health = client.health(
                    deadline=self._liveness_probe_deadline_seconds)
                ready = bool(isinstance(health, dict) and health.get('ready'))
            except Exception as exc:
                logger.debug('storage health monitor probe failed: %s', exc)
                ready = False
            if ready:
                if failures:
                    logger.info(
                        'External Storage Sidecar liveness recovered after '
                        '%d failed probe(s)', failures)
                failures = 0
                continue
            failures += 1
            if failures < self._liveness_failure_threshold:
                logger.warning(
                    'External Storage Sidecar liveness probe failed (%d/%d)',
                    failures, self._liveness_failure_threshold)
                continue
            logger.critical(
                'External Storage Sidecar failed %d consecutive liveness '
                'probes; detaching', failures)
            with self._lock:
                if client is not self._client or self._intentional_stop:
                    return
                self._last_exit_code = -1
                self._client = None
                self._external_attached = False
                self._ready_backend = None
                self._endpoint_port = None
            logger.critical('External Storage Sidecar became unavailable')
            for callback in tuple(self._crash_callbacks):
                try:
                    callback(-1)
                except Exception:
                    logger.exception('Storage crash callback failed')
            return

    def _abort_failed_start(self, process: subprocess.Popen[str]) -> None:
        self._close_owner_channel(process)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        # The reaped child's stderr pipe is at EOF; collect the drainer so
        # the next spawn never observes a live one bound to a dead child.
        self._join_stderr_drainer()
        with self._lock:
            self._last_exit_code = process.returncode
            self._client = None
            self._process = None
            self._process_started_at = None
            self._token = ''
            self._starting = False
            self._ready_backend = None
            self._endpoint_port = None

    @staticmethod
    def _close_owner_channel(process: subprocess.Popen[str]) -> None:
        """Close the one-image parent lease without leaking a pipe descriptor."""
        channel = process.stdin
        if channel is None:
            return
        try:
            channel.close()
        except (OSError, ValueError):
            # Process exit and concurrent stop can close the same wrapper.
            pass

    def _start_stderr_drainer(self, process: subprocess.Popen[str]) -> None:
        """Bind one stderr drainer thread to one managed child.

        A previous child's drainer must have observed EOF and exited before
        a new spawn reuses this supervisor; a still-alive one is lifecycle
        evidence (a child whose pipe never reached EOF) and is reported, so
        two drainers can never silently share one pipe or one child pair.
        """
        previous = self._stderr_drainer
        if previous is not None and previous.is_alive():
            previous.join(timeout=0.5)
            if previous.is_alive():
                logger.critical(
                    'Storage sidecar stderr drainer %s is still alive at the '
                    'next spawn; the previous child pipe never reached EOF',
                    previous.name)
        self._stderr_drainer = None
        stream = process.stderr
        if stream is None:
            return
        drainer = threading.Thread(
            target=_drain_child_stderr,
            args=(stream,),
            kwargs={'pid': process.pid},
            name=f'storage-stderr-drainer-{process.pid}',
            daemon=True,
        )
        drainer.start()
        self._stderr_drainer = drainer

    def _join_stderr_drainer(self, *, timeout: float = 2.0) -> None:
        """Bounded wait for the current child's drainer after its EOF."""
        drainer = self._stderr_drainer
        if (drainer is not None
                and drainer is not threading.current_thread()
                and drainer.is_alive()):
            drainer.join(timeout=max(0.0, timeout))
            if drainer.is_alive():
                logger.warning(
                    'Storage sidecar stderr drainer %s did not exit within '
                    '%.1fs of the child EOF', drainer.name, timeout)

    def _probe_managed_child_liveness(self, client: StorageClient) -> bool:
        """One bounded readiness probe; a failure or non-ready answer counts.

        Never raises: the monitor thread is the managed child's only
        watchdog and must survive every client/transport error mode. Both
        the SQL Sidecar client and the native storage.v2 client expose
        ``health(deadline=...)``. The storage.v2 client runs the probe on a
        dedicated session outside its query pool (``_health_call``), so a
        pool saturated by real work cannot fail the probe — a failure here
        means the daemon itself is not answering, which is the wedged signal.
        """
        try:
            health = client.health(
                deadline=self._liveness_probe_deadline_seconds)
        except Exception as exc:
            logger.debug(
                'Storage sidecar liveness probe error: %s: %s',
                type(exc).__name__, str(exc)[:200])
            return False
        return bool(isinstance(health, dict) and health.get('ready'))

    def _terminate_unresponsive_child(
            self, process: subprocess.Popen[str]) -> int | None:
        """Bounded stop for a child that answers neither RPCs nor exit.

        Owner-channel EOF releases authority ownership and gives a merely
        busy child one graceful window; SIGTERM and SIGKILL then bound the
        teardown so the monitor's ordinary exit path (fence + crash
        callbacks + restart) always runs. Returns the exit code, or ``None``
        when even SIGKILL did not reap the child within its grace — a
        kernel-state process no software recycle can help, and the monitor
        still fences and reports rather than hanging on wait() forever.
        Every escalation step is logged: this sequence is the only durable
        evidence of how a wedged authority was recycled.
        """
        logger.warning(
            'Storage sidecar pid=%d unresponsive; closing the owner channel '
            'and waiting %.1fs for a graceful exit',
            process.pid, _LIVENESS_EOF_GRACE_SECONDS)
        self._close_owner_channel(process)
        try:
            return process.wait(timeout=_LIVENESS_EOF_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is None:
            logger.warning(
                'Storage sidecar pid=%d ignored owner-channel EOF for %.1fs; '
                'sending SIGTERM', process.pid, _LIVENESS_EOF_GRACE_SECONDS)
            process.terminate()
            try:
                return process.wait(timeout=_LIVENESS_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            logger.error(
                'Storage sidecar pid=%d ignored SIGTERM for %.1fs; sending '
                'SIGKILL', process.pid, _LIVENESS_TERMINATE_GRACE_SECONDS)
            process.kill()
            try:
                return process.wait(timeout=_LIVENESS_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.critical(
                    'Storage sidecar pid=%d ignored SIGKILL for %.0fs; '
                    'leaving the unreapable child to the OS',
                    process.pid, _LIVENESS_KILL_GRACE_SECONDS)
        return process.poll()

    def _escalate_overload_recycle_backoff(self) -> None:
        """Raise the floor before the next overload-forced recycle.

        The first forced recycle waits ``_liveness_overload_recycle_cooldown``;
        each subsequent one, with no healthy probe in between, doubles that
        floor up to ``_MAX_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_S``. The backoff
        lives on the supervisor (not the monitor) because each recycle ends
        its monitor and a replacement child gets a fresh one; it is cleared by
        any successful probe. This is what keeps the bound from turning into
        the recycle storm the overload gate was introduced to stop.
        """
        current = self._overload_recycle_backoff
        next_floor = (
            self._liveness_overload_recycle_cooldown
            if current <= 0.0
            else min(current * 2.0,
                     _MAX_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_S))
        self._overload_recycle_backoff = next_floor
        self._overload_recycle_not_before = time.monotonic() + next_floor

    def _host_overload_sticky(self) -> str | None:
        """Overload verdict with stickiness against cache-reclaim flicker.

        Wraps ``_host_overload_detail``: a fresh overloaded reading arms the
        sticky window and is returned verbatim; a healthy reading inside the
        window still reports overloaded (annotated as sticky) so a momentary
        page-cache reclaim dip cannot reset the liveness failure attribution
        mid-streak; a healthy reading past the window clears the latch.
        """
        now = time.monotonic()
        detail = _host_overload_detail(
            self._liveness_overload_mem_ratio,
            self._liveness_overload_load_factor)
        if detail is not None:
            self._overload_sticky_until = (
                now + self._liveness_overload_sticky_seconds)
            self._overload_sticky_detail = detail
            return detail
        if (self._overload_sticky_detail is not None
                and now < self._overload_sticky_until):
            return self._overload_sticky_detail + ' (sticky)'
        self._overload_sticky_detail = None
        self._overload_sticky_until = 0.0
        return None
    def _monitor_process(self, process: subprocess.Popen[str]) -> None:
        """Watch one managed child for exit AND for live-but-unresponsive service.

        Waiting on exit alone missed the wedged-daemon failure mode: a child
        whose listener is dead but whose process stays alive kept
        ``ready=True`` indefinitely, crash callbacks never fired, and storage
        stayed down until an operator restarted the application (2026-09-21:
        27 minutes of database_unavailable behind one tofu-db daemon that
        had closed its listener without exiting). The external co-container
        monitor has always health-probed its peer; the managed child now
        gets the same treatment — a bounded run of consecutive liveness
        failures terminates the child so the ordinary exit path below fences
        readiness and schedules the restart. One refinement: while the host
        itself is observably overloaded (cgroup memory pressure or a runaway
        load average), failed probes are attributed to starvation rather
        than a wedged child, the failure streak is reset, and the recycle is
        withheld — killing a starved-but-healthy child only converts host
        pressure into a crash/restart loop (2026-09-22). Suppression is
        bounded: after ``_liveness_overload_max_suppress`` consecutive
        failures the monitor forces exactly one recycle, rate-limited by an
        escalating cooldown, so a shared host whose overload never clears
        still cannot withhold a wedged authority forever.
        """
        failures = 0
        suppressed = 0
        last_suppressed_log = 0.0
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            client = None
            with self._lock:
                if process is self._process and not self._intentional_stop:
                    client = self._client
            if client is not None:
                if self._probe_managed_child_liveness(client):
                    if failures:
                        logger.info(
                            'Storage sidecar liveness recovered after %d '
                            'failed probe(s)', failures)
                    failures = 0
                    # A served probe proves the child is healthy: clear the
                    # overload-forced recycle rate limit.
                    self._overload_recycle_backoff = 0.0
                    self._overload_recycle_not_before = 0.0
                else:
                    overload = self._host_overload_sticky()
                    if overload is not None:
                        suppressed += 1
                        if failures:
                            logger.info(
                                'Storage sidecar liveness failure streak '
                                'reset under host overload (%s)', overload)
                        failures = 0
                        now = time.monotonic()
                        if (suppressed >= self._liveness_overload_max_suppress
                                and now >= self._overload_recycle_not_before):
                            # The child stayed unresponsive through a whole
                            # suppression window. A shared cgroup pinned near
                            # its limit never clears, so withholding forever
                            # would be a permanent outage; force exactly one
                            # bounded recycle and escalate the floor so a
                            # starved child is not cold-started into a storm.
                            # The probe runs on a dedicated session
                            # (v2_client._health_call), so this verdict is no
                            # longer reachable from a merely busy pool — a
                            # failed probe here means the daemon itself is
                            # not answering.
                            logger.critical(
                                'Storage sidecar pid=%d stayed unresponsive '
                                'through %d probes while the host is '
                                'overloaded (%s); forcing a bounded recycle '
                                'so a wedged authority cannot be withheld '
                                'indefinitely', process.pid, suppressed,
                                overload)
                            self._escalate_overload_recycle_backoff()
                            recycled_code = (
                                self._terminate_unresponsive_child(
                                    process))
                            return_code = (
                                recycled_code
                                if recycled_code is not None else -1)
                            break
                        if (suppressed == 1
                                or now - last_suppressed_log >= 30.0):
                            logger.warning(
                                'Storage sidecar liveness probe failed '
                                '(suppressed=%d) but the host is overloaded '
                                '(%s); withholding recycle of starved '
                                'pid=%d', suppressed, overload, process.pid)
                            last_suppressed_log = now
                    else:
                        if suppressed:
                            logger.warning(
                                'Host overload cleared after %d suppressed '
                                'liveness failure(s); normal recycle '
                                'accounting resumed', suppressed)
                            suppressed = 0
                        failures += 1
                        if failures < self._liveness_failure_threshold:
                            logger.warning(
                                'Storage sidecar liveness probe failed (%d/%d) '
                                'pid=%d',
                                failures, self._liveness_failure_threshold,
                                process.pid)
                        else:
                            logger.critical(
                                'Storage sidecar pid=%d failed %d consecutive '
                                'liveness probes; terminating so the exit path '
                                'can fence and restart storage',
                                process.pid, failures)
                            recycled_code = self._terminate_unresponsive_child(
                                process)
                            # An unreapable child is still fenced through the
                            # ordinary exit path with a synthetic code so crash
                            # callbacks schedule the restart loop; the loop
                            # keeps retrying in case the kernel ever reaps it.
                            return_code = (
                                recycled_code
                                if recycled_code is not None else -1)
                            break
            time.sleep(self._liveness_interval_seconds)
        self._close_owner_channel(process)
        with self._lock:
            if process is not self._process:
                return
            crashed = not self._intentional_stop
            uptime_seconds = (
                None if self._process_started_at is None
                else time.monotonic() - self._process_started_at)
            self._last_exit_code = return_code
            self._client = None
            self._process = None
            self._process_started_at = None
            self._token = ''
            self._starting = False
            self._ready_backend = None
            self._endpoint_port = None
        self._join_stderr_drainer()
        uptime_note = f' uptime={uptime_seconds:.1f}s' if uptime_seconds is not None else ''
        if crashed:
            logger.critical(
                'Storage sidecar exited unexpectedly exit_code=%d%s%s',
                return_code, _exit_signal_note(return_code), uptime_note)
            for callback in tuple(self._crash_callbacks):
                try:
                    callback(return_code)
                except Exception:
                    logger.exception('Storage crash callback failed')
        else:
            logger.info(
                'Storage sidecar stopped exit_code=%d%s%s',
                return_code, _exit_signal_note(return_code), uptime_note)

    def add_crash_callback(self, callback: Callable[[int], None]) -> None:
        if not callable(callback):
            raise TypeError('storage crash callback must be callable')
        with self._lock:
            self._crash_callbacks.append(callback)

    @staticmethod
    def _close_client_quietly(client: StorageClient | None) -> None:
        """Deterministic client teardown on the stop path.

        Dropping the last reference is not enough: the storage.v2 client's
        session pool holds a bound-method factory back into the client, so
        the pair is collectible only by the cyclic GC — until it happens to
        run, pooled sessions stay open and the daemon's connection workers
        wait out the socket I/O timeout before they can exit, pushing
        shutdown past the stop budget and into SIGKILL.
        """
        if client is None:
            return
        closer = getattr(client, 'close', None)
        if closer is None:
            return
        try:
            closer()
        except Exception as _e:
            logger.debug('close client quietly failed: %s', _e)
            pass

    def stop(self, timeout: float = 10.0) -> None:
        if self._connection_file is not None:
            with self._lock:
                self._intentional_stop = True
                client, self._client = self._client, None
                self._starting = False
                self._external_attached = False
                self._ready_backend = None
                self._endpoint_port = None
                monitor = self._monitor
            self._close_client_quietly(client)
            self._external_monitor_stop.set()
            if (monitor is not None
                    and monitor is not threading.current_thread()
                    and monitor.is_alive()):
                monitor.join(timeout=max(0.0, timeout))
            return
        with self._lock:
            process = self._process
            native = self._ready_backend == 'tofudb'
            embedded, self._embedded_runtime = self._embedded_runtime, None
            lease, self._embedded_lease = self._embedded_lease, None
            self._intentional_stop = True
            client, self._client = self._client, None
            self._starting = False
        close_started = time.monotonic()
        self._close_client_quietly(client)
        logger.info('Storage stop step=close_client elapsed_s=%.3f', time.monotonic() - close_started)
        if embedded is not None:
            # Graceful stop exactly like the daemon's stdin-EOF tail: drivers
            # join, then both engines' WAL windows retire so the next open
            # replays ~nothing. There is no kill backstop — an in-process
            # authority cannot outlive its process.
            self._embedded_monitor_stop.set()
            monitor = self._monitor
            if (monitor is not None
                    and monitor is not threading.current_thread()
                    and monitor.is_alive()):
                monitor.join(timeout=max(0.0, min(timeout, 2.0)))
            try:
                embedded.close()  # type: ignore[attr-defined]
            except Exception:
                logger.exception('Embedded Tofu-DB graceful close failed')
            if lease is not None:
                lease.release()  # type: ignore[attr-defined]
            with self._lock:
                self._token = ''
                self._ready_backend = None
                self._endpoint_port = None
            return
        if process is None:
            return
        if process.poll() is None:
            # EOF is the authority-ownership signal and also covers execv,
            # where the parent PID is intentionally stable.  SIGTERM remains a
            # prompt graceful-stop nudge; bounded kill is the final backstop.
            child_started = time.monotonic()
            self._close_owner_channel(process)
            logger.info('Storage stop step=owner_eof pid=%s budget_s=%.3f', process.pid, timeout)
            if not native:
                process.terminate()
            try:
                process.wait(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired:
                logger.warning('Storage stop step=child_exit state=timeout pid=%s elapsed_s=%.3f; killing child',
                               process.pid, time.monotonic() - child_started)
                process.kill()
                process.wait(timeout=3)
            logger.info('Storage stop step=child_exit pid=%s exit_code=%s elapsed_s=%.3f',
                        process.pid, process.poll(), time.monotonic() - child_started)
        self._close_owner_channel(process)
        self._join_stderr_drainer()
        with self._lock:
            if self._process is process:
                self._process = None
                self._process_started_at = None
            self._token = ''
            self._ready_backend = None
            self._endpoint_port = None

    def wait_until_unready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self.ready and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self.ready

    def __enter__(self) -> 'StorageSupervisor':
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


__all__ = ['StorageSupervisor']
