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


class _SilentCatchFinder(ast.NodeVisitor):
    """AST visitor that finds except blocks with no logging."""

    def __init__(self):
        self.issues: list[tuple[int, str]] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        body = node.body
        is_silent = False
        if len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, (ast.Pass, ast.Return, ast.Continue)):
                is_silent = True

        if is_silent:
            has_log = False
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Attribute) and func.attr in (
                        'debug', 'info', 'warning', 'error', 'critical', 'exception'
                    ):
                        has_log = True
                        break
            if not has_log:
                # Check for acceptable patterns:
                # 1. Exception class definitions (pass in class body) — not caught here since those are ClassDef
                # 2. Truly expected harmless: OSError in /proc walking, encoding fallback loops
                exc_type = ''
                if node.type:
                    if isinstance(node.type, ast.Name):
                        exc_type = node.type.id
                    elif isinstance(node.type, ast.Tuple):
                        exc_type = ','.join(
                            getattr(e, 'id', '?') for e in node.type.elts
                        )
                self.issues.append((node.lineno, exc_type))

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
        ('lib/compat.py', 83), ('lib/compat.py', 127), ('lib/compat.py', 133),
        ('lib/compat.py', 135), ('lib/compat.py', 160), ('lib/compat.py', 170),
        ('lib/compat.py', 227),
        # Encoding fallback loops — continue to try next encoding
        ('lib/doc_parser.py', 679), ('lib/file_reader.py', 304),
        # /proc comm read — process may exit between checks, harmless
        ('lib/project_mod/tools.py', 1053),
        # /proc walking — processes exit between checks, completely harmless
        ('lib/project_mod/tools.py', 1056), ('lib/project_mod/tools.py', 1083),
        # stdin pipe inode fstat — may fail if pipe not ready
        ('lib/project_mod/tools.py', 1134),
        # select on fds — fd may already be closed
        ('lib/project_mod/tools.py', 1163),
        # Pipe I/O in non-blocking mode — BlockingIOError is expected
        ('lib/project_mod/tools.py', 1176), ('lib/project_mod/tools.py', 1185),
        ('lib/project_mod/tools.py', 1191),
        # proc.stdin.close() — expected OSError during cleanup
        ('lib/project_mod/tools.py', 1217),
        # proc.kill() — process may have already exited
        ('lib/project_mod/tools.py', 1238),
        # fd.close() in finally — harmless cleanup
        ('lib/project_mod/tools.py', 1248),
        # proc.wait TimeoutExpired — kill and wait in cleanup
        ('lib/project_mod/tools.py', 1252),
        # os.stat in snapshot loop — files may vanish during walk
        ('lib/project_mod/tools.py', 810),
        # bytes decode fallback — keep as raw bytes if not valid text
        ('lib/project_mod/tools.py', 1579),
        # grep count parsing — non-numeric lines in grep -c output
        ('lib/project_mod/read_tools.py', 406),
        # Cross-DC probe — FileNotFoundError is the EXPECTED outcome (measuring latency)
        ('lib/cross_dc.py', 238),
        # project_error_tracker — standalone module, JSON parse-or-skip in log parsing
        ('lib/project_error_tracker.py', 257),
        # daily_report — filename parsing in directory listing loop
        ('routes/daily_report.py', 1281),
    }

    def test_no_silent_catches_in_lib(self):
        """All except blocks in lib/ must log something."""
        violations = self._scan(LIB_DIR, 'lib')
        if violations:
            msg = f'{len(violations)} silent catch(es) found:\n'
            msg += '\n'.join(f'  {f}:{line} except {exc}' for f, line, exc in violations)
            pytest.fail(msg)

    def test_no_silent_catches_in_routes(self):
        """All except blocks in routes/ must log something."""
        violations = self._scan(ROUTES_DIR, 'routes')
        if violations:
            msg = f'{len(violations)} silent catch(es) found:\n'
            msg += '\n'.join(f'  {f}:{line} except {exc}' for f, line, exc in violations)
            pytest.fail(msg)

    def _scan(self, directory: Path, prefix: str) -> list[tuple[str, int, str]]:
        violations = []
        for path in _py_files(directory):
            rel = str(path.relative_to(PROJECT_ROOT))
            try:
                source = path.read_text(encoding='utf-8')
                tree = ast.parse(source, rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
            finder = _SilentCatchFinder()
            finder.visit(tree)
            for lineno, exc_type in finder.issues:
                if (rel, lineno) not in self.ACCEPTABLE:
                    violations.append((rel, lineno, exc_type))
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
        for path in _py_files(directory):
            rel = str(path.relative_to(PROJECT_ROOT))
            try:
                source = path.read_text(encoding='utf-8')
                tree = ast.parse(source, rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
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
