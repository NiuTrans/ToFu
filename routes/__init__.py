"""routes/ — Flask Blueprints for each domain.

Each module is self-contained and registers its own routes.
Shared helpers: lib/database/, lib/llm/ (package), lib/__init__.py (config).
"""

import lib as _lib

from .browser import browser_bp
from .chat import chat_bp
# Side-effect imports: each registers additional routes on chat_bp.
from . import chat_queue  # noqa: F401  — /api/chat/queue/*
from . import chat_human_io  # noqa: F401  — /api/chat/{stdin,human}_response
from . import chat_tool_state  # noqa: F401  — /api/chat/tool-state/<id>
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

# ── Trading blueprints (conditionally loaded) ──
# NOTE: blueprint registration happens at *import time*, so flipping
# `TRADING_ENABLED` later via Settings UI cannot retroactively register the
# trading routes. ``TRADING_ROUTES_REGISTERED`` snapshots the registration
# decision so save_features() can correctly tell the user "needs_restart"
# when they enable trading on a server that booted with it disabled.
TRADING_ROUTES_REGISTERED = bool(_lib.TRADING_ENABLED)

if TRADING_ROUTES_REGISTERED:
    # Importing each trading module triggers its handler decorators to
    # register on the corresponding api_v1 blueprint. We then add those
    # v1 blueprints to ALL_BLUEPRINTS — they are NOT in ALL_V1_BLUEPRINTS
    # by default because trading is conditional on TRADING_ENABLED.
    from . import trading_autopilot  # noqa: F401
    from . import trading_brain      # noqa: F401
    from . import trading_decision   # noqa: F401
    from . import trading_holdings   # noqa: F401
    from . import trading_intel      # noqa: F401
    from . import trading_simulator  # noqa: F401
    from . import trading_tasks      # noqa: F401

    from .api_v1.trading.autopilot import api_v1_trading_autopilot_bp
    from .api_v1.trading.brain     import api_v1_trading_brain_bp
    from .api_v1.trading.decision  import api_v1_trading_decision_bp
    from .api_v1.trading.holdings  import api_v1_trading_holdings_bp
    from .api_v1.trading.intel     import api_v1_trading_intel_bp
    from .api_v1.trading.simulator import api_v1_trading_simulator_bp
    from .api_v1.trading.tasks     import api_v1_trading_tasks_bp

    TRADING_BLUEPRINTS = [
        api_v1_trading_holdings_bp,
        api_v1_trading_intel_bp,
        api_v1_trading_decision_bp,
        api_v1_trading_autopilot_bp,
        api_v1_trading_tasks_bp,
        api_v1_trading_brain_bp,
        api_v1_trading_simulator_bp,
    ]
    ALL_BLUEPRINTS.extend(TRADING_BLUEPRINTS)


def register_all(app):
    """Register all blueprints on the Flask app."""
    import logging
    _log = logging.getLogger(__name__)

    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)

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

    # ── Post-registration init hooks ──
    if _lib.TRADING_ENABLED:
        with app.app_context():
            try:
                from .trading_brain import init_brain
                init_brain()
            except Exception as e:
                _log.debug('Brain init deferred: %s', e)
