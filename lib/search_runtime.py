"""Lazy activation boundary for the Tofu host's optional search runtime.

Responsibility: keep the heavyweight search/fetch dependency graph out of
ordinary server boot and non-search requests while guaranteeing that every
real network search/fetch entry point receives the host's configuration,
browser, and auth-source providers before use.

Entry points:
``ensure_search_runtime`` activates the runtime on first real use;
``sync_search_config_if_loaded`` hot-applies settings without causing a cold
optional import; ``search_library_is_loaded`` supports dependency-free schema
decisions.  The module deliberately imports ``lib.search_bridge`` only inside
the activation function.
"""

from __future__ import annotations

import sys
from types import ModuleType

from lib.log import get_logger


logger = get_logger(__name__)

__all__ = [
    'ensure_search_runtime',
    'prepare_search_dependency_import',
    'search_library_is_loaded',
    'search_runtime_is_active',
    'sync_search_config_if_loaded',
]


def prepare_search_dependency_import() -> bool:
    """Apply process resource policy before any direct tofu-search import.

    Parser, PDF-export, and interactive-login consumers use tofu-search
    submodules without needing the host provider bridge. They still require the
    classic PyMuPDF policy or a standalone worker can create an unused ONNX
    session pool merely by importing the package.
    """
    try:
        from runtime_guards import install_pymupdf_classic_policy
        return install_pymupdf_classic_policy()
    except Exception as error:
        logger.warning(
            'Optional tofu-search import policy could not be installed: %s',
            error)
        return False


def search_library_is_loaded() -> bool:
    """Return whether tofu-search is already resident, without importing it."""
    return sys.modules.get('tofu_search') is not None


def search_runtime_is_active() -> bool:
    """Return whether chatui's providers are installed, without cold imports."""
    bridge = sys.modules.get('lib.search_bridge')
    return bool(bridge is not None and getattr(bridge, '_installed', False))


def _linkage_diagnostic(error: BaseException) -> str:
    message = str(error)
    if not any(token in message for token in ('GLIBCXX', 'libstdc++', 'symbol')):
        return ''
    try:
        from lib.server_linkage_forensics import capture_linkage_forensics
        return f' | LINKAGE: {capture_linkage_forensics()}'
    except Exception as diagnostic_error:
        logger.debug(
            'Native linkage forensics unavailable: %s',
            type(diagnostic_error).__name__,
        )
        return ' | LINKAGE: unavailable'


def ensure_search_runtime() -> ModuleType:
    """Activate and return tofu-search at the first real capability use.

    Activation is fail-local: callers receive the original exception and their
    existing route/tool boundary converts it into a per-call error.  A linkage
    failure is logged with enough native-library evidence to diagnose it; it
    can no longer abort server startup.
    """
    try:
        # Direct paper/CLI workers do not pass through server.py.  Install the
        # same dependency-light policy here before tofu-search can transitively
        # import pymupdf4llm and create an unused host-sized ONNX pool.
        prepare_search_dependency_import()

        from lib.search_bridge import install_search_bridge
        install_search_bridge()
        search_module = sys.modules.get('tofu_search')
        if search_module is None:  # defensive: the bridge owns this import
            raise RuntimeError(
                'tofu-search bridge installed without a loaded tofu_search module')
        return search_module
    except Exception as error:
        logger.error(
            'Web search/fetch is UNAVAILABLE for this call — optional runtime '
            'activation failed: %s%s. Other server capabilities remain active.',
            error, _linkage_diagnostic(error), exc_info=True)
        raise


def sync_search_config_if_loaded() -> bool:
    """Hot-sync tofu-search only when its bridge is already resident.

    Returns ``False`` when no search/fetch call has activated the optional
    runtime yet.  The first later activation reads the newest settings, so
    skipping a cold import loses no configuration update.
    """
    bridge = sys.modules.get('lib.search_bridge')
    sync = getattr(bridge, 'sync_search_config', None) if bridge else None
    if not callable(sync):
        return False
    sync()
    return True
