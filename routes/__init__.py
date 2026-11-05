"""routes/ — Quart blueprints for each domain.

Each module is self-contained and registers its own routes.
Shared helpers live in focused ``lib/`` service packages. Durable reads and
writes go through semantic ``lib.storage`` clients; routes never own SQL.

Optional feature blueprints are discovered through ``lib.app.plugin_registry``.
Process startup and shutdown are owned by ``lib.server`` and never run during
blueprint registration.
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

from .response_assets import response_assets_bp
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

    response_assets_bp,
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


def register_all(app, *, discover_plugins=True):
    """Mount core and plugin blueprints without starting process services."""
    import logging
    _log = logging.getLogger(__name__)

    # Once the release major is 4, the retired v1 product surface is no
    # longer mounted at all: handlers live on through the v4 projection, and
    # /api/v1 requests meet the 426 upgrade fence. The per-process
    # TOFU_API_MAJOR=1 compatibility lane (shared v1 test app) keeps the
    # legacy mount until the test ladder finishes migrating.
    from lib.api_v4 import active_api_major
    from lib.generated_api_v4_constants import API_MAJOR as _RELEASE_MAJOR
    legacy_mount_active = active_api_major(app) < _RELEASE_MAJOR
    legacy_v1 = set(ALL_V1_BLUEPRINTS) if not legacy_mount_active else ()
    for bp in ALL_BLUEPRINTS:
        if bp in legacy_v1:
            continue
        app.register_blueprint(bp)

    # Dual-mounted v4 product routes ship dark until per-batch flip; withdraw
    # is a config change that never touches the live v1 surface.
    from lib.api_v4 import v4_routes_enabled
    from .api_v4 import API_V4_PRODUCT_BLUEPRINTS
    if v4_routes_enabled(app):
        for bp in API_V4_PRODUCT_BLUEPRINTS:
            app.register_blueprint(bp)

    # ── Plugin blueprints (tofu.blueprints entry-point group) ──
    # External feature packages (e.g. the tofu-trading subsystem) mount their
    # Blueprints here. Discovery is fail-soft and returns [] when no plugin is
    # installed, so this is a no-op for a vanilla core install. The name guard
    # is defensive against a plugin shipping a duplicate blueprint name.
    if discover_plugins:
        from lib.api_v4 import product_blueprints_for_policy
        from lib.app.plugin_registry import discover_blueprint_plugins
        _already = set(app.blueprints)
        for source in discover_blueprint_plugins():
            for bp in product_blueprints_for_policy(
                source, legacy_enabled=legacy_mount_active,
                v4_enabled=v4_routes_enabled(app),
            ):
                if bp.name in _already:
                    _log.warning('[BlueprintRegistry] plugin blueprint %r already '
                                 'registered in-tree — skipping', bp.name)
                    continue
                app.register_blueprint(bp)
                _already.add(bp.name)
