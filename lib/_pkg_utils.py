"""lib/_pkg_utils.py — Shared utilities for package façade consistency.

Every decomposed package under ``lib/`` uses the same resilient-import
façade pattern.  This module provides the helper functions so each
``__init__.py`` stays DRY and consistent.

Quick-start (preferred pattern)::

    from lib._pkg_utils import build_facade, safe_import

    __all__: list[str] = []
    _import = safe_import(__name__, globals(), __all__)

    # Core (must load — let exceptions propagate)
    from .config import *    # noqa: F401,F403
    from .core import *      # noqa: F401,F403
    build_facade(__all__, config, core)

    # Optional (degrade gracefully — 1 line each!)
    _import('optional_mod', 'feature X')
    _import('another_mod')

Batch pattern (most concise for many optional modules)::

    from lib._pkg_utils import build_facade, facade_imports

    __all__: list[str] = []

    # Core
    from .config import *  # noqa: F401,F403
    build_facade(__all__, config)

    # Optional — one call for all
    facade_imports(__name__, globals(), __all__, [
        ('browser',   'browser tool schemas'),
        ('code_exec', 'code execution tool'),
    ])
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from types import ModuleType

from lib.log import get_logger

__all__ = [
    "extend_all",
    "build_facade",
    "resilient_import",
    "safe_import",
    "safe_star_import",
    "facade_imports",
    "validate_all",
]

_logger = get_logger(__name__)


# ── Core helpers ─────────────────────────────────────────

def extend_all(pkg_all: list[str], module: ModuleType) -> None:
    """Append *module*'s ``__all__`` entries to *pkg_all* (the package list).

    If *module* has no ``__all__``, this is a no-op — nothing leaks.
    """
    mod_all = getattr(module, "__all__", None)
    if mod_all:
        pkg_all.extend(mod_all)


def build_facade(pkg_all: list[str], *modules: ModuleType) -> None:
    """Convenience: call :func:`extend_all` for each module in order."""
    for mod in modules:
        extend_all(pkg_all, mod)


def safe_star_import(
    pkg_globals: dict,
    module: ModuleType,
    pkg_all: list[str] | None = None,
) -> None:
    """Programmatically do ``from module import *`` into *pkg_globals*.

    Reads ``module.__all__`` to decide which names to copy.  If *pkg_all*
    is provided, extends it too (combining the ``extend_all`` step).
    """
    mod_all = getattr(module, "__all__", None)
    if not mod_all:
        return
    for name in mod_all:
        obj = getattr(module, name, None)
        if obj is not None:
            pkg_globals[name] = obj
    if pkg_all is not None:
        pkg_all.extend(mod_all)


def resilient_import(
    mod_name: str,
    target_globals: dict,
    target_all: list[str],
    logger: logging.Logger,
    *,
    feature_label: str = "",
) -> ModuleType | None:
    """Import *mod_name* resiliently, merging its public API on success.

    Combines ``importlib.import_module`` + ``safe_star_import`` in a single
    try/except.  On failure, logs a warning via *logger* and returns ``None``
    so the rest of the package keeps working.

    Parameters
    ----------
    mod_name:
        Fully-qualified module name (e.g. ``'lib.feishu.pipeline'``).
    target_globals:
        The caller's ``globals()`` dict — symbols are injected here.
    target_all:
        The caller's ``__all__`` list — extended with the module's exports.
    logger:
        Logger instance for the warning on failure.
    feature_label:
        Human-readable label for the log message (defaults to *mod_name*).
    """
    try:
        mod = importlib.import_module(mod_name)
        safe_star_import(target_globals, mod, target_all)
        return mod
    except Exception as exc:
        label = feature_label or mod_name
        logger.warning(
            "%s failed to load — %s disabled: %s",
            mod_name,
            label,
            exc,
            exc_info=True,
        )
        return None


# ── Safe importer (bound to a specific package) ─────────

class _SafeImporter:
    """Callable returned by :func:`safe_import`.

    Wraps ``importlib.import_module`` + ``safe_star_import`` inside
    try/except so a single broken sub-module doesn't take down the
    whole package.  On success, the sub-module's ``__all__`` names are
    injected into *pkg_globals* **and** appended to *pkg_all*.
    """

    __slots__ = ("_pkg", "_globals", "_all", "_logger")

    def __init__(
        self,
        pkg_name: str,
        pkg_globals: dict,
        pkg_all: list[str],
    ) -> None:
        self._pkg = pkg_name
        self._globals = pkg_globals
        self._all = pkg_all
        self._logger = get_logger(pkg_name)

    def __call__(
        self,
        submodule_name: str,
        feature_label: str = "",
    ) -> ModuleType | None:
        """Try to import *submodule_name* and merge its public API.

        On success the sub-module's ``__all__`` symbols are injected into
        the package's ``globals()`` and ``__all__``.  On failure a warning
        is logged and ``None`` is returned.

        Parameters
        ----------
        submodule_name:
            Relative name — ``'ensemble'`` or ``'.ensemble'`` both work.
        feature_label:
            Human-readable label for the warning message
            (e.g. ``'Monte Carlo simulation'``).  Defaults to *submodule_name*.
        """
        # Normalise: accept both '.foo' and 'foo'
        bare = submodule_name.lstrip(".")
        fqn = f"{self._pkg}.{bare}"
        try:
            mod = importlib.import_module(fqn)
            safe_star_import(self._globals, mod, self._all)
            return mod
        except Exception as exc:
            label = feature_label or bare
            self._logger.warning(
                "%s.%s failed to load — %s disabled: %s",
                self._pkg,
                bare,
                label,
                exc,
                exc_info=True,
            )
            return None


def safe_import(
    pkg_name: str,
    pkg_globals: dict,
    pkg_all: list[str],
) -> _SafeImporter:
    """Factory: returns a callable ``_import(submod, label)`` bound to a package.

    The returned callable imports a sub-module by name, injects its
    ``__all__`` names into *pkg_globals*, extends *pkg_all*, and logs
    a warning on failure.

    Example::

        from lib._pkg_utils import build_facade, safe_import

        __all__: list[str] = []
        _import = safe_import(__name__, globals(), __all__)

        # Core
        from .core import *  # noqa: F401,F403
        build_facade(__all__, core)

        # Optional — one line each
        _import('browser', 'browser tool schemas')
        _import('code_exec', 'code execution tool')
    """
    return _SafeImporter(pkg_name, pkg_globals, pkg_all)


# ── Batch helper ─────────────────────────────────────────

def facade_imports(
    pkg_name: str,
    pkg_globals: dict,
    pkg_all: list[str],
    modules: Sequence[str | tuple[str, str]],
) -> dict[str, ModuleType | None]:
    """Import multiple optional sub-modules in one call.

    Each entry in *modules* is either a bare module name (``str``) or a
    ``(name, feature_label)`` tuple.  Returns a dict mapping each bare
    name to its module (or ``None`` on failure).

    Example::

        facade_imports(__name__, globals(), __all__, [
            ('browser',   'browser tool schemas'),
            ('code_exec', 'code execution tool'),
            'utils',  # label defaults to 'utils'
        ])
    """
    importer = _SafeImporter(pkg_name, pkg_globals, pkg_all)
    results: dict[str, ModuleType | None] = {}
    for entry in modules:
        if isinstance(entry, str):
            name, label = entry, ""
        else:
            name, label = entry[0], entry[1] if len(entry) > 1 else ""
        results[name] = importer(name, label)
    return results


# ── Validation (for tests / CI) ─────────────────────────

def validate_all(module: ModuleType) -> list[str]:
    """Return names listed in *module.__all__* that don't actually exist.

    Useful in CI to catch stale ``__all__`` entries after a rename.
    """
    mod_all = getattr(module, "__all__", [])
    return [name for name in mod_all if not hasattr(module, name)]
