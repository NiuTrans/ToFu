"""Model-facing generic website-research handler.

The orchestration and context policy live in :mod:`lib.browser.research`; this
module only preserves the uniform ``handler(arguments, runtime)`` boundary.
"""

from lib.browser.research import research_page


def _handle_research_page(fn_args, runtime):
    return research_page(fn_args, runtime)


__all__ = ['_handle_research_page']
