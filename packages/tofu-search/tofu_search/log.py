"""tofu_search.log — Standalone logging utilities.

Provides get_logger() for consistent logging across the package.
No dependency on chatui's lib/log.py.
"""

import logging
import sys

__all__ = ['get_logger']

# ── Package-level handler (console) ──
# Configured once at import time, but ONLY for genuinely standalone usage.
#
# When tofu_search is embedded in a host application (e.g. chatui) that has
# already configured the ROOT logger with its own handlers, we must NOT add a
# handler of our own: records emitted by 'tofu_search.*' loggers propagate up
# to the root logger and are written by the host's handlers (file/console).
# Attaching our own stderr handler on top would (a) duplicate every line onto
# the console and (b) wrest control of log routing away from the host. The
# correct library idiom is to defer to the application's logging config.
#
# Standalone usage (examples, scripts that never call logging.basicConfig)
# leaves the root logger handler-less — there we DO attach a console handler so
# the library's pipeline diagnostics are visible out of the box.
_pkg_logger = logging.getLogger('tofu_search')
_root_logger = logging.getLogger()

if not _pkg_logger.handlers and not _root_logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s [%(name)s] %(message)s',
        datefmt='%H:%M:%S',
    ))
    _pkg_logger.addHandler(_handler)
    _pkg_logger.setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given module name.

    Args:
        name: Typically ``__name__`` from the calling module.

    Returns:
        A logging.Logger instance under the 'tofu_search' hierarchy.
    """
    return logging.getLogger(name)
