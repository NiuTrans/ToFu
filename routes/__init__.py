"""routes/ — Flask Blueprints for each domain.

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
    legacy_redirects_bp,
]


def register_all(app):
    """Register all blueprints on the Flask app."""
    import logging
    _log = logging.getLogger(__name__)

    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)

    # ── Plugin blueprints (tofu.blueprints entry-point group) ──
    # External feature packages (e.g. the tofu-trading subsystem) mount their
    # Blueprints here. Discovery is fail-soft and returns [] when no plugin is
    # installed, so this is a no-op for a vanilla core install. The name guard
    # is defensive against a plugin shipping a duplicate blueprint name.
    from .plugin_registry import discover_blueprint_plugins, run_startup_hooks
    _already = {bp.name for bp in ALL_BLUEPRINTS}
    for bp in discover_blueprint_plugins():
        if bp.name in _already:
            _log.warning('[BlueprintRegistry] plugin blueprint %r already '
                         'registered in-tree — skipping', bp.name)
            continue
        app.register_blueprint(bp)
        _already.add(bp.name)

    # ── Start daily report background scheduler ──
    try:
        from lib.daily_report import start_report_scheduler
        start_report_scheduler()
    except Exception as e:
        _log.warning('Daily report scheduler start deferred (DB unavailable): %s', e)

    # ── Start proactive agent / cron scheduler ──
    try:
        from lib.scheduler import start_scheduler_worker
        start_scheduler_worker()
    except Exception as e:
        _log.warning('Scheduler worker start deferred (DB unavailable): %s', e)

    # ── Plugin startup hooks (tofu.startup entry-point group) ──
    # Background workers / schedulers / post-registration init that an optional
    # feature needs (e.g. the trading intel + autopilot workers, brain
    # cycle-count restore). No-op when no plugin is installed.
    try:
        run_startup_hooks(app)
    except Exception as e:
        _log.warning('Plugin startup hooks deferred: %s', e)
