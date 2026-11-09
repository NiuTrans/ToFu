"""Lifecycle-owned background services and crash-resume sweeps."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


_LAN_DISCOVERY_LOCK = threading.Lock()
_LAN_DISCOVERY_RESPONDER: Any | None = None


def start_lan_discovery_responder(
    port: int,
    *,
    bind_host: str,
    environ: Mapping[str, str],
) -> Any | None:
    """Start or reuse the one LAN responder owned by this request process."""
    global _LAN_DISCOVERY_RESPONDER
    from lib.desktop.pairing import maybe_start_responder

    with _LAN_DISCOVERY_LOCK:
        current = _LAN_DISCOVERY_RESPONDER
        if current is not None and current.is_running():
            return current
        responder = maybe_start_responder(
            port, environ=environ, bind_host=bind_host)
        if responder is not None:
            _LAN_DISCOVERY_RESPONDER = responder
        return responder


def stop_lan_discovery_responder(timeout: float = 2.0) -> bool:
    """Stop the exact process-owned LAN responder without duplicating it."""
    global _LAN_DISCOVERY_RESPONDER
    with _LAN_DISCOVERY_LOCK:
        responder = _LAN_DISCOVERY_RESPONDER
    if responder is None:
        return True
    stopped = responder.stop(timeout=timeout)
    if stopped:
        with _LAN_DISCOVERY_LOCK:
            if _LAN_DISCOVERY_RESPONDER is responder:
                _LAN_DISCOVERY_RESPONDER = None
    return stopped


def _timed(log: logging.Logger, label: str, fn: Callable[[], Any]) -> Any:
    # Per-step boot timing: startup phase 7 reports one aggregate, so a
    # slow sweep/import hides inside it. Fail-soft timing only.
    started = time.monotonic()
    try:
        return fn()
    finally:
        log.info('[Server] background-services step %s took %.1fs',
                 label, time.monotonic() - started)


def start_background_services(
    app: Any,
    *,
    process_role: str = 'all',
    load_saved_proxy_config: Callable[[], Any],
    bootstrap_personal_key: Callable[[], Any],
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Start the READINESS-CRITICAL lifecycle-owned workers.

    Imports stay inside this function so importing the HTTP application cannot
    launch threads, touch persisted job state or probe the network. The caller
    runs this function off the event loop and does not announce readiness until
    every owner has either started or reported its best-effort failure.

    Only owners a request can observe within seconds of the gate stay on this
    path: role-scoped schedulers, proxy configuration, the first-boot key and
    the integration worker. Best-effort resume sweeps and probes live in
    :func:`start_deferred_background_services`, which runs after the readiness
    gate — under storage-authority starvation each sweep re-pays the full
    authority round trip, and serializing them ahead of the gate turned a
    healthy ~4s phase 7 into 147-190s (2026-09-22 boot profile).
    """
    from lib.process_roles import (
        CAPABILITY_NETWORK_CONFIGURATION,
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
    )

    log = logger or logging.getLogger('server')
    env = os.environ if environ is None else environ
    process_role = normalize_process_role(process_role)
    from runtime_guards import distributed_preview_is_read_only

    # The local-kernel launch fact (app.extensions['tofu_local_kernel']) must
    # precede any turn's tool exposure; recording it probes nothing. The
    # environment probe itself runs after readiness (deferred services).
    from lib.tools.environment_capabilities import (
        record_environment_launch_facts)

    record_environment_launch_facts(app)

    if distributed_preview_is_read_only(env):
        log.info(
            '[Server] background services disabled by distributed read-only '
            'preview fence (role=%s)', process_role)
        return
    owns_network_configuration = process_role_has(
        process_role, CAPABILITY_NETWORK_CONFIGURATION)
    owns_request_services = process_role_has(
        process_role, CAPABILITY_REQUEST_SERVICES)
    owns_task_workers = process_role_has(
        process_role, CAPABILITY_TASK_WORKERS)

    try:
        _timed(
            log,
            'process-services',
            lambda: start_registered_background_services(
                app, process_role=process_role))
    except Exception as exc:
        log.warning('[Server] process background services failed: %s', exc)

    # Turn-search materialization is owned by the Storage Sidecar. Starting a
    # second web-process worker would put historical repair back on the
    # authority command lane and duplicate work across API replicas.

    # Proxy state must be installed before the first network probe, and the
    # first-boot key requires the initialized system database.
    if owns_network_configuration:
        load_saved_proxy_config()
    if owns_request_services:
        bootstrap_personal_key()
        from lib.tasks_pkg.command_control_runtime import configure
        configure(app, process_role=process_role, environ=env)
        from lib.conversation_sync.delivery_runtime import ensure_started
        _timed(log, 'conversation-delivery', lambda: ensure_started(app))

    if owns_task_workers:
        try:
            from lib.durable_worker.runtime import ensure_workers_started

            if _timed(log, 'durable-workers', lambda: ensure_workers_started(app, environ=env)):
                log.info('[Server] canonical conversation and automation workers armed')
        except Exception as exc:
            log.warning('[Server] durable worker start failed: %s', exc)

        try:
            from lib.integration_control import ensure_worker_started

            if ensure_worker_started():
                log.info('[Server] deterministic integration worker armed')
        except Exception as exc:
            log.warning('[Server] integration worker start failed: %s', exc)


def start_deferred_background_services(
    app: Any,
    *,
    process_role: str = 'all',
    logger: logging.Logger | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run best-effort sweeps/probes AFTER the readiness gate opens.

    Every step here is idempotent, fail-soft and invisible to the user within
    the first seconds after ready: the environment-capability probe, resuming
    interrupted productions, marking interrupted podcasts, the network-path
    prober and the LAN discovery responder. They were moved off the readiness
    path because none of them gates a request, and each storage-touching
    sweep re-pays the full authority round trip while the authority is
    starved.
    """
    from lib.process_roles import (
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
    )

    log = logger or logging.getLogger('server')
    env = os.environ if environ is None else environ
    process_role = normalize_process_role(process_role)
    from runtime_guards import distributed_preview_is_read_only

    if distributed_preview_is_read_only(env):
        log.info(
            '[Server] deferred background services disabled by distributed '
            'read-only preview fence (role=%s)', process_role)
        return
    owns_request_services = process_role_has(
        process_role, CAPABILITY_REQUEST_SERVICES)
    owns_task_workers = process_role_has(
        process_role, CAPABILITY_TASK_WORKERS)

    # This boot's environment-capability probe (presence checks only: PATH
    # lookups, a browser on disk). A turn that starts first takes it instead;
    # either way it runs once until a refresh event.
    try:
        from lib.tools.environment_capabilities import environment_snapshot

        _timed(log, 'environment-snapshot', environment_snapshot)
    except Exception as exc:
        log.warning('[Server] environment-capability probe failed: %s', exc)

    # Outbound route probes are demand-driven and shared by the HTTP layer.
    # Do not start the retired direct-versus-environment polling worker.

    if owns_task_workers:
        try:
            from lib.motion_video.engine import resume_interrupted_jobs

            resumed = _timed(log, 'motion-resume', resume_interrupted_jobs)
            if resumed:
                log.info(
                    '[Server] resumed %d interrupted motion job(s)', resumed)
        except Exception as exc:
            log.warning('[Server] motion job resume failed: %s', exc)

        try:
            from lib.research.api import resume_interrupted_research

            resumed = _timed(log, 'research-resume', resume_interrupted_research)
            if resumed:
                log.info(
                    '[Server] resumed %d interrupted research job(s)', resumed)
        except Exception as exc:
            log.warning('[Server] research job resume failed: %s', exc)

        try:
            from lib.research.action_startup import resume_interrupted_actions

            resumed = _timed(log, 'research-action-resume', lambda: resume_interrupted_actions(
                stop_event=app.extensions.get('tofu_shutdown_requested')))
            if resumed:
                log.info('[Server] resumed %d interrupted Research action(s)', resumed)
        except Exception as exc:
            log.warning('[Server] Research action startup resume failed: %s', exc)

        try:
            from lib.slides.api import resume_interrupted_decks

            resumed = _timed(log, 'slides-resume', resume_interrupted_decks)
            if resumed:
                log.info(
                    '[Server] resumed %d interrupted slides job(s)', resumed)
        except Exception as exc:
            log.warning('[Server] slides job resume failed: %s', exc)

        try:
            from lib.paper.podcast_engine.worker import mark_interrupted_podcasts

            _timed(log, 'podcast-sweep', mark_interrupted_podcasts)
        except Exception as exc:
            log.warning('[Server] podcast interrupted sweep failed: %s', exc)

    if owns_request_services:
        try:
            responder = start_lan_discovery_responder(
                int(env.get('_TOFU_RUNTIME_PORT') or '15000'),
                environ=env,
                bind_host=env.get('_TOFU_RUNTIME_HOST') or '',
            )
            if responder is not None:
                log.info(
                    '[Server] LAN discovery responder up on UDP 15001 (%s)',
                    responder.url,
                )
        except Exception as exc:
            log.warning('[Server] LAN discovery responder failed: %s', exc)


def start_registered_background_services(app, *, process_role='all'):
    """Start role-scoped schedulers/plugin workers once for a serving app.

    Blueprint registration is intentionally import-safe.  Tests, desktop
    smoke checks and WSGI/ASGI tooling all import ``server.app`` merely to
    inspect routes; starting production workers in that import path caused
    real network/DB work to outlive the importing process.  The real server
    calls this only after database initialisation from its startup lifecycle.
    ``app.extensions`` supplies a per-app idempotence latch so an embedder that
    explicitly starts services twice cannot duplicate scheduler threads.
    """
    import logging
    from lib.process_roles import (
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_SCHEDULED_JOBS,
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
    )

    _log = logging.getLogger(__name__)
    process_role = normalize_process_role(process_role)
    extensions = getattr(app, 'extensions', None)
    if extensions is None:
        extensions = {}
        app.extensions = extensions
    marker = 'tofu_registered_background_services'
    if extensions.get(marker):
        registered_role = extensions.get(
            'tofu_registered_background_services_role', 'all')
        if registered_role != process_role:
            raise RuntimeError(
                'background services already belong to process role '
                f'{registered_role!r}, not {process_role!r}')
        return 0
    # Latch before invoking plugins: a hook can indirectly re-enter app setup.
    extensions[marker] = True
    extensions['tofu_registered_background_services_role'] = process_role
    started = 0

    # The split API/execution hosts share one browser bridge: the API owns
    # extension polls, the execution host relays its queue verbs there.
    try:
        from lib.browser.bridge_relay import configure_bridge_owner
        if configure_bridge_owner(process_role=process_role, environ=os.environ) == 'owner':
            started += 1
    except Exception as e:
        _log.warning('Browser bridge relay unavailable for role %s: %s', process_role, e)

    if process_role_has(process_role, CAPABILITY_TASK_WORKERS):
        from lib.evolution.automation import start_terminal_checks
        start_terminal_checks()  # Lazy: no thread until a committed bound task.

    # ── Start the sole durable scheduler (including daily-report backfill) ──
    if process_role_has(process_role, CAPABILITY_SCHEDULED_JOBS):
        try:
            from lib.channels.dx.supervisor import start_channel_bridges
            from lib.scheduler.manager import start_scheduler_worker
            from lib.identity import PERSONAL_USER_ID, PrincipalContext
            from runtime_guards import load_deployment_configuration

            deployment = load_deployment_configuration()
            scheduler_principal = PrincipalContext.system(
                subject_id='scheduler-worker',
                owner_user_id=(
                    PERSONAL_USER_ID
                    if deployment.mode == 'personal'
                    else None
                ),
                scopes={'scheduler:run'},
            )
            start_channel_bridges()
            start_scheduler_worker(principal=scheduler_principal)
            started += 1
        except Exception as e:
            _log.warning(
                'Scheduler worker start deferred (DB unavailable): %s', e)

        # Settings → 数据库 daily dual-path backup.
        try:
            from lib.database_panel.maintenance import start_maintenance

            if start_maintenance():
                started += 1
        except Exception as e:
            _log.warning('Database panel maintenance start failed: %s', e)

    # ── Catalogue projections (OAuth reconcile + world model catalogue) ──
    # Both syncs are idempotent, insert-or-prune projections consumed only by
    # post-readiness request paths (model catalog UI, provider setup), so
    # they no longer sit on the time-to-ready critical path: compiling every
    # provider template bundle and re-evaluating pricing tags cost ~2s of
    # every boot. They run once the readiness gate opens; without a serving
    # gate (embedders, tests) they run inline exactly as before.
    start_post_ready_catalog_sync(app, process_role=process_role)

    # ── Refresh the authenticated Codex `/model` catalogue ──
    if process_role_has(process_role, CAPABILITY_REQUEST_SERVICES):
        try:
            from lib.oauth.codex_catalog import start_codex_catalog_refresher
            from runtime_guards import load_deployment_configuration

            deployment = load_deployment_configuration()
            if deployment.mode == 'personal':
                if start_codex_catalog_refresher():
                    started += 1
            else:
                # TODO(enterprise): enumerate account owners through an
                # owner-scoped OAuth/catalog repository before enabling this
                # personal-token worker in distributed deployments.
                _log.info(
                    'Codex model catalogue refresher disabled in distributed '
                    'mode: no owner-scoped catalogue authority is configured')
        except Exception as e:
            _log.warning(
                'Codex model catalogue refresher start deferred: %s', e)

    # Resume only corpora whose durable owner settings explicitly opted in.
    if process_role_has(process_role, CAPABILITY_TASK_WORKERS):
        try:
            from lib.knowledge.enrichment import resume_visual_enrichment
            from lib.identity import PrincipalContext

            knowledge_principal = PrincipalContext.system(
                subject_id='knowledge-enrichment-worker',
                scopes={'knowledge:maintain'},
            )
            started += int(resume_visual_enrichment(
                principal=knowledge_principal) or 0)
        except Exception as e:
            _log.warning('Knowledge visual enrichment start deferred: %s', e)

    # ── Plugin startup hooks (tofu.startup entry-point group) ──
    started += _run_or_schedule_plugin_startup_hooks(
        app, process_role=process_role)
    return started


def _run_or_schedule_plugin_startup_hooks(app, *, process_role: str) -> int:
    """Run ``tofu.startup`` plugin hooks; post-gate when a gate exists.

    Plugin workers idle behind their own feature flag — nothing they do at
    boot gates a request. Under storage-authority starvation their boot-time
    state restores re-pay the full authority round trip (2026-09-22 boot:
    two ``plugin.tofu.trading.list_rows`` calls cost ~60s of phase 7 while
    the feature was OFF). With a production readiness gate the hooks run
    once the gate opens; without one (embedders, tests) they run inline
    exactly as before.
    """
    import logging

    from lib.process_roles import (
        CAPABILITY_TASK_WORKERS,
        normalize_process_role,
        process_role_has,
    )

    _log = logging.getLogger(__name__)
    if not process_role_has(
            normalize_process_role(process_role), CAPABILITY_TASK_WORKERS):
        return 0
    from lib.app.plugin_registry import run_startup_hooks

    if _schedule_post_ready(
            app, lambda: run_startup_hooks(app),
            name='tofu-post-ready-plugin-startup-hooks'):
        return 0
    try:
        return int(run_startup_hooks(app) or 0)
    except Exception as exc:
        _log.warning('Plugin startup hooks deferred: %s', exc)
        return 0


def _schedule_post_ready(app, callback: Callable[[], Any], *, name: str) -> bool:
    """Schedule on the serving owner; callers retain explicit inline behavior."""
    import asyncio

    extensions = getattr(app, 'extensions', None) or {}
    gate = extensions.get('tofu_production_startup_gate')
    stop_event = extensions.get('tofu_shutdown_requested')
    serving = extensions.get('tofu_serving_loop_lifecycle') or {}
    runtime = serving.get('runtime')
    if gate is None or runtime is None:
        return False

    async def run() -> None:
        await gate.wait()
        if stop_event is not None and stop_event.is_set():
            return
        try:
            await asyncio.to_thread(callback)
        except Exception as exc:
            logging.getLogger(__name__).warning('%s failed: %s', name, exc)

    runtime.create_task(run(), name=name)
    return True


def start_post_ready_catalog_sync(app, *, process_role='all') -> bool:
    """Run the catalogue projections after the readiness gate opens.

    Returns True when the work was scheduled on the serving loop; False when
    it ran inline (no production gate — embedders and direct test calls keep
    the historical synchronous semantics).
    """
    import logging
    from lib.process_roles import (
        CAPABILITY_REQUEST_SERVICES,
        normalize_process_role,
        process_role_has,
    )

    _log = logging.getLogger(__name__)
    process_role = normalize_process_role(process_role)
    if not process_role_has(process_role, CAPABILITY_REQUEST_SERVICES):
        return False

    def _run_sync() -> None:
        from runtime_guards import load_deployment_configuration

        deployment = load_deployment_configuration()
        if deployment.mode != 'personal':
            # TODO(enterprise): enumerate account owners through owner-scoped
            # OAuth/model-routing repositories before reconciling personal
            # catalogues in distributed deployments.
            _log.info(
                'Catalogue sync disabled in distributed mode: no '
                'owner-scoped OAuth/model-routing authority is configured')
            return
        try:
            # Template/spec updates (e.g. a newly shipped Claude model) must
            # not wait for an OAuth page visit or re-login. Idempotent: an
            # unchanged projection writes nothing; the codex side reads only
            # the local catalogue cache here.
            from lib.oauth.outbound import reconcile_oauth_providers
            reconcile_oauth_providers()
        except Exception as exc:
            _log.warning('OAuth provider reconciliation deferred: %s', exc)
        try:
            # The Model catalog tab lists models that EXIST, not models a
            # configured provider supplies: newly shipped models must not
            # wait for a provider bundle to carry them. Idempotent and
            # insert-only except for template-retired identities, which are
            # pruned — an unchanged catalogue writes nothing.
            from lib.model_routing.world_catalog import (
                sync_world_model_catalog)
            sync_world_model_catalog()
        except Exception as exc:
            _log.warning('World model catalogue sync deferred: %s', exc)

    if _schedule_post_ready(
            app, _run_sync, name='tofu-post-ready-catalog-sync'):
        return True
    _run_sync()
    return False


# The owner whose stop is in progress, so a caller whose own budget ends first
# can name what was still stopping.
STOPPING_OWNER_KEY = 'tofu_background_service_stopping'


def stop_registered_background_services(app, *, timeout: float = 2.0) -> int:
    """Stop process/plugin-owned workers with bounded, idempotent joins."""
    import logging

    log = logging.getLogger(__name__)
    extensions = getattr(app, 'extensions', {})
    marker = 'tofu_registered_background_services'
    if not extensions.get(marker):
        return 0

    from lib.process_roles import (
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_SCHEDULED_JOBS,
        CAPABILITY_TASK_WORKERS,
        process_role_has,
    )

    process_role = extensions.get(
        'tofu_registered_background_services_role', 'all')
    stopped = 0
    all_stopped = True
    if process_role_has(process_role, CAPABILITY_TASK_WORKERS):
        try:
            # Plugins are started last and may depend on core owners, so their
            # teardown runs first.
            extensions[STOPPING_OWNER_KEY] = 'plugin shutdown hooks'
            from lib.app.plugin_registry import run_shutdown_hooks
            stopped += int(run_shutdown_hooks(app) or 0)
        except Exception as exc:
            all_stopped = False
            log.warning('Plugin shutdown hooks failed: %s', exc)

    owners = (
        ('evolution terminal checks',
         'lib.evolution.automation', 'stop_terminal_checks',
         CAPABILITY_TASK_WORKERS),
        ('knowledge visual enrichment',
         'lib.knowledge.enrichment', 'stop_visual_enrichment',
         CAPABILITY_TASK_WORKERS),
        ('knowledge attachment OCR',
         'lib.knowledge.image_ocr_jobs', 'stop_attachment_image_ocr',
         CAPABILITY_TASK_WORKERS),
        ('Codex model catalogue',
         'lib.oauth.codex_catalog', 'stop_codex_catalog_refresher',
         CAPABILITY_REQUEST_SERVICES),
        ('proactive scheduler',
         'lib.scheduler.manager', 'stop_scheduler_worker',
         CAPABILITY_SCHEDULED_JOBS),
        # After the scheduler: its tick is what restarts bridges.
        ('channel bridges',
         'lib.channels.dx.supervisor', 'stop_channel_bridges',
         CAPABILITY_SCHEDULED_JOBS),
        ('GitHub channel sessions',
         'lib.channels.github.supervisor', 'stop_github_sessions',
         CAPABILITY_SCHEDULED_JOBS),
        ('bot channel sessions',
         'lib.channels.bots.supervisor', 'stop_bot_sessions',
         CAPABILITY_SCHEDULED_JOBS),
        ('database panel maintenance',
         'lib.database_panel.maintenance', 'stop_maintenance',
         CAPABILITY_SCHEDULED_JOBS),
    )
    try:
        from lib.browser.bridge_relay import stop_bridge_owner
        if not stop_bridge_owner(timeout=timeout):
            all_stopped = False
            log.warning('browser bridge relay did not stop within %.1fs', timeout)
    except Exception as exc:
        all_stopped = False
        log.warning('browser bridge relay shutdown failed: %s', exc)

    import importlib
    for label, module_name, stop_name, capability in owners:
        if not process_role_has(process_role, capability):
            continue
        try:
            extensions[STOPPING_OWNER_KEY] = label
            module = importlib.import_module(module_name)
            stop = getattr(module, stop_name)
            if stop(timeout=timeout):
                stopped += 1
            else:
                all_stopped = False
                log.warning('%s did not stop within %.1fs', label, timeout)
        except Exception as exc:
            all_stopped = False
            log.warning('%s shutdown failed: %s', label, exc)

    extensions.pop(STOPPING_OWNER_KEY, None)
    # A timed-out owner remains live. Preserve the latch so a reused app cannot
    # launch duplicate workers on top of it; a clean stop permits restart.
    extensions[marker] = not all_stopped
    if all_stopped:
        extensions.pop('tofu_registered_background_services_role', None)
    return stopped


__all__ = [
    'start_registered_background_services',
    'stop_registered_background_services',
    'start_post_ready_catalog_sync',
    'start_background_services',
    'start_deferred_background_services',
    'start_lan_discovery_responder',
    'stop_lan_discovery_responder',
]
