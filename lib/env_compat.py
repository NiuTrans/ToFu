"""lib/env_compat.py — Tofu env-var reader.

``getenv_compat(*names, default='')`` returns the first non-empty value
among ``names``. Originally, this helper carried a one-time deprecation
warning when a legacy ``CHATUI_*`` alias resolved a request — the
project was rebranded from ChatUI to Tofu, and we kept ``CHATUI_*``
working through a transition window. The transition is over: only
``TOFU_*`` names are honoured.

The variadic signature is preserved so call sites that pass a single
name keep working without churn. Pass legacy names if you must, but
they are now treated as ordinary env-var lookups — no warning, no
special behaviour.
"""

import os

__all__ = ['getenv_compat']


def getenv_compat(*names, default=''):
    """Return the first non-empty env var among ``names`` else ``default``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default
