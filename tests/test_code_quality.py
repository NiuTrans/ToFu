"""tests/test_code_quality.py — Automated code quality enforcement.

Ensures that CLAUDE.md logging standards are maintained:
- No silent exception catches (except blocks without logging)
- No f-strings in logger calls
- All lib/ and routes/ .py files use lib.log.get_logger (not raw logging)
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = PROJECT_ROOT / 'lib'
ROUTES_DIR = PROJECT_ROOT / 'routes'

# Files that are allowed to use raw `import logging` for valid reasons:
# - lib/log.py: IS the logging module
# - lib/__init__.py: loaded before lib.log may be ready
# - lib/_pkg_utils.py: needs `import logging` for type hints (Logger param)
# - lib/version.py: loaded at import-time before lib.log may exist
# - lib/project_error_tracker.py: standalone universal module, no lib.log dependency
# - lib/fetch/utils.py: uses `import logging as _logging` to silence urllib3
# - lib/compat.py: platform detection, loaded very early
RAW_LOGGING_ALLOWLIST = {
    'lib/log.py',
    'lib/__init__.py',
    'lib/_pkg_utils.py',
    'lib/version.py',
    'lib/project_error_tracker.py',
    'lib/fetch/utils.py',
    'lib/compat.py',
}


import functools


def _py_files(*dirs: Path):
    """Yield all .py files under the given directories."""
    for d in dirs:
        if not d.exists():
            continue
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [s for s in subdirs if s != '__pycache__']
            for f in files:
                if f.endswith('.py'):
                    yield Path(root) / f


@functools.lru_cache(maxsize=None)
def _parsed_trees(directory: Path) -> tuple[tuple[str, ast.Module], ...]:
    """Walk + read + parse every .py file under ``directory`` ONCE.

    Cached across tests so the 7 checks don't each re-walk and re-parse the
    whole tree — a big win on slow FUSE mounts. Returns ``(rel_path, tree)``
    tuples; files that fail to read/parse are skipped.
    """
    out: list[tuple[str, ast.Module]] = []
    for path in _py_files(directory):
        rel = str(path.relative_to(PROJECT_ROOT))
        try:
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source, rel)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        out.append((rel, tree))
    return tuple(out)


# Names that count as "the failure was handled" inside an except block.
# Includes lib.api_response error helpers — they communicate the failure
# outward to the HTTP client (NOT silent) and api_internal_error also
# auto-logs at ERROR with traceback (CLAUDE.md §4.6.2).
_LOG_OR_HANDLE_NAMES = frozenset({
    'debug', 'info', 'warning', 'error', 'critical', 'exception',
    'log_exception', 'audit_log',
    'api_internal_error', 'api_error', 'api_bad_request',
    'api_not_found', 'api_unauthorized', 'api_forbidden',
    'api_conflict', 'api_payload_too_large', 'api_method_not_allowed',
})


def _handler_has_log_or_raise(node: ast.ExceptHandler) -> bool:
    """True if the except body logs, calls an api_* error helper, or re-raises."""
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr in _LOG_OR_HANDLE_NAMES:
                return True
            if isinstance(func, ast.Name) and func.id in _LOG_OR_HANDLE_NAMES:
                return True
    return False


def _exc_type_str(node: ast.ExceptHandler) -> str:
    """Human-readable caught-exception spec (comma-joined for tuples)."""
    if node.type is None:
        return '<bare>'
    t = node.type
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    if isinstance(t, ast.Tuple):
        return ','.join(getattr(e, 'id', getattr(e, 'attr', '?')) for e in t.elts)
    return '?'


def _body_action(body: list[ast.stmt]) -> str:
    """Describe a log-free except body: pass / return / assign / continue /
    break / logic (anything more complex)."""
    if not body or all(isinstance(s, ast.Pass) for s in body):
        return 'pass'
    simple = (ast.Pass, ast.Return, ast.Continue, ast.Break,
              ast.Assign, ast.AnnAssign, ast.AugAssign)
    if not all(isinstance(s, simple) for s in body):
        return 'logic'
    for kind, label in ((ast.Return, 'return'),
                        ((ast.Assign, ast.AnnAssign, ast.AugAssign), 'assign'),
                        (ast.Continue, 'continue'),
                        (ast.Break, 'break')):
        if any(isinstance(s, kind) for s in body):
            return label
    return 'pass'


class _ContextMixin(ast.NodeVisitor):
    """Tracks the enclosing function qualname + loop nesting for richer reports."""

    def __init__(self):
        self._func_stack: list[str] = []
        self._loop_depth = 0

    def visit_FunctionDef(self, node):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node):
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    visit_AsyncFor = visit_For
    visit_While = visit_For

    @property
    def _qualname(self) -> str:
        return '.'.join(self._func_stack) if self._func_stack else '<module>'

    def _record(self, node: ast.ExceptHandler):
        """Build a detailed finding dict for a silent except handler."""
        flags = []
        if self._loop_depth > 0:
            flags.append('in-loop')
        if node.name is None:
            flags.append('unbound')
        return {
            'lineno': node.lineno,
            'exc': _exc_type_str(node),
            'func': self._qualname,
            'action': _body_action(node.body),
            'flags': flags,
        }


def _fmt_violation(rel: str, f: dict) -> str:
    """Render a finding as a single detailed line for a failure message."""
    flag_str = (' [' + ', '.join(f['flags']) + ']') if f['flags'] else ''
    return (f"  {rel}:{f['lineno']}  except {f['exc']}  "
            f"in {f['func']}()  action={f['action']}{flag_str}")


def _render_violations(label: str, violations: list[tuple[str, dict]]) -> str:
    """Build a detailed pytest.fail message, grouped by body action.

    Each line carries file:line, caught exception type, enclosing function,
    the body action (pass/return/assign/…), and flags (in-loop / unbound) so
    the failure points straight at the fix without re-grepping.
    """
    from collections import Counter
    by_action: Counter = Counter(f['action'] for _, f in violations)
    summary = ', '.join(f'{a}={n}' for a, n in by_action.most_common())
    lines = [f'{len(violations)} {label} found [by action: {summary}]:']
    for rel, f in sorted(violations, key=lambda rv: (rv[0], rv[1]['lineno'])):
        lines.append(_fmt_violation(rel, f))
    return '\n'.join(lines)


class _SilentCatchFinder(_ContextMixin):
    """AST visitor that finds except blocks (single pass/return/continue body)
    with no logging."""

    _LOG_OR_HANDLE_NAMES = _LOG_OR_HANDLE_NAMES

    def __init__(self):
        super().__init__()
        self.issues: list[dict] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        body = node.body
        is_silent = (len(body) == 1
                     and isinstance(body[0], (ast.Pass, ast.Return, ast.Continue)))
        if is_silent and not _handler_has_log_or_raise(node):
            if not _all_exc_exempt(_exc_type_str(node)):
                self.issues.append(self._record(node))
        self.generic_visit(node)


# Exception types whose silent catch is a legitimate control-flow /
# optional-dependency branch, not a swallowed error. Catching ONLY these and
# discarding (pass / fallback) needs no log — mirrors the audit's TIER C.
_EXEMPT_EXC_TYPES = frozenset({
    'ImportError', 'ModuleNotFoundError', 'NameError', 'StopIteration',
    'StopAsyncIteration', 'GeneratorExit', 'KeyboardInterrupt', 'SystemExit',
    'CancelledError', 'TimeoutError', 'Empty', 'Full',
    'BlockingIOError', 'TimeoutExpired',
})
# Back-compat alias (older references).
_ASSIGN_EXEMPT_EXC_TYPES = _EXEMPT_EXC_TYPES


def _all_exc_exempt(exc_str: str) -> bool:
    """True iff EVERY caught type in ``exc_str`` is a control-flow / optional-dep
    type (so a silent catch is legitimate). A bare ``except:`` ('<bare>') or any
    broad/narrow-data type makes the whole handler non-exempt."""
    parts = [p.strip() for p in exc_str.split(',') if p.strip()]
    if not parts:
        return False
    return all(p in _EXEMPT_EXC_TYPES for p in parts)


class _AssignSilentCatchFinder(_ContextMixin):
    """Find except blocks whose body only assigns a fallback value (or
    pass/return/continue/break) with NO logging — the ``body = ''`` class of
    silent swallow that the statement-shape check in _SilentCatchFinder misses.

    Optional-dep / control-flow exception types (see _ASSIGN_EXEMPT_EXC_TYPES)
    are skipped: there, ``x = default`` is a legitimate branch, not an error
    that went unlogged.
    """

    def __init__(self):
        super().__init__()
        self.issues: list[dict] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        body = node.body
        only_assign_or_flow = all(
            isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                           ast.Pass, ast.Return, ast.Continue, ast.Break))
            for s in body
        )
        if only_assign_or_flow and not _handler_has_log_or_raise(node):
            if not _all_exc_exempt(_exc_type_str(node)):
                self.issues.append(self._record(node))
        self.generic_visit(node)


class _FStringLoggerFinder(ast.NodeVisitor):
    """AST visitor that finds f-strings used in logger method calls."""

    LOGGER_METHODS = {'debug', 'info', 'warning', 'error', 'critical', 'exception'}

    def __init__(self):
        self.issues: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in self.LOGGER_METHODS):
            # Check if first positional arg is an f-string
            if node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.JoinedStr):
                    self.issues.append((node.lineno, func.attr))
        self.generic_visit(node)


# ─── Tests ───────────────────────────────────────────────


class TestSilentCatches:
    """No except blocks should silently swallow exceptions without logging."""

    # Known acceptable silent catches (file:line pairs) — these are truly
    # harmless expected exceptions where logging would be pure noise.
    # Each entry must have a comment explaining why it's acceptable.
    ACCEPTABLE = {
        # Platform detection — expected to fail on non-Linux
        ('lib/compat.py', 83), ('lib/compat.py', 112), ('lib/compat.py', 139),
        ('lib/compat.py', 145), ('lib/compat.py', 147), ('lib/compat.py', 172),
        ('lib/compat.py', 182), ('lib/compat.py', 239),
        # Encoding fallback loops — continue to try next encoding
        ('lib/doc_parser.py', 724), ('lib/file_reader.py', 305),
        # /proc comm read — process may exit between checks, harmless
        ('lib/project_mod/tools.py', 1056),
        # /proc walking — processes exit between checks, completely harmless
        ('lib/project_mod/tools.py', 1059), ('lib/project_mod/tools.py', 1086),
        # stdin pipe inode fstat — may fail if pipe not ready
        ('lib/project_mod/tools.py', 1137),
        # select on fds — fd may already be closed
        ('lib/project_mod/tools.py', 1166),
        # Pipe I/O in non-blocking mode — BlockingIOError is expected
        ('lib/project_mod/tools.py', 1179), ('lib/project_mod/tools.py', 1188),
        ('lib/project_mod/tools.py', 1194),
        # proc.stdin.close() — expected OSError during cleanup
        ('lib/project_mod/tools.py', 1220),
        # proc.kill() — process may have already exited
        ('lib/project_mod/tools.py', 1241),
        # fd.close() in finally — harmless cleanup
        ('lib/project_mod/tools.py', 1251),
        # proc.wait TimeoutExpired — kill and wait in cleanup
        ('lib/project_mod/tools.py', 1255),
        # os.stat in snapshot loop — files may vanish during walk
        ('lib/project_mod/tools.py', 810),
        # bytes decode fallback — keep as raw bytes if not valid text
        ('lib/project_mod/tools.py', 1598),
        # grep count parsing — non-numeric lines in grep -c output
        ('lib/project_mod/read_tools.py', 486),
        # Cross-DC probe — FileNotFoundError is the EXPECTED outcome (measuring latency)
        ('lib/cross_dc.py', 238),
        # Cross-DC probe — OSError when mount point is inaccessible
        ('lib/cross_dc.py', 241),
        # Temp file cleanup on failure — file may already be gone
        ('lib/project_mod/modifications.py', 36),
        # project_error_tracker — standalone module, parse-or-skip in log/JSON parsing
        ('lib/project_error_tracker.py', 257), ('lib/project_error_tracker.py', 292),
        # doc_parser — date format fallback for xls cells
        ('lib/doc_parser.py', 509),
        # tool_display — URL parse fallback
        ('lib/tasks_pkg/tool_display.py', 56),
        # safe_route.wrapper: `except Exception: return _handle(e)` — _handle
        # routes to api_internal_error (auto-logs ERROR+traceback per §4.6.2)
        # or api_bad_request. Not visible through the local _handle indirection.
        ('lib/api_response.py', 296), ('lib/api_response.py', 304),
    }

    def test_no_silent_catches_in_lib(self):
        """All except blocks in lib/ must log something."""
        violations = self._scan(LIB_DIR)
        if violations:
            pytest.fail(_render_violations('silent catch(es)', violations))

    def test_no_silent_catches_in_routes(self):
        """All except blocks in routes/ must log something."""
        violations = self._scan(ROUTES_DIR)
        if violations:
            pytest.fail(_render_violations('silent catch(es)', violations))

    def _scan(self, directory: Path) -> list[tuple[str, dict]]:
        violations = []
        for rel, tree in _parsed_trees(directory):
            finder = _SilentCatchFinder()
            finder.visit(tree)
            for f in finder.issues:
                if (rel, f['lineno']) not in self.ACCEPTABLE:
                    violations.append((rel, f))
        return violations


class TestAssignmentSilentCatches:
    """No except block may swallow an error by only assigning a fallback
    value (e.g. ``body = ''``) without logging.

    Complements TestSilentCatches, which only inspects pass/return/continue
    single-statement bodies. Optional-dep / control-flow exception types are
    exempt (see _ASSIGN_EXEMPT_EXC_TYPES).
    """

    # Genuinely-legit assignment-only catches (file:line). Each needs a reason.
    ACCEPTABLE = {
        # safe_route._handle(e) routes to api_internal_error (auto-logs ERROR
        # + traceback per CLAUDE.md §4.6.2) or api_bad_request — not silent.
        ('lib/api_response.py', 296), ('lib/api_response.py', 304),
        # entry_points().get(...) fallback selects the Python <3.10 API shape
        # — control-flow, not an error swallow.
        ('lib/llm_dispatch/provider_registry.py', 171),
        ('lib/tools/registry.py', 594),
    }

    def test_no_assignment_silent_catches_in_lib(self):
        violations = self._scan(LIB_DIR)
        if violations:
            pytest.fail(_render_violations(
                'assignment-only silent catch(es) (add a logger.debug or '
                'narrow the except)', violations))

    def test_no_assignment_silent_catches_in_routes(self):
        violations = self._scan(ROUTES_DIR)
        if violations:
            pytest.fail(_render_violations(
                'assignment-only silent catch(es) (add a logger.debug or '
                'narrow the except)', violations))

    def _scan(self, directory: Path) -> list[tuple[str, dict]]:
        violations = []
        for rel, tree in _parsed_trees(directory):
            finder = _AssignSilentCatchFinder()
            finder.visit(tree)
            for f in finder.issues:
                if (rel, f['lineno']) not in self.ACCEPTABLE:
                    violations.append((rel, f))
        return violations


class TestNoFStringInLoggerCalls:
    """Logger calls must use %-style formatting, not f-strings."""

    def test_no_fstrings_in_lib(self):
        violations = self._scan(LIB_DIR)
        if violations:
            msg = f'{len(violations)} f-string logger call(s) found:\n'
            msg += '\n'.join(f'  {f}:{line} logger.{method}(f"...")' for f, line, method in violations)
            pytest.fail(msg)

    def test_no_fstrings_in_routes(self):
        violations = self._scan(ROUTES_DIR)
        if violations:
            msg = f'{len(violations)} f-string logger call(s) found:\n'
            msg += '\n'.join(f'  {f}:{line} logger.{method}(f"...")' for f, line, method in violations)
            pytest.fail(msg)

    def _scan(self, directory: Path) -> list[tuple[str, int, str]]:
        violations = []
        for rel, tree in _parsed_trees(directory):
            finder = _FStringLoggerFinder()
            finder.visit(tree)
            for lineno, method in finder.issues:
                violations.append((rel, lineno, method))
        return violations


class TestLoggerStandardization:
    """All .py files in lib/ (except allowlist) must use lib.log.get_logger."""

    def test_no_raw_logging_getlogger(self):
        """Files should not use `logging.getLogger(__name__)` for their module logger."""
        violations = []
        pattern = re.compile(r'^\s*_?logger\s*=\s*logging\.getLogger\(', re.MULTILINE)

        for path in _py_files(LIB_DIR):
            rel = str(path.relative_to(PROJECT_ROOT))
            if rel in RAW_LOGGING_ALLOWLIST:
                continue
            try:
                source = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            if pattern.search(source):
                violations.append(rel)

        if violations:
            msg = f'{len(violations)} file(s) use raw logging.getLogger instead of lib.log.get_logger:\n'
            msg += '\n'.join(f'  {f}' for f in sorted(violations))
            pytest.fail(msg)
