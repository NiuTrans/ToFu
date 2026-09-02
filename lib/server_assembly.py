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
_tofu_do_mlock = False
_PROJECT_ROOT = None
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
    _CRITICAL_IMPORTS = [
        'lib.tasks_pkg.orchestrator',
        'lib.tasks_pkg.executor',
        'tofu_search.fetch',
        'tofu_search.search',
        'lib.search_bridge',
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


def _start_storage_sidecar():
    """Start and verify the required storage authority."""
    from lib.storage import start_storage

    _boot('Starting storage sidecar…')
    client = start_storage()
    health = client.health(deadline=2.0)
    if not health.get('ready'):
        raise RuntimeError('storage sidecar did not report ready')
    _server_log.info(
        '[Storage] required sidecar ready backend=%s protocol=%s',
        health.get('backend', 'unknown'), health.get('protocol', 'unknown'))
    _boot('Storage sidecar ready.')


def _validate_storage_cutover_boundary():
    """Fail before lease acquisition while any legacy DB owner remains."""
    from pathlib import Path
    from lib.storage_boundary import require_exclusive_sidecar_boundary

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
        try:
            step()
            return
        except Exception as exc:
            if attempt >= attempts:
                _server_log.warning(
                    'Sidecar %s failed after %d attempts: %s',
                    label, attempts, exc)
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

    from lib.turn_lifecycle import cleanup_superseded_attempts
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
    _run_boot_recovery_step(
        'task and turn recovery',
        lambda: recover_stale_tasks_on_startup(
            prev_shutdown=previous_shutdown),
    )
    _run_boot_recovery_step(
        'superseded-attempt cleanup', cleanup_superseded_attempts)

    # Durable orchestration headers cannot keep running after their executor
    # threads die with the old process. Settle them before clients reconnect.
    try:
        from lib.orchestration.startup_recovery import (
            retire_interrupted_orchestration_runs,
        )
        retired_runs = retire_interrupted_orchestration_runs(
            error={
                'kind': 'worker_lost',
                'message': (
                    'Run interrupted by a server restart before completion.'),
                'source': 'orchestration.startup_recovery',
            },
        )
        if retired_runs:
            _server_log.warning(
                'Retired %d interrupted orchestration run(s)', retired_runs)
    except Exception as exc:
        _server_log.warning('Orchestration run recovery failed: %s', exc)

    # Presence is process-local live state. A restart begins empty by design;
    # the first newly running task starts its batch-scoped TTL owner.

    try:
        from lib.swarm.integration import rehydrate_swarms_on_startup
        rehydrate_swarms_on_startup()
    except Exception as exc:
        _server_log.warning('Swarm rehydration failed: %s', exc)

    _start_log_aggregate_runtime_after_recovery()
