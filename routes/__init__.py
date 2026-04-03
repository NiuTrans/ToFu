"""routes/ — Flask Blueprints for each domain.

Each module is self-contained and registers its own routes.
Shared helpers: lib/database.py, lib/llm_client.py, lib/__init__.py (config).
"""

import lib as _lib

from .browser import browser_bp
from .chat import chat_bp
from .common import common_bp
from .config import config_bp
from .conversations import conversations_bp
from .daily_report import daily_report_bp
from .desktop import desktop_bp
from .endpoint import endpoint_bp
from .errors import errors_bp
from .project import project_bp
from .scheduler import scheduler_bp
from .skills import skills_bp
from .swarm import swarm_bp
from .translate import translate_bp
from .upload import upload_bp

# ── Core (always-on) blueprints ──
ALL_BLUEPRINTS = [
    common_bp,
    errors_bp,
    config_bp,
    conversations_bp,
    upload_bp,
    translate_bp,
    chat_bp,
    project_bp,
    skills_bp,
    browser_bp,
    desktop_bp,
    scheduler_bp,
    swarm_bp,
    endpoint_bp,
    daily_report_bp,
]

# ── Trading blueprints (conditionally loaded) ──
if _lib.TRADING_ENABLED:
    from .trading_autopilot import trading_autopilot_bp
    from .trading_brain import trading_brain_bp
    from .trading_decision import trading_decision_bp
    from .trading_holdings import trading_holdings_bp
    from .trading_intel import trading_intel_bp
    from .trading_simulator import trading_simulator_bp
    from .trading_tasks import trading_tasks_bp

    TRADING_BLUEPRINTS = [
        trading_holdings_bp,
        trading_intel_bp,
        trading_decision_bp,
        trading_autopilot_bp,
        trading_tasks_bp,
        trading_brain_bp,
        trading_simulator_bp,
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
        from .daily_report import start_report_scheduler
        start_report_scheduler()
    except Exception as e:
        _log.warning('Daily report scheduler start deferred (DB unavailable): %s', e)

    # ── Start proactive agent / cron scheduler ──
    try:
        from .scheduler import start_scheduler_worker
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
