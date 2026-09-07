"""User-facing error formatter — thin wrapper around :mod:`lib.error_envelope`.

Historically this module returned a multi-line bilingual string that
became the literal ``task['error']`` value on the wire.  As of
2026-05-22 every emit site uses a typed envelope dict instead (see
:mod:`lib.error_envelope` for the schema), so this wrapper exists only
to keep the call-site signature stable while the rest of the codebase
migrates.

There is **no string return path anymore.**  Every call to
:func:`format_llm_error_for_user` produces a fully typed envelope
(``{'kind', 'severity', 'retryable', 'message', 'hint', 'detail',
'model', 'context', 'source', 'raw'}``).
"""

from __future__ import annotations

from lib.error_envelope import from_exception
from lib.log import get_logger

logger = get_logger(__name__)


def format_llm_error_for_user(exc: BaseException, *, model: str = '',
                              context: str = '',
                              source: str = 'llm') -> dict:
    """Return a typed error envelope for *exc*.

    Parameters
    ----------
    exc : BaseException
        The exception raised by the LLM dispatch / streaming layer.
    model : str
        The model that was being called (shown to help users pick a
        different one in Settings).
    context : str
        Short context tag (``'fallback'``, ``'task-fatal'``, etc.) so
        operators can tell which code path fired.
    source : str
        Component that originated the error.  Default ``'llm'`` covers
        the streaming / dispatch path.  Tool and orchestrator paths
        pass their own source tag.

    Returns
    -------
    dict
        Typed error envelope — see :mod:`lib.error_envelope` for the
        schema.
    """
    return from_exception(exc, model=model, context=context, source=source)
