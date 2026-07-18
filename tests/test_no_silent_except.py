"""Silent-except guard (CLAUDE.md §2.2).

Goal
----
§2.2 mandates ZERO silent exception catches: every ``except`` that swallows
control flow (``pass`` / bare ``return`` / ``continue`` / ``break`` /
assignment-only) MUST leave a trace — a ``logger.*`` call, a ``log_exception`` /
``audit_log``, a re-``raise``, or a structured ``api_*`` / ``jsonify`` /
``abort`` response that surfaces the error to the caller.

This test AST-scans ``lib/`` and ``routes/`` for the highest-risk violation:
a BROAD handler (``except:`` / ``except Exception`` / ``except BaseException``)
whose body is silent by the definition above. It is a RATCHET keyed on
``ALLOWED`` — the count can only go DOWN. A new broad silent swallow fails CI
the moment it lands, which is exactly the decay that let coverage rot before.

End state (2026-07-13)
----------------------
``ALLOWED`` holds the 3 legitimately-silent sites (documented individually
below); every other broad silent swallow in lib/ and routes/ has been given a
trace. To drive this to ``{}`` you would have to eliminate even those 3 — but
each is genuinely correct to leave silent, so they are pinned with a reason.

Adding a fallback that swallows
-------------------------------
Add ``logger.debug('<why>: %s', e)`` (bind the exception with ``as e``) — that
is the §2.2 floor for an expected/harmless fallback. Do NOT add a file to
``ALLOWED`` to dodge this test unless the handler genuinely must not log
(e.g. it guards the logging call itself).
"""

from __future__ import annotations

import ast
import os

# ── Configuration ────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, '..'))
SCAN_ROOTS = ('lib', 'routes')
EXCL_DIRS = {'.tofu', '__pycache__', 'node_modules', '.git',
             '.tofu_worktrees', 'tofu_worktrees', '.project_sessions'}

_LOG_ATTRS = {'debug', 'info', 'warning', 'warn', 'error', 'critical',
              'exception', 'log', 'log_exception', 'audit_log', 'log_context'}
_LOG_NAMES = {'log_exception', 'audit_log'}
_STRUCT_NAMES = {'jsonify', 'abort', 'make_response'}

# ── Allowlist: broad handlers that are CORRECTLY silent ──────────────
# Keyed by repo-relative path → count of pinned sites in that file.
# Each entry documents WHY the handler must not log/raise.
ALLOWED: dict[str, int] = {
    # safe_route's _handle(e) routes to api_internal_error, which itself
    # logs the full traceback (exc_info=True). Logging here too would
    # double-log; the trace exists one call downstream.
    'lib/api_response.py': 2,
    # _run_with_deadline's _worker stores the exception in box['error'] and it
    # is re-raised on the caller thread — surfaced, not swallowed. (Relocated
    # from lib/memory/prefetch.py when that module was split into a package.)
    'lib/memory/prefetch/_rerank.py': 1,
    # _trace_fallback guards the logging call ITSELF; logging here could
    # recurse into a failing logging backend. §2.2 logging-path exemption.
    # (Relocated from lib/tasks_pkg/system_context.py when it became a package.)
    'lib/tasks_pkg/system_context/_inject.py': 1,
}


# ── AST predicates ───────────────────────────────────────────────────
def _is_broad(exc_type: ast.expr | None) -> bool:
    if exc_type is None:
        return True
    if isinstance(exc_type, ast.Name) and exc_type.id in ('Exception', 'BaseException'):
        return True
    if isinstance(exc_type, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in ('Exception', 'BaseException')
                   for e in exc_type.elts)
    return False


def _handler_is_silent(node: ast.ExceptHandler) -> bool:
    """True when the handler swallows control flow without leaving a trace."""
    for st in ast.walk(node):
        if isinstance(st, ast.Call):
            f = st.func
            if isinstance(f, ast.Attribute) and f.attr in _LOG_ATTRS:
                return False
            if isinstance(f, ast.Name) and f.id in _LOG_NAMES:
                return False
            name = None
            if isinstance(f, ast.Name):
                name = f.id
            elif isinstance(f, ast.Attribute):
                name = f.attr
            if name and (name.startswith('api_') or name in _STRUCT_NAMES):
                return False
        if isinstance(st, ast.Raise):
            return False
    body = node.body
    if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Return, ast.Continue, ast.Break)):
        return True
    return all(isinstance(x, (ast.Pass, ast.Return, ast.Continue, ast.Break,
                              ast.Assign, ast.AugAssign)) for x in body)


def _iter_py_files():
    for root in SCAN_ROOTS:
        base = os.path.join(REPO, root)
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in EXCL_DIRS]
            for f in fn:
                if f.endswith('.py'):
                    yield os.path.join(dp, f)


def _scan() -> dict[str, list[int]]:
    """repo-relative path → sorted list of broad-silent handler line numbers."""
    hits: dict[str, list[int]] = {}
    for path in _iter_py_files():
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = os.path.relpath(path, REPO)
        for n in ast.walk(tree):
            if isinstance(n, ast.ExceptHandler) and _is_broad(n.type) and _handler_is_silent(n):
                hits.setdefault(rel, []).append(n.lineno)
    return {k: sorted(v) for k, v in hits.items()}


# ── Tests ────────────────────────────────────────────────────────────
def test_no_broad_silent_except_beyond_allowlist():
    """No file may exceed its pinned allowance of broad silent swallows."""
    hits = _scan()
    offenders = []
    for rel, lines in sorted(hits.items()):
        allowed = ALLOWED.get(rel, 0)
        if len(lines) > allowed:
            offenders.append(f'{rel}: {len(lines)} broad silent handler(s) '
                             f'at lines {lines} (allowed {allowed})')
    assert not offenders, (
        'New broad silent exception swallow(s) detected — §2.2 requires a '
        'trace (add `logger.debug(\"<why>: %s\", e)`):\n  ' + '\n  '.join(offenders))


def test_allowlist_entries_still_exist():
    """Ratchet integrity: a pinned file must not silently drop below its
    allowance (fix it → decrement ALLOWED) and must still be scannable."""
    hits = _scan()
    stale = []
    for rel, allowed in sorted(ALLOWED.items()):
        actual = len(hits.get(rel, []))
        if actual < allowed:
            stale.append(f'{rel}: allowance {allowed} but only {actual} '
                         f'remain — lower the ALLOWED entry')
    assert not stale, 'ALLOWED is now too loose:\n  ' + '\n  '.join(stale)
