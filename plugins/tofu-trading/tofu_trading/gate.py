"""tofu_trading/gate.py — the single place that answers "is trading on?".

``flags.py`` DECLARES the ``trading_enabled`` flag to the host; this module
READS it. Before this existed, nothing read it at all: ``web.register()``
returned its blueprints, ``start_workers()`` started its threads, and
``schema.register()`` created its tables unconditionally, so switching the
Settings toggle off changed nothing — the REST surface kept serving and the
intel crawler kept spending LLM budget in the background.

Two reasons the check is a live read rather than a boot-time snapshot:

1. **Cost.** The intel crawler analyses batches of items through
   ``smart_chat_batch`` every 2 hours and the autopilot cycle calls
   ``smart_chat``. Both run on daemon threads with no user watching, so an
   unread flag means a user who turned the feature off is still paying for it.
   Re-reading each pass means "off" stops the spend at the next tick.
2. **Hot toggling.** ``POST /api/v1/features`` rewrites ``features.json`` and
   assigns ``lib.TRADING_ENABLED`` in-process. Reading the attribute per call
   therefore takes effect immediately, with no restart.

``getattr`` with a ``False`` default (rather than ``import lib; lib.TRADING_ENABLED``)
keeps this working on a host whose flag registry never ran — the attribute is
only set when ``lib._load_plugin_flags()`` discovered our ``tofu.flags`` entry
point.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# features.json key / lib attribute — kept in sync with flags.FLAG.
_JSON_KEY = 'trading_enabled'
_ENV_KEY = 'TRADING_ENABLED'


def trading_enabled() -> bool:
    """Return whether the trading feature is currently switched on.

    Reads the live ``lib.TRADING_ENABLED`` attribute, which the host's
    ``/api/v1/features`` handler reassigns on every toggle. Never raises: a
    host without the flag registry reads as disabled.
    """
    try:
        import lib as _lib
        return bool(getattr(_lib, _ENV_KEY, False))
    except Exception as e:
        # A failure here must not take down a request or kill a worker thread;
        # failing closed also means a broken host cannot silently spend budget.
        logger.warning('[tofu-trading] flag read failed, treating as disabled: %s', e)
        return False


def wait_until_enabled(sleep_fn, interval: float) -> None:
    """Block a background worker while the feature is switched off.

    Args:
        sleep_fn: the worker's sleep callable (``time.sleep``).
        interval: seconds to sleep between re-checks.

    The worker loops here instead of exiting so that turning the feature back
    on resumes it without a restart — the threads are started once at boot and
    are never re-created.
    """
    logged = False
    while not trading_enabled():
        if not logged:
            # Once per off-period, not once per poll: this would otherwise be
            # the noisiest line in app.log for anyone running with it off.
            logger.info('[tofu-trading] feature disabled — worker idle, '
                        'no LLM calls will be made until it is re-enabled')
            logged = True
        sleep_fn(interval)
    if logged:
        logger.info('[tofu-trading] feature re-enabled — worker resuming')


__all__ = ['trading_enabled', 'wait_until_enabled']
