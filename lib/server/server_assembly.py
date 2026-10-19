"""App-assembly startup steps (extracted from server.py).

These are the startup-phase callbacks composed by ``register_server_production_lifecycle``
in server.py. They are passed by object reference into ``lib.production_lifecycle``.
This module must NOT import ``server`` (server.py imports it), so the server-owned
globals they read are injected via :func:`inject_runtime` from the composition root.
"""

import logging
import os
import time


# Late-bound hooks injected by server.py's composition root.
_boot = None
_server_log = None
_DEPLOYMENT_CONFIGURATION = None

_PROJECT_ROOT = None

# Readiness-critical storage steps must ride out a storage sidecar restart.
# Observed 2026-09-23: one ~13s liveness recycle window failed boot phase 4
# (storage recovery); the boot runner's auto-retry budget expired against a
# shared-host memory-pressure recycle cadence, and the server stayed unready
# for hours behind an ever-growing splash. These steps are revision-idempotent,
# so a typed retryable storage failure is retried until this bound; a
# non-retryable error still fails fast and the bound itself still raises.
_READINESS_STORAGE_RETRY_DEADLINE_S = 240.0

# Phase 3 spawns the sidecar inline and fails the whole lifespan on any error.
# A restart can race the previous generation's sidecar still holding the
# data-root lease ("another tofu-db process owns the data directory"): the new
# child exits 1 immediately, which used to hard-fail startup. Bounded retries
# let the old owner process exit and release the lease.
_SIDECAR_START_ATTEMPTS = 6
_SIDECAR_START_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0)

# Boot-scoped handoff: the readiness-critical retire fence records this boot
# generation's elected runs, including durable pending Chat Goal handoffs.
# The post-ready pass consumes this slot once; loss of the slot does not lose
# election because the next startup re-reads the indexed pending authority.
_BOOT_ORCHESTRATION_CONTINUITY_CANDIDATES = []


def set_boot_orchestration_continuity_candidates(candidates) -> None:
    global _BOOT_ORCHESTRATION_CONTINUITY_CANDIDATES
    _BOOT_ORCHESTRATION_CONTINUITY_CANDIDATES = list(candidates or [])


def pop_boot_orchestration_continuity_candidates() -> list:
    """Consume the boot continuity handoff; a second call returns empty."""
    global _BOOT_ORCHESTRATION_CONTINUITY_CANDIDATES
    candidates = _BOOT_ORCHESTRATION_CONTINUITY_CANDIDATES
    _BOOT_ORCHESTRATION_CONTINUITY_CANDIDATES = []
    return candidates
_start_log_aggregate_runtime_after_recovery = None


def inject_runtime(*, boot, server_log, deployment_configuration,
                   tofu_do_mlock, project_root,
                   start_log_aggregate_runtime_after_recovery):
    """Bind server-owned globals so the moved steps keep their old behaviour."""
    global _boot, _server_log, _DEPLOYMENT_CONFIGURATION
    global _tofu_do_mlock, _PROJECT_ROOT
    global _start_log_aggregate_runtime_after_recovery
    _boot = boot
    _server_log = server_log
    _DEPLOYMENT_CONFIGURATION = deployment_configuration
    _tofu_do_mlock = tofu_do_mlock
    _PROJECT_ROOT = project_root
    _start_log_aggregate_runtime_after_recovery = (
        start_log_aggregate_runtime_after_recovery)


def _load_or_create_flask_secret_key():
    from lib.config_dir import config_path as _cfg_path
    _env_key = os.environ.get('FLASK_SECRET_KEY', '').strip()
    if _env_key:
        return _env_key
    _key_file = _cfg_path('flask_secret_key')
    try:
        if os.path.isfile(_key_file):
            with open(_key_file, 'r', encoding='utf-8') as _kf:
                _existing = _kf.read().strip()
            if _existing:
                return _existing
    except Exception as exc:
        logging.getLogger('server').debug(
            '[FlaskSecret] existing key read failed: %s', exc)
    _new_key = os.urandom(32).hex()
    try:
        os.makedirs(os.path.dirname(_key_file), exist_ok=True)
        _flag = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            _fd = os.open(_key_file, _flag, 0o600)
            try:
                os.write(_fd, _new_key.encode('utf-8'))
            finally:
                os.close(_fd)
        except (AttributeError, OSError):
            with open(_key_file, 'w', encoding='utf-8') as _kf:
                _kf.write(_new_key)
    except Exception as e:
        logging.getLogger('server').warning('[FlaskSecret] Failed to persist: %s', e)
    return _new_key


def _check_frontend_artifact():
    from lib.process_roles import CAPABILITY_FRONTEND, process_role_has

    if not process_role_has(
            _DEPLOYMENT_CONFIGURATION.process_role, CAPABILITY_FRONTEND):
        _boot(
            'Frontend asset validation skipped for process role %s.',
            _DEPLOYMENT_CONFIGURATION.process_role,
        )
        return
    try:
        from lib.vite_assets import validate_published_vite_artifact
        validate_published_vite_artifact()
    except Exception as artifact_error:
        role = _DEPLOYMENT_CONFIGURATION.process_role
        message = (
            f'Required frontend artifact validation failed for process role '
            f'{role}: {artifact_error}. Run `npm run build:frontend`, then '
            'restart Tofu.')
        _server_log.error(message)
        raise RuntimeError(message) from artifact_error


def _validate_imports():
    """Validate critical imports at startup."""
    # tofu_search.fetch/search are deliberately ABSENT: tofu-search is an
    # optional, heavyweight capability (pymupdf/onnxruntime native chain,
    # ~2s of the boot phase on a network mount) and ``lib.search.search_runtime``
    # already installs its seams at the first real search/fetch call. An
    # eager validation import here defeated that lazy seam for zero
    # readiness value — a broken search env surfaces, truthfully, at the
    # first search call instead of holding every boot hostage.
    _CRITICAL_IMPORTS = [
        'lib.tasks_pkg.orchestrator',
        'lib.tasks_pkg.executor',
        'lib.search.search_bridge',
        'lib.llm',
    ]
    _boot('Validating critical imports…')
    failures = []
    for mod_name in _CRITICAL_IMPORTS:
        _boot('  • importing %s', mod_name)
        try:
            __import__(mod_name)
        except ImportError as ie:
            failures.append((mod_name, ie))
            _server_log.error('Critical import failed: %s — %s', mod_name, ie)
    if failures:
        msgs = [f'  {m}: {e}' for m, e in failures]
        raise ImportError('Missing dependencies:\n' + '\n'.join(msgs))
    _boot('All critical imports validated.')

    # ── Eager-load heavy C extensions only when mlockall is enabled ──
    # These are the .so modules seen in past SIGBUS faulthandler dumps.
    # Loading them now (under mlockall MCL_FUTURE) ensures their code
    # pages are resident before any request arrives — the demand-fault
    # window that causes Bus errors on FUSE is eliminated.
    _NATIVE_PRELOADS = [
        'PIL._imaging',
        'lxml.etree',
        'greenlet._greenlet',
        'numpy.core._multiarray_umath',
        'markupsafe._speedups',
        'charset_normalizer.md',
    ]
    # These are optional — may not be installed in all environments.
    # yaml._yaml: only used by routes/api_docs.py::openapi_yaml, which already
    # degrades to JSON on ImportError — never a hard dependency.
    _NATIVE_PRELOADS_OPTIONAL = [
        'pymupdf._extra',
        'yaml._yaml',
    ]
    if _tofu_do_mlock:
        _boot('Eager-loading native extensions (FUSE SIGBUS mitigation)…')
        for _mod in _NATIVE_PRELOADS:
            try:
                __import__(_mod)
            except ImportError as _ie:
                _server_log.warning('Native preload failed (required): %s — %s', _mod, _ie)
        for _mod in _NATIVE_PRELOADS_OPTIONAL:
            try:
                __import__(_mod)
            except ImportError as _ie:
                _server_log.debug('Optional native preload %s unavailable: %s',
                                  _mod, _ie)  # optional — not all deployments have these
        _boot('Native extensions preloaded.')
    else:
        _boot('Native extension preload skipped (mlock disabled).')


def _run_readiness_critical_storage_step(
        label, step, *,
        deadline_s=_READINESS_STORAGE_RETRY_DEADLINE_S):
    """Run one readiness-critical storage step, absorbing transient outages.

    Unlike :func:`_run_boot_recovery_step`, a failure is never swallowed: after
    the bounded budget the last error is re-raised so the boot phase still
    latches. Only failures whose typed ``retryable`` flag is ``True`` (e.g.
    ``StorageError`` while the sidecar is mid-restart) are retried; a
    non-retryable error fails immediately.
    """
    started = time.monotonic()
    delay = 1.0
    attempt = 0
    while True:
        attempt += 1
        attempt_started = time.monotonic()
        _server_log.info('Recovery step start label=%s attempt=%d', label, attempt)
        try:
            result = step()
            _server_log.info(
                'Recovery step done label=%s attempt=%d elapsed_s=%.3f',
                label, attempt, time.monotonic() - attempt_started)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            retryable = getattr(exc, 'retryable', None) is True
            remaining = deadline_s - elapsed
            if not retryable or remaining <= 0.0:
                _server_log.error(
                    'Readiness step %s failed after %.3fs (attempt %d, '
                    'code=%s, retryable=%s): %s',
                    label, elapsed, attempt, getattr(exc, 'code', type(exc).__name__),
                    retryable, getattr(exc, 'message', str(exc)))
                raise
            wait = min(delay, remaining)
            _server_log.info(
                'Readiness step %s hit a transient storage failure '
                '(attempt %d, %s) — retrying in %.1fs (%.0fs budget left)',
                label, attempt, exc, wait, remaining)
            time.sleep(wait)
            delay = min(delay * 2.0, 15.0)


def _start_storage_authority_sql():
    """Start and verify the required storage authority."""
    from lib.storage import start_storage, storage_status

    # Direct/container starts and a re-exec from an older updater must also
    # prepare the current engine. Keep this in the visible boot phase, never
    # on an ordinary query's storage acquisition path.
    from serverctl import prepare_native_engine_artifact
    preparation_error = prepare_native_engine_artifact('storage startup', project=_PROJECT_ROOT)
    if preparation_error:
        raise RuntimeError(preparation_error)
    _boot('Starting storage sidecar…')
    # The runtime start performs the authoritative ready handshake and
    # raises on a failed one; a third explicit probe here was a redundant
    # RPC on the boot critical path. The in-memory snapshot carries the
    # same truth for the log line without another round trip.
    status = None
    for attempt in range(1, _SIDECAR_START_ATTEMPTS + 1):
        try:
            start_storage()
            status = storage_status()
            if not status.get('ready'):
                raise RuntimeError('storage sidecar did not report ready')
        except Exception as exc:
            # Integrity/configuration failures cannot improve by replaying the
            # same files. Preserve unknown legacy failures for lease handoff.
            if getattr(exc, 'retryable', None) is False or attempt >= _SIDECAR_START_ATTEMPTS:
                raise
            delay = _SIDECAR_START_BACKOFF_S[
                min(attempt - 1, len(_SIDECAR_START_BACKOFF_S) - 1)]
            _server_log.warning(
                '[Storage] sidecar start attempt %d/%d failed: %s — '
                'retrying in %.0fs',
                attempt, _SIDECAR_START_ATTEMPTS, exc, delay)
            time.sleep(delay)
        else:
            break
    _server_log.info(
        '[Storage] required sidecar ready backend=%s pid=%s',
        status.get('backend', 'unknown'), status.get('pid', 'unknown'))
    _boot('Storage sidecar ready.')


def _validate_storage_cutover_boundary():
    """Fail before lease acquisition while any legacy DB owner remains."""
    from pathlib import Path
    from lib.storage.storage_boundary import require_exclusive_sidecar_boundary

    _boot('Validating exclusive storage boundary…')
    require_exclusive_sidecar_boundary(Path(_PROJECT_ROOT))
    _boot('Exclusive storage boundary ready.')


def _run_boot_recovery_step(label, step, *, attempts=4):
    """Run ONE boot recovery step with bounded backoff, isolated from the rest.

    The sidecar can answer its ready probe and still be unable to finish a
    storage command within the deadline for a few seconds (cold page cache on
    the network FS, writer-lane warmup). A single transient failure must not
    skip restart settlement for the whole process lifetime — one shared
    try/except here is exactly how the 2026-08-19 zombie turns were born
    ("侧边栏好多回答中 + badge 永远重连中"): recovery died 5s after
    sidecar-ready and nothing ever re-ran it.
    """
    delay = 2.0
    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        _server_log.info('Recovery step start label=%s attempt=%d', label, attempt)
        try:
            result = step()
            _server_log.info(
                'Recovery step done label=%s attempt=%d elapsed_s=%.3f',
                label, attempt, time.monotonic() - attempt_started)
            return result
        except Exception as exc:
            # Protocol/schema/programming failures do not heal by sleeping.
            # Retrying one six times used to add 49.5s to every restart.
            if getattr(exc, 'retryable', None) is not True or attempt >= attempts:
                _server_log.warning(
                    'Sidecar %s failed after %d attempts: %s (%s)',
                    label, attempt, exc, getattr(exc, 'message', str(exc)))
                return
            _server_log.info(
                'Sidecar %s attempt %d/%d failed: %s — retrying in %.0fs',
                label, attempt, attempts, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2.5, 15.0)


def _init_database():
    """Recover all process-local services after sidecar storage is ready."""
    from lib.process_roles import CAPABILITY_TASK_RECOVERY, process_role_has

    _boot('Storage authority ready; recovering process-local state.')
    if _DEPLOYMENT_CONFIGURATION.distributed_preview_read_only:
        _server_log.info(
            '[Server] task recovery disabled by distributed read-only '
            'preview fence (role=%s)',
            _DEPLOYMENT_CONFIGURATION.process_role,
        )
        _start_log_aggregate_runtime_after_recovery()
        return

    # Project Brain has no compatibility runtime. Readiness on the authority
    # owner stays closed until the backup-backed cutover verifies. A declared
    # distributed read-only preview must never attempt the mutating migration.
    from lib.conversations.project_brain_startup import (
        ensure_project_brain_cutover,
    )
    _run_readiness_critical_storage_step(
        'project-brain cutover', ensure_project_brain_cutover)
    if not process_role_has(
            _DEPLOYMENT_CONFIGURATION.process_role, CAPABILITY_TASK_RECOVERY):
        _server_log.info(
            '[Server] task recovery is not owned by process role=%s',
            _DEPLOYMENT_CONFIGURATION.process_role,
        )
        _start_log_aggregate_runtime_after_recovery()
        return

    previous_shutdown = None
    try:
        from lib.shutdown_marker import report_and_arm
        previous_shutdown = report_and_arm()
    except Exception as exc:
        _server_log.warning('Shutdown-marker classification failed: %s', exc)

    from lib.tasks_pkg.manager import (
        record_boot_interrupted_conversations,
        recover_stale_tasks_on_startup,
    )
    # Durable settlement is not optional enrichment: do not sleep on a
    # protocol fault or publish readiness after silently abandoning orphans.
    recovery_result = _run_readiness_critical_storage_step(
        'task and turn recovery',
        lambda: recover_stale_tasks_on_startup(
            prev_shutdown=previous_shutdown),
    )
    # Publish the just-settled conversation list as a boot-scoped hint so the
    # browser can offer batch resume. A failed recovery (None) keeps the old
    # record; an empty recovery clears it (see _pending_resume.py).
    _run_boot_recovery_step(
        'pending-resume record',
        lambda: record_boot_interrupted_conversations(recovery_result),
        attempts=2,
    )
    # Durable background commands: deliver finished results, reattach to
    # surviving processes, report ones that died with the server.
    from lib.tasks_pkg.background_commands import (
        recover_background_commands_on_startup,
    )
    _run_boot_recovery_step(
        'background command recovery',
        lambda: recover_background_commands_on_startup(),
        attempts=2,
    )
    from lib.conversations.project_brain_startup import recover_active_work_items
    # Unlike reconstructible enrichment, ownership recovery is readiness
    # critical: an orphaned active item must never look claimable after boot.
    _run_readiness_critical_storage_step(
        'active work-item recovery', recover_active_work_items)

    # Durable orchestration headers cannot keep running after their executor
    # threads die with the old process. Settle them before clients reconnect.
    # The retired-run descriptors are handed to the post-ready continuity
    # pass, which resumes the auto-policy ones without human action
    # (docs/RUN_CONTINUITY.md D2).
    try:
        from lib.orchestration.startup_recovery import (
            retire_interrupted_orchestration_runs_detailed,
        )
        retirement = retire_interrupted_orchestration_runs_detailed(
            error={
                'kind': 'worker_lost',
                'message': (
                    'Run interrupted by a server restart before completion.'),
                'source': 'orchestration.startup_recovery',
            },
        )
        set_boot_orchestration_continuity_candidates(retirement.candidates)
        if retirement.retired:
            _server_log.warning(
                'Retired %d interrupted orchestration run(s)',
                retirement.retired)
    except Exception as exc:
        _server_log.warning('Orchestration run recovery failed: %s', exc)

    # Presence is process-local live state. A restart begins empty by design;
    # the first newly running task starts its batch-scoped TTL owner.

    _start_log_aggregate_runtime_after_recovery()


def _run_post_ready_deferred():
    """Best-effort recovery that must not hold the readiness gate.

    These steps are idempotent and reconstructible: cache warm-up,
    superseded-attempt cleanup, swarm rehydration and output housekeeping.
    Required task/turn and work-item settlement stays in _init_database.
    Wired as ``ProductionStartupSteps.post_ready`` by the composition root.
    """
    try:
        from lib.storage.warmup import start_keep_warm
        # The existing lifecycle-owned keeper runs the initial pass off the
        # readiness path, including when periodic warming is disabled.
        start_keep_warm(initial_pass=True)
    except Exception as exc:
        _server_log.warning('[Server] storage warm-up aborted: %s', exc)
    from lib.turn_lifecycle import cleanup_superseded_attempts
    _run_boot_recovery_step(
        'superseded-attempt cleanup', cleanup_superseded_attempts)
    try:
        from lib.swarm.integration import rehydrate_swarms_on_startup
        rehydrate_swarms_on_startup()
    except Exception as exc:
        _server_log.warning('Swarm rehydration failed: %s', exc)
    try:
        from lib.swarm.integration import start_swarm_output_cleanup
        start_swarm_output_cleanup()
    except Exception as exc:
        _server_log.warning('Swarm output cleanup failed to start: %s', exc)
