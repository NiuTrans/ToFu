"""lib/env_compat.py — Tofu env-var reader with legacy-alias fallback.

The project was rebranded from **ChatUI** to **Tofu**, so every environment
variable moved from the ``CHATUI_*`` namespace to ``TOFU_*``. We promise (in
the README / CLAUDE.md / INSTALL docs) that the old ``CHATUI_*`` names keep
working as aliases — so an operator upgrading an existing deployment isn't
forced to rename every var at once.

``getenv_compat(*names, default='')`` returns the first non-empty value among
``names``, AND — this is the whole point — for any ``TOFU_*`` name it is given
it ALSO transparently checks the matching ``CHATUI_*`` alias right after it.
A call site therefore only ever passes the modern ``TOFU_*`` name; the legacy
alias is honoured automatically:

    getenv_compat('TOFU_DB_PATH')          # checks TOFU_DB_PATH, then CHATUI_DB_PATH
    getenv_compat('TOFU_PG_HOST', default='127.0.0.1')

Precedence: a ``TOFU_*`` value always wins over its own ``CHATUI_*`` alias;
the alias only resolves when the ``TOFU_*`` var is unset/empty. The variadic
signature is preserved so existing single-name call sites are unchanged, and
explicitly-passed names keep their relative order (each followed by its derived
alias). Names that are not ``TOFU_*`` (or a ``CHATUI_*`` already passed
directly) are looked up verbatim.
"""

import os

__all__ = ['getenv_compat']

_LEGACY_PREFIX = 'CHATUI_'
_MODERN_PREFIX = 'TOFU_'


def _expand_aliases(names):
    """Yield each name, inserting the derived CHATUI_* alias after a TOFU_* name.

    Duplicates (e.g. the alias was also passed explicitly) are suppressed so the
    lookup order stays clean and each var is read at most once.
    """
    seen = set()
    for name in names:
        if name and name not in seen:
            seen.add(name)
            yield name
        if name and name.startswith(_MODERN_PREFIX):
            alias = _LEGACY_PREFIX + name[len(_MODERN_PREFIX):]
            if alias not in seen:
                seen.add(alias)
                yield alias


def getenv_compat(*names, default=''):
    """Return the first non-empty env var among ``names`` (+ legacy aliases).

    For every ``TOFU_*`` name the matching ``CHATUI_*`` alias is checked
    immediately after it, so legacy deployments keep working without the call
    site having to know the old name. Returns ``default`` if nothing is set.
    """
    for name in _expand_aliases(names):
        value = os.environ.get(name)
        if value:
            return value
    return default
