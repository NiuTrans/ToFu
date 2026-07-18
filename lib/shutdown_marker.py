"""lib/shutdown_marker.py — Clean-shutdown dirty-bit for OS-kill detection.

The problem this solves (a real production incident): in a shared-cgroup pod
the kernel OOM killer can ``SIGKILL`` our process because of *neighbour*
memory pressure even when our own RSS is tiny (``logs/app.log`` shows the
cgroup at 99.9% of 200 GiB while our RSS is ~11 GiB). ``SIGKILL`` cannot be
trapped, so the graceful ``[Server] Received signal … shutting down`` line
never runs and the death leaves NO trace — it looks like a silent "Killed".
A supervisor respawns us and the cycle repeats (8,545 process starts on
2026-07-09 vs a normal ~100–500/day).

The mechanism is a filesystem *dirty-bit*:

  * :func:`arm` is called ONCE at boot — it writes ``state="running"`` (dirty).
  * :func:`mark_clean` is called ONLY on a deliberate graceful exit path (the
    SIGTERM/SIGINT handler, the in-place re-exec, and the manual shutdown
    button) — it flips the marker to ``state="clean"`` with the *reason*.
  * :func:`classify_previous_shutdown` is called at the NEXT boot, BEFORE
    :func:`arm` overwrites the marker. A marker still reading ``running``
    proves the previous process died WITHOUT running any clean path → an
    untrappable OS kill (SIGKILL/OOM) or hard crash. ``clean`` proves a
    controlled shutdown (manual button, signal drain, or restart).

This is the single source of truth for "was the last exit manual?", consumed
by :func:`lib.tasks_pkg.manager.recover_stale_tasks_on_startup` to tag each
recovered turn ``killed`` (auto-recover) vs ``manual`` (intentional).

The marker lives next to the instance lock in the writable data root, so it
is per-install-copy isolated exactly like ``.server.lock``.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any

from lib.json_store import read_json, write_json_atomic
from lib.log import audit_log, get_logger
from lib.runtime_paths import data_root

logger = get_logger(__name__)

# Marker filename (in the writable data root, alongside .server.lock).
_MARKER_NAME = '.server_shutdown.json'

# Boot-history ring (separate small file — arm() overwrites the marker, so the
# boot timestamps cannot live in it). Used to detect a restart STORM: the
# incident was 3,286 process starts in one evening driven by neighbour OOM, so
# killed-turn recovery must NOT blindly re-fire billed LLM turns into a crash
# loop (a thundering herd that worsens the OOM and re-kills the same turns).
_BOOTS_NAME = '.server_boots.json'
_BOOTS_KEEP = 50   # bounded ring — newest N boot timestamps

# Restart-storm thresholds (env-tunable; defaults chosen so a normal
# restart/redeploy — a handful of boots — never trips it, but the incident's
# many-boots-per-minute pattern does).
_STORM_THRESHOLD = int(os.environ.get('TOFU_RESTART_STORM_THRESHOLD', '5') or 5)
_STORM_WINDOW_SECS = float(os.environ.get('TOFU_RESTART_STORM_WINDOW_SECS', '120') or 120)

# Reasons that count as a DELIBERATE (manual/controlled) shutdown. A marker
# left in state="running" is, by definition, none of these — it is a kill.
CLEAN_REASONS = frozenset({'manual', 'signal', 'restart'})

# classify_previous_shutdown verdicts.
VERDICT_FIRST_BOOT = 'first_boot'   # no prior marker — nothing to classify
VERDICT_CLEAN = 'clean'             # previous exit ran a graceful path
VERDICT_UNCLEAN = 'unclean'         # previous process died dirty → OS kill


def marker_path() -> str:
    """Absolute path to the shutdown marker file."""
    return os.path.join(data_root(), _MARKER_NAME)


def _read_marker() -> dict[str, Any] | None:
    """Read the current marker, or ``None`` if absent/unreadable."""
    data = read_json(marker_path(), default=None)
    if isinstance(data, dict):
        return data
    return None


def classify_previous_shutdown() -> dict[str, Any]:
    """Inspect the marker LEFT BY THE PREVIOUS PROCESS and classify its exit.

    MUST be called at boot BEFORE :func:`arm` overwrites the marker.

    Returns a dict::

        {
          "verdict": "first_boot" | "clean" | "unclean",
          "manual":  bool,          # True only for a deliberate shutdown
          "reason":  str | None,    # the clean reason, when verdict == clean
          "prev_pid": int | None,
          "prev_host": str | None,
          "age_secs": float | None, # seconds since the previous boot/clean stamp
        }

    ``verdict == "unclean"`` is the OS-kill signal: the previous process never
    ran a clean path (SIGKILL/OOM or hard crash).
    """
    m = _read_marker()
    if not m:
        return {'verdict': VERDICT_FIRST_BOOT, 'manual': False, 'reason': None,
                'prev_pid': None, 'prev_host': None, 'age_secs': None}

    state = m.get('state')
    prev_pid = m.get('pid')
    prev_host = m.get('host')
    reason = m.get('reason')
    stamp = m.get('clean_ts') or m.get('boot_ts')
    age = None
    try:
        if stamp:
            age = max(0.0, time.time() - float(stamp))
    except (TypeError, ValueError) as e:
        logger.debug('[shutdown_marker] bad timestamp in marker: %s', e)

    if state == 'clean':
        return {'verdict': VERDICT_CLEAN, 'manual': reason in CLEAN_REASONS,
                'reason': reason, 'prev_pid': prev_pid, 'prev_host': prev_host,
                'age_secs': age}
    # Any non-clean state (running, or a corrupt/missing state field) means the
    # previous process did not reach a clean path → treat as an OS kill.
    return {'verdict': VERDICT_UNCLEAN, 'manual': False, 'reason': None,
            'prev_pid': prev_pid, 'prev_host': prev_host, 'age_secs': age}


def arm(pid: int | None = None) -> None:
    """Arm the dirty-bit: record this process as ``running`` (unclean until told).

    Idempotent; call once at boot AFTER :func:`classify_previous_shutdown`.
    Best-effort — a failure to write the marker never blocks boot (the worst
    case is a future boot mis-classifying, which only affects diagnostics).
    """
    pid = pid if pid is not None else os.getpid()
    try:
        write_json_atomic(marker_path(), {
            'state': 'running',
            'pid': pid,
            'host': socket.gethostname(),
            'boot_ts': time.time(),
            'reason': 'boot',
        }, fsync=True)
        logger.debug('[shutdown_marker] armed (pid=%d)', pid)
    except OSError as e:
        logger.warning('[shutdown_marker] arm failed (non-fatal): %s', e)


def mark_clean(reason: str = 'signal') -> None:
    """Flip the marker to ``clean`` — a DELIBERATE shutdown is in progress.

    Call from EVERY graceful exit path: the SIGTERM/SIGINT handler
    (``reason="signal"``), the in-place re-exec (``reason="restart"``), and the
    manual shutdown button (``reason="manual"``). The next boot will then
    classify the exit as ``clean`` and NOT flag it as an OS kill.

    Must be safe to call from a signal handler: it does one atomic file write
    and swallows/logs any error (never raises into the handler).
    """
    if reason not in CLEAN_REASONS:
        logger.debug('[shutdown_marker] non-standard clean reason %r → recorded as-is', reason)
    try:
        write_json_atomic(marker_path(), {
            'state': 'clean',
            'pid': os.getpid(),
            'host': socket.gethostname(),
            'clean_ts': time.time(),
            'reason': reason,
        }, fsync=True)
        logger.info('[shutdown_marker] marked clean (reason=%s)', reason)
    except OSError as e:
        logger.warning('[shutdown_marker] mark_clean failed (non-fatal): %s', e)


def _boots_path() -> str:
    return os.path.join(data_root(), _BOOTS_NAME)


def record_boot(ts: float | None = None) -> list[float]:
    """Append this boot's timestamp to the bounded boot-history ring.

    Returns the (post-append, newest-last) list of boot timestamps. Best-effort:
    a read/write failure returns whatever we have and never blocks boot.
    """
    ts = ts if ts is not None else time.time()
    boots: list[float] = []
    raw = read_json(_boots_path(), default=None)
    if isinstance(raw, list):
        for v in raw:
            try:
                boots.append(float(v))
            except (TypeError, ValueError):
                continue
    boots.append(ts)
    boots = boots[-_BOOTS_KEEP:]
    try:
        write_json_atomic(_boots_path(), boots, fsync=True)
    except OSError as e:
        logger.warning('[shutdown_marker] record_boot write failed (non-fatal): %s', e)
    return boots


def is_restart_storm(boots: list[float] | None = None, *, now: float | None = None) -> bool:
    """True when boots are arriving too fast — a crash/restart storm is active.

    A storm means the process is being repeatedly killed (the neighbour-OOM
    incident). During a storm, killed-turn recovery must STAND DOWN: re-firing
    billed LLM turns would pile more memory pressure onto an already-dying pod
    and re-kill the same turns forever. Definition: ``>= _STORM_THRESHOLD`` boots
    within the last ``_STORM_WINDOW_SECS`` seconds (this boot counts).
    """
    now = now if now is not None else time.time()
    if boots is None:
        raw = read_json(_boots_path(), default=None)
        boots = [float(v) for v in raw if _is_num(v)] if isinstance(raw, list) else []
    recent = [b for b in boots if (now - b) <= _STORM_WINDOW_SECS]
    return len(recent) >= _STORM_THRESHOLD


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def report_and_arm() -> dict[str, Any]:
    """Boot convenience: classify the previous exit, LOG/AUDIT it, then re-arm.

    Returns the classification dict from :func:`classify_previous_shutdown` so
    the caller (startup recovery) can tag recovered turns ``killed`` vs
    ``manual``. An ``unclean`` verdict is logged at ERROR and audited so the
    silent-kill incident is finally visible in ``logs/error.log`` +
    ``logs/audit.log``.
    """
    cls = classify_previous_shutdown()
    verdict = cls['verdict']
    if verdict == VERDICT_UNCLEAN:
        logger.error(
            '[shutdown_marker] PREVIOUS EXIT WAS UNCLEAN — the last server '
            'process (pid=%s host=%s) died WITHOUT a graceful shutdown '
            '(untrappable OS SIGKILL/OOM or hard crash). Interrupted turns '
            'from that process will be recovered and tagged "killed".',
            cls.get('prev_pid'), cls.get('prev_host'))
        audit_log('unclean_shutdown_detected',
                  prev_pid=cls.get('prev_pid'), prev_host=cls.get('prev_host'),
                  age_secs=cls.get('age_secs'))
    elif verdict == VERDICT_CLEAN:
        logger.info('[shutdown_marker] previous exit was clean (reason=%s)',
                    cls.get('reason'))
    else:
        logger.info('[shutdown_marker] no prior shutdown marker (first boot on '
                    'this data dir)')
    # Record this boot and detect a restart STORM so killed-turn recovery can
    # stand down (never re-fire billed turns into a crash loop).
    try:
        boots = record_boot()
        cls['restart_storm'] = is_restart_storm(boots)
        if cls['restart_storm']:
            logger.error(
                '[shutdown_marker] RESTART STORM detected (>=%d boots within %ds) '
                '— killed-turn auto-recovery will STAND DOWN to avoid a '
                'thundering-herd re-dispatch that worsens the crash loop.',
                _STORM_THRESHOLD, int(_STORM_WINDOW_SECS))
            audit_log('restart_storm_detected',
                      threshold=_STORM_THRESHOLD, window_secs=_STORM_WINDOW_SECS)
    except Exception as e:
        logger.warning('[shutdown_marker] boot-ring/storm check failed (non-fatal): %s', e)
        cls['restart_storm'] = False
    arm()
    return cls


__all__ = [
    'CLEAN_REASONS', 'VERDICT_FIRST_BOOT', 'VERDICT_CLEAN', 'VERDICT_UNCLEAN',
    'marker_path', 'classify_previous_shutdown', 'arm', 'mark_clean',
    'report_and_arm', 'record_boot', 'is_restart_storm',
]
