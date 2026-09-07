"""tofu_trading/web — ``tofu.blueprints`` + ``tofu.startup`` registrars.

The Tofu host calls :func:`register` (the ``tofu.blueprints`` entry point) from
``routes/__init__.py::register_all`` and :func:`start_workers` (the
``tofu.startup`` entry point) right after, once blueprints are mounted.

``register()`` returns:
  1. the 7 ``api_v1_trading_*_bp`` v1 Blueprints (REST surface) — importing the
     handler modules attaches their route decorators to those blueprints;
  2. a ``trading_pages_bp`` that serves the ``/trading.html`` SPA page and the
     trading-owned static assets (``/trading-static/...`` → trading.css +
     static/js/trading/*). The page itself reuses the host's canonical shared
     assets (``/static/js/api.js`` and ``/static/vendor/*``) over the same
     origin — those stay in core and are NOT vendored here.

``start_workers(app)`` launches the intel + autopilot background threads and
restores brain cycle-count (formerly core's ``server.py`` block + the
``register_all`` init_brain hook).

The ``trading_enabled`` flag is enforced at REQUEST time, not at mount time
(see :mod:`tofu_trading.gate`): the blueprints are always registered and each
one refuses to serve while the feature is off. Registering them conditionally
would make the Settings toggle require a restart to take effect, because Quart
cannot mount a blueprint after the app has started serving. The background
workers check the same flag every pass so turning the feature off stops their
LLM spend rather than merely hiding the UI.
"""

from __future__ import annotations

import os

from flask import Blueprint, abort, send_from_directory

from lib.api_response import api_error
from lib.log import get_logger

from tofu_trading.gate import trading_enabled

# Startup and feature-gate records are operator-facing lifecycle evidence, not
# noisy dependency chatter.  The routes.* namespace is retained by app.log.
logger = get_logger('routes.plugins.tofu_trading')

_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES_DIR = os.path.join(_PKG_DIR, 'templates')
_STATIC_DIR = os.path.join(_PKG_DIR, 'static')

# Page + static blueprint for the trading SPA and its owned assets.
trading_pages_bp = Blueprint('trading_pages', __name__)


@trading_pages_bp.route('/trading.html')
def trading_page():
    """Serve the trading SPA page (reuses core's api.js + vendor/* assets)."""
    if not trading_enabled():
        # 404 rather than 403: while the feature is off the page genuinely does
        # not exist for this deployment, and a 403 would imply a permission
        # problem the user could fix by logging in as someone else.
        abort(404)
    return send_from_directory(_TEMPLATES_DIR, 'trading.html')


@trading_pages_bp.route('/trading-static/<path:filename>')
def trading_static(filename):
    """Serve trading-owned static assets (trading.css, static/js/trading/*)."""
    if not trading_enabled():
        abort(404)
    full = os.path.join(_STATIC_DIR, filename)
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(_STATIC_DIR, filename)


def _reject_when_disabled():
    """Blueprint ``before_request`` hook: 404 every route while the flag is off.

    Returning a value from a ``before_request`` hook short-circuits the request,
    so the handler (and its DB/LLM work) never runs.
    """
    if not trading_enabled():
        return api_error('trading_disabled', status=404,
                         context='The trading module is switched off in Settings.')
    return None


def register() -> list:
    """Return the trading Blueprints to mount on the host app."""
    # Side-effect imports: attach @bp.route handlers to the v1 blueprints.
    from tofu_trading.web.handlers import (  # noqa: F401
        trading_autopilot, trading_brain, trading_decision, trading_holdings,
        trading_intel, trading_reconcile, trading_simulator, trading_tasks,
    )

    from tofu_trading.web.v1.autopilot import api_v1_trading_autopilot_bp
    from tofu_trading.web.v1.brain import api_v1_trading_brain_bp
    from tofu_trading.web.v1.decision import api_v1_trading_decision_bp
    from tofu_trading.web.v1.holdings import api_v1_trading_holdings_bp
    from tofu_trading.web.v1.intel import api_v1_trading_intel_bp
    from tofu_trading.web.v1.reconcile import api_v1_trading_reconcile_bp
    from tofu_trading.web.v1.simulator import api_v1_trading_simulator_bp
    from tofu_trading.web.v1.tasks import api_v1_trading_tasks_bp

    blueprints = [
        api_v1_trading_holdings_bp,
        api_v1_trading_intel_bp,
        api_v1_trading_decision_bp,
        api_v1_trading_autopilot_bp,
        api_v1_trading_tasks_bp,
        api_v1_trading_brain_bp,
        api_v1_trading_simulator_bp,
        api_v1_trading_reconcile_bp,
        trading_pages_bp,
    ]
    # Refuse every trading API call while the feature is off. Attached here
    # rather than per-handler so a newly added route is covered by default;
    # trading_pages_bp guards its own two routes inline because it also serves
    # static assets.
    #
    # Idempotent by necessity: the blueprints are module-level singletons, so a
    # second register() (a test that calls it twice, a host that re-discovers
    # plugins) would re-attach the hook — and Flask REFUSES a setup method on an
    # already-registered blueprint ("can no longer be called ... registered at
    # least once"). Marking the blueprint keeps the guard attached exactly once
    # for the life of the process.
    for bp in blueprints:
        if bp is trading_pages_bp:
            continue
        if getattr(bp, '_trading_gate_attached', False):
            continue
        bp.before_request(_reject_when_disabled)
        bp._trading_gate_attached = True
    logger.info('[tofu-trading] registered %d trading blueprint(s)', len(blueprints))
    return blueprints


def get_task_runtimes() -> list:
    """``tofu.task_runtimes`` hook: expose the trading-sim TaskRuntime.

    Lets the host's generic ``/api/v1/tasks`` endpoints discover the
    ``trading-sim`` task kind without core naming it.
    """
    enabled = trading_enabled()
    try:
        from tofu_trading.web.handlers.trading_simulator import _runtime
        return [_runtime]
    except Exception as e:
        if enabled:
            logger.warning(
                '[tofu-trading] trading-sim runtime failed to load: %s', e,
                exc_info=True)
        else:
            logger.debug('[tofu-trading] trading-sim runtime unavailable: %s', e)
        return []


def start_workers(app) -> None:
    """``tofu.startup`` hook: launch background workers + restore brain state.

    Storage registration and the verified legacy import run before any worker
    or DB-backed module. Other startup failures propagate to the host's
    fail-soft entry-point registry so it records the hook as failed.

    The threads are started regardless of the flag and idle inside their own
    loop while it is off (see :func:`tofu_trading.gate.wait_until_enabled`), so
    re-enabling the feature resumes them without a restart. Nothing that costs
    money runs before the first flag check.
    """
    enabled = trading_enabled()
    from tofu_trading.storage import prepare_storage

    migration = prepare_storage()
    logger.info(
        '[tofu-trading] sidecar storage ready: migration=%s manifest=%d '
        'tables=%d rows=%d',
        migration.get('migration', 'unknown'),
        int(migration.get('manifest_version') or 0),
        len(migration.get('tables') or {}),
        int(migration.get('total_rows') or 0),
    )

    if not enabled:
        logger.info('[tofu-trading] feature is off at boot — workers will idle '
                    'until it is enabled in Settings')
    try:
        from tofu_trading.web.handlers.trading_brain import init_brain
        init_brain()
    except Exception as e:
        if enabled:
            logger.warning(
                '[tofu-trading] brain initialization failed: %s', e,
                exc_info=True)
        else:
            logger.debug('[tofu-trading] brain init deferred: %s', e)

    # Resolve both factories before starting either one. Import failures then
    # propagate to the host's fail-soft entry-point registry without leaving a
    # half-started plugin or producing a false "startup hook ran" success.
    from tofu_trading.web.handlers.trading_intel import start_intel_worker
    from tofu_trading.web.handlers.trading_autopilot import start_autopilot_worker

    start_intel_worker(app)
    start_autopilot_worker()
    logger.info('[tofu-trading] background workers started')


__all__ = ['register', 'start_workers', 'get_task_runtimes', 'trading_pages_bp']
