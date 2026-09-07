"""routes/ — Quart blueprints for each domain.

Each module is self-contained and registers its own routes.
Shared helpers live in focused ``lib/`` service packages. Durable reads and
writes go through semantic ``lib.storage`` clients; routes never own SQL.

Optional feature bundles (e.g. the trading subsystem, now the standalone
``tofu-trading`` package) are NOT imported here — they mount via the
``tofu.blueprints`` / ``tofu.startup`` entry-point groups discovered in
``register_all`` (see ``routes/plugin_registry.py``).
"""

from .browser import browser_bp
# Side-effect imports: each registers focused controls on api_v1_chat_bp.
from . import chat_queue  # noqa: F401  — /api/chat/queue/*
from . import chat_human_io  # noqa: F401  — /api/chat/{stdin,human}_response
from . import chat_tool_state  # noqa: F401  — /api/chat/tool-state/<id>
from . import chat_poll_abort  # noqa: F401  — abort/interrupt/flow-trace
from . import conversations_search  # noqa: F401  — /api/conversations/search
from . import conversations_compaction  # noqa: F401  — /api/conversations/<id>/compactions[/<id>]

from .common import common_bp
from . import config  # noqa: F401  — registers routes on api_v1_config_bp
from . import conversations  # noqa: F401  — registers routes on api_v1_conversations_bp
from .desktop import desktop_bp
from .oauth import oauth_bp
from .translate import translate_bp
from .upload import upload_bp
from .artifacts import artifacts_bp
from .paper import paper_bp
from .push import push_bp

# ── Headless API surface ──
# Native v1 (/api/v1/*), OpenAI compat (/v1/chat/completions, /v1/models,
# /v1/embeddings), Anthropic compat (/v1/messages), and OpenAPI viewers
# (/api/openapi.json, /api/docs, /api/redoc).
from .api_v1 import ALL_V1_BLUEPRINTS
from .compat_openai import compat_openai_bp
from .compat_anthropic import compat_anthropic_bp
from .api_docs import api_docs_bp
from .api_v4 import api_v4_bp
from .metrics import metrics_bp
from .conversation_sync_v3 import conversation_sync_v3_bp

# ── Core (always-on) blueprints ──
ALL_BLUEPRINTS = [
    common_bp,
    upload_bp,
    translate_bp,
    browser_bp,
    desktop_bp,
    oauth_bp,
    paper_bp,
    artifacts_bp,
    push_bp,
    # Headless API:
    *ALL_V1_BLUEPRINTS,
    compat_openai_bp,
    compat_anthropic_bp,
    api_docs_bp,
    api_v4_bp,
    metrics_bp,
    conversation_sync_v3_bp,
]


def start_registered_background_services(app, *, process_role='all'):
    """Start route-owned schedulers/plugin workers once for a serving app.

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
                'route background services already belong to process role '
                f'{registered_role!r}, not {process_role!r}')
        return 0
    # Latch before invoking plugins: a hook can indirectly re-enter app setup.
    extensions[marker] = True
    extensions['tofu_registered_background_services_role'] = process_role
    started = 0

    # ── Start the sole durable scheduler (including daily-report backfill) ──
    if process_role_has(process_role, CAPABILITY_SCHEDULED_JOBS):
        try:
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
            start_scheduler_worker(principal=scheduler_principal)
            started += 1
        except Exception as e:
            _log.warning(
                'Scheduler worker start deferred (DB unavailable): %s', e)

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
    if process_role_has(process_role, CAPABILITY_TASK_WORKERS):
        try:
            from .plugin_registry import run_startup_hooks
            started += int(run_startup_hooks(app) or 0)
        except Exception as e:
            _log.warning('Plugin startup hooks deferred: %s', e)
    return started


def stop_registered_background_services(app, *, timeout: float = 2.0) -> int:
    """Stop route/plugin-owned workers with bounded, idempotent joins."""
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
            from .plugin_registry import run_shutdown_hooks
            stopped += int(run_shutdown_hooks(app) or 0)
        except Exception as exc:
            all_stopped = False
            log.warning('Plugin shutdown hooks failed: %s', exc)

    owners = (
        ('knowledge visual enrichment',
         'lib.knowledge.enrichment', 'stop_visual_enrichment',
         CAPABILITY_TASK_WORKERS),
        ('Codex model catalogue',
         'lib.oauth.codex_catalog', 'stop_codex_catalog_refresher',
         CAPABILITY_REQUEST_SERVICES),
        ('proactive scheduler',
         'lib.scheduler.manager', 'stop_scheduler_worker',
         CAPABILITY_SCHEDULED_JOBS),
    )
    import importlib
    for label, module_name, stop_name, capability in owners:
        if not process_role_has(process_role, capability):
            continue
        try:
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

    # A timed-out owner remains live. Preserve the latch so a reused app cannot
    # launch duplicate workers on top of it; a clean stop permits restart.
    extensions[marker] = not all_stopped
    if all_stopped:
        extensions.pop('tofu_registered_background_services_role', None)
    return stopped


def register_all(app, *, start_workers=True, discover_plugins=True):
    """Register all blueprints; optionally start route-owned workers.

    ``start_workers=True`` preserves the historical embedder API.  Core's
    ``server.py`` passes ``False`` at import time and starts the workers from
    its real serving lifecycle after the database is ready.
    """
    import logging
    _log = logging.getLogger(__name__)

    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)

    # ── Plugin blueprints (tofu.blueprints entry-point group) ──
    # External feature packages (e.g. the tofu-trading subsystem) mount their
    # Blueprints here. Discovery is fail-soft and returns [] when no plugin is
    # installed, so this is a no-op for a vanilla core install. The name guard
    # is defensive against a plugin shipping a duplicate blueprint name.
    if discover_plugins:
        from .plugin_registry import discover_blueprint_plugins
        _already = {bp.name for bp in ALL_BLUEPRINTS}
        for bp in discover_blueprint_plugins():
            if bp.name in _already:
                _log.warning('[BlueprintRegistry] plugin blueprint %r already '
                             'registered in-tree — skipping', bp.name)
                continue
            app.register_blueprint(bp)
            _already.add(bp.name)

    if start_workers:
        start_registered_background_services(app)
