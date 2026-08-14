"""routes/ — Quart blueprints for each domain.

Each module is self-contained and registers its own routes.
Shared helpers: lib/database/, lib/llm/ (package), lib/__init__.py (config).

Optional feature bundles (e.g. the trading subsystem, now the standalone
``tofu-trading`` package) are NOT imported here — they mount via the
``tofu.blueprints`` / ``tofu.startup`` entry-point groups discovered in
``register_all`` (see ``routes/plugin_registry.py``).
"""

from .browser import browser_bp
from .chat import chat_bp
# Side-effect imports: each registers additional routes on chat_bp.
from . import chat_queue  # noqa: F401  — /api/chat/queue/*
from . import chat_human_io  # noqa: F401  — /api/chat/{stdin,human}_response
from . import chat_tool_state  # noqa: F401  — /api/chat/tool-state/<id>
from . import chat_poll_abort  # noqa: F401  — poll/abort/flow-trace (pt_04686ac6 slice 10)
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
from .metrics import metrics_bp
from .turns_v2 import turns_v2_bp
from .legacy_redirects import legacy_redirects_bp

# ── Core (always-on) blueprints ──
ALL_BLUEPRINTS = [
    common_bp,
    upload_bp,
    translate_bp,
    chat_bp,
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
    metrics_bp,
    turns_v2_bp,
    legacy_redirects_bp,
]


def start_registered_background_services(app):
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
    _log = logging.getLogger(__name__)
    extensions = getattr(app, 'extensions', None)
    if extensions is None:
        extensions = {}
        app.extensions = extensions
    marker = 'tofu_registered_background_services'
    if extensions.get(marker):
        return 0
    # Latch before invoking plugins: a hook can indirectly re-enter app setup.
    extensions[marker] = True
    started = 0

    # ── Start daily report background scheduler ──
    try:
        from lib.daily_report import start_report_scheduler
        start_report_scheduler()
        started += 1
    except Exception as e:
        _log.warning('Daily report scheduler start deferred (DB unavailable): %s', e)

    # ── Start proactive agent / cron scheduler ──
    try:
        from lib.scheduler import start_scheduler_worker
        start_scheduler_worker()
        started += 1
    except Exception as e:
        _log.warning('Scheduler worker start deferred (DB unavailable): %s', e)

    # ── Refresh the authenticated Codex `/model` catalogue ──
    try:
        from lib.oauth.codex_catalog import start_codex_catalog_refresher
        start_codex_catalog_refresher()
        started += 1
    except Exception as e:
        _log.warning('Codex model catalogue refresher start deferred: %s', e)

    # ── Reconcile ordinary API-provider /models catalogues ──
    try:
        from lib.llm_dispatch.model_catalog_sync import start_model_catalog_sync
        start_model_catalog_sync()
        started += 1
    except Exception as e:
        _log.warning('Provider model catalogue sync start deferred: %s', e)

    # ── Resume consented local-knowledge image descriptions ──
    try:
        from lib.knowledge.enrichment import start_visual_enrichment
        if start_visual_enrichment():
            started += 1
    except Exception as e:
        _log.warning('Knowledge visual enrichment start deferred: %s', e)

    # ── Plugin startup hooks (tofu.startup entry-point group) ──
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

    stopped = 0
    all_stopped = True
    try:
        # Plugins are started last and may depend on core schedulers/catalogues,
        # so their teardown runs first.
        from .plugin_registry import run_shutdown_hooks
        stopped += int(run_shutdown_hooks(app) or 0)
    except Exception as exc:
        all_stopped = False
        log.warning('Plugin shutdown hooks failed: %s', exc)

    owners = (
        ('knowledge visual enrichment',
         'lib.knowledge.enrichment', 'stop_visual_enrichment'),
        ('provider model catalogue',
         'lib.llm_dispatch.model_catalog_sync', 'stop_model_catalog_sync'),
        ('Codex model catalogue',
         'lib.oauth.codex_catalog', 'stop_codex_catalog_refresher'),
        ('proactive scheduler',
         'lib.scheduler', 'stop_scheduler_worker'),
        ('daily report scheduler',
         'lib.daily_report', 'stop_report_scheduler'),
    )
    import importlib
    for label, module_name, stop_name in owners:
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
    return stopped


def register_all(app, *, start_workers=True):
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
