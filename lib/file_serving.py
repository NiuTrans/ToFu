"""lib/file_serving.py — conditional (Range-aware) file serving, one seam.

WHY THIS SEAM EXISTS (measured 2026-08-02, live on the desktop-installer
download route; independently verified twice)
-----------------------------------------------
With the installed quart / werkzeug 3.1.8 pair, ``send_file(...,
conditional=True)`` 500s on a SINGLE-BYTE Range (``bytes=0-0``,
``bytes=5-5`` — the classic "does this server resume?" probe every
download manager sends). Chain: quart's ``_process_range_request`` passes
``end - 1`` as the inclusive stop into ``ContentRange.set``, and
werkzeug's ``is_byte_range_valid`` rejects ``start >= stop`` outright —
so ``begin == end - 1`` raises ``AssertionError: Bad range provided``,
which escapes as an uncaught 500. Multi-byte ranges (``bytes=0-99``) and
unsatisfiable ones (416) work fine; plain GETs are unaffected.

``send_file_conditional`` is the single seam every file-serving route
calls instead of send_file directly. Behaviour:

  * identical to ``send_file(..., conditional=True)`` for every input
    except that exact AssertionError;
  * that AssertionError degrades to a plain full-body 200 (a spec-legal
    answer to any GET — a server may always ignore a Range header),
    logged with the path, never a 500.

The catch is narrowed to the werkzeug message so no OTHER assertion
silently turns into a 200.

Sync-shaped and call-time-imported ON PURPOSE: ``server.py``'s Flask→Quart
shim replaces ``quart.send_file`` with a sync-safe adapter at app
construction, so every route in this codebase calls a SYNC send_file
from thread-pool handlers and gets a real Response back. Resolving the
name at call time means this seam always lands on whatever binding the
running app installed — never on a module-import-time snapshot of the
pre-shim async original.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def send_file_conditional(path, **kwargs):
    """``send_file(path, conditional=True, **kwargs)`` without the 500.

    Drop-in for the routes' current ``send_file(..., conditional=True)``
    calls (sync or async handlers alike — see module docstring).
    """
    from quart import send_file as _send_file
    kwargs['conditional'] = True
    try:
        return _send_file(path, **kwargs)
    except AssertionError as e:
        if 'Bad range provided' not in str(e):
            raise
        logger.warning('[FileServing] conditional send_file failed for %s '
                       '(single-byte Range probe; see module docstring) — '
                       'falling back to full-body 200', path)
        kwargs['conditional'] = False
        return _send_file(path, **kwargs)


__all__ = ['send_file_conditional']
