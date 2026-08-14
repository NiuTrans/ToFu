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

from functools import lru_cache
import os
from pathlib import Path

from lib.log import get_logger


logger = get_logger(__name__)
__all__ = ['getenv_compat', 'getenv_project_compat']

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


@lru_cache(maxsize=8)
def _project_env_values(project_root: str) -> dict[str, str]:
    """Read one project's dotenv file once without mutating ``os.environ``."""
    values: dict[str, str] = {}
    path = Path(project_root) / '.env'
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError as exc:
        logger.debug('[EnvCompat] project dotenv unavailable: %s', exc)
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1]
                and value[0] in ('"', "'")):
            value = value[1:-1]
        values[key] = value
    return values


def getenv_project_compat(*names, default='', project_root=None):
    """Resolve env aliases with the project ``.env`` as a fallback.

    This mirrors ``server.py``'s fill-if-absent dotenv loading without
    mutating process-global environment state, so standalone data tools see
    the same storage authority flags as the server. Explicit shell variables,
    including an empty value, shadow the file exactly as server startup does.
    """
    root = (Path(project_root).resolve() if project_root is not None
            else Path(__file__).resolve().parent.parent)
    file_values = _project_env_values(str(root))
    for name in _expand_aliases(names):
        value = (os.environ[name] if name in os.environ
                 else file_values.get(name))
        if value:
            return value
    return default
