"""One-shot logging-discipline audit (read-only, no fixes).

Scans `lib/` and `routes/` and reports:
  1. Silent except blocks (no logger.* / log_exception / audit_log call inside).
  2. `print(` calls (CLAUDE.md §2.1 forbids them outside debug/).
  3. f-strings passed as the message argument to logger.* (§2.6).
  4. Modules missing `get_logger(__name__)` (§2.1).

Run:  python3 debug/audit_logging.py
"""
from __future__ import annotations

import ast
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = ['lib', 'routes']

# Names that count as "logging happened" inside an except block.
# Note: api_* error helpers are included not because they log, but because they
# communicate the failure outward to the client — the failure isn't silently
# swallowed. For api_internal_error specifically, it ALSO auto-logs at ERROR
# with traceback (CLAUDE.md §4.6.2).
LOG_CALLEE_NAMES = {
    'debug', 'info', 'warning', 'warn', 'error', 'critical', 'exception',
    'log_exception', 'audit_log', 'log_context', 'logger', 'get_logger',
    'api_internal_error', 'api_error', 'api_bad_request', 'api_not_found',
    'api_unauthorized', 'api_forbidden', 'api_conflict',
    'api_payload_too_large', 'api_method_not_allowed',
}
# Re-raise / propagate also counts (caller will log).
RAISE_OK = True

# Exceptions that are part of normal control flow — silently catching them is fine.
CONTROL_FLOW_EXC = {
    'StopIteration', 'StopAsyncIteration', 'GeneratorExit', 'KeyboardInterrupt',
    'SystemExit', 'CancelledError', 'asyncio.CancelledError', 'asyncio.TimeoutError',
    'queue.Empty', 'queue.Full', 'Empty', 'Full', '_queue.Empty',
    'BlockingIOError', 'subprocess.TimeoutExpired',
}

# Exceptions that almost always indicate "optional dependency missing".
OPTIONAL_DEP_EXC = {'ImportError', 'ModuleNotFoundError', 'NameError'}

# "Narrow" data-coercion exceptions where a default-value fallback is fine.
NARROW_DATA_EXC = {
    'ValueError', 'TypeError', 'IndexError', 'KeyError', 'AttributeError',
    'json.JSONDecodeError', 'JSONDecodeError', 'UnicodeDecodeError',
    'LookupError', 'FileNotFoundError', 'PermissionError', 'OSError',
    'ProcessLookupError',
}


def iter_py_files() -> list[Path]:
    """Walk TARGET_DIRS via os.scandir (faster on FUSE than Path.rglob)."""
    files: list[Path] = []
    skip_dir_names = {'__pycache__', '.git', '.venv', 'node_modules', '.mypy_cache', '.pytest_cache',
                      # CLI test harnesses: print()-based, broad-catch is by design.
                      'tests'}

    def walk(base: str):
        try:
            with os.scandir(base) as it:
                entries = list(it)
        except OSError as e:
            print(f'  scandir {base}: {e}', flush=True)
            return
        for entry in entries:
            name = entry.name
            if name.startswith('.'):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name in skip_dir_names:
                        continue
                    walk(entry.path)
                elif entry.is_file(follow_symlinks=False) and name.endswith('.py'):
                    files.append(Path(entry.path))
            except OSError:
                continue

    for d in TARGET_DIRS:
        base = str(ROOT / d)
        if os.path.isdir(base):
            walk(base)
    return sorted(files)


# ---------------------------------------------------------------------------
# Audit 1: silent except blocks
# ---------------------------------------------------------------------------
def _exc_names(node: ast.expr | None) -> list[str]:
    """Extract caught exception names from an except clause's type spec."""
    if node is None:
        return []  # bare except
    if isinstance(node, ast.Tuple):
        out: list[str] = []
        for elt in node.elts:
            out.extend(_exc_names(elt))
        return out
    try:
        return [ast.unparse(node)]
    except Exception:
        return ['<?>']


def _classify_handler(exc_names: list[str]) -> str:
    """Return 'A' (broad/dangerous), 'B' (narrow), or 'C' (legitimate)."""
    if not exc_names:
        return 'A'  # bare except:
    # Broad catches → tier A
    if any(n in {'Exception', 'BaseException'} for n in exc_names):
        return 'A'
    # All optional-dep / control-flow → tier C
    if all(n in (OPTIONAL_DEP_EXC | CONTROL_FLOW_EXC) for n in exc_names):
        return 'C'
    # Otherwise narrow data-coercion → tier B
    return 'B'


class SilentCatchVisitor(ast.NodeVisitor):
    def __init__(self):
        # tier → list[(lineno, desc)]
        self.findings: dict[str, list[tuple[int, str]]] = {'A': [], 'B': [], 'C': []}

    @staticmethod
    def _has_log_call(body: list[ast.stmt]) -> bool:
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in LOG_CALLEE_NAMES:
                    return True
                if isinstance(func, ast.Name) and func.id in LOG_CALLEE_NAMES:
                    return True
            if RAISE_OK and isinstance(node, ast.Raise):
                return True
        return False

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if not self._has_log_call(node.body):
            names = _exc_names(node.type)
            tier = _classify_handler(names)
            if node.type is None:
                exc_repr = 'except:'
            else:
                try:
                    exc_repr = 'except ' + ast.unparse(node.type) + ':'
                except Exception:
                    exc_repr = 'except <?>:'
            first = node.body[0] if node.body else None
            preview = ''
            if first is not None:
                try:
                    preview = ast.unparse(first).splitlines()[0][:80]
                except Exception:
                    preview = type(first).__name__
            self.findings[tier].append((node.lineno, f'{exc_repr}  →  {preview}'))
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Audit 2: print() calls
# ---------------------------------------------------------------------------
class PrintVisitor(ast.NodeVisitor):
    def __init__(self):
        self.findings: list[int] = []

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == 'print':
            self.findings.append(node.lineno)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Audit 3: f-string as logger.* message argument
# ---------------------------------------------------------------------------
LOG_METHODS = {'debug', 'info', 'warning', 'warn', 'error', 'critical', 'exception'}


class FStringLoggerVisitor(ast.NodeVisitor):
    def __init__(self):
        self.findings: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):
        func = node.func
        is_logger_call = False
        if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
            # Heuristic: callee object name contains 'log' (logger / self.logger / _logger / log)
            base = func.value
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id.lower()
            elif isinstance(base, ast.Attribute):
                base_name = base.attr.lower()
            if base_name and ('log' in base_name):
                is_logger_call = True

        if is_logger_call and node.args:
            msg = node.args[0]
            if isinstance(msg, ast.JoinedStr):  # f-string
                try:
                    snippet = ast.unparse(msg)[:80]
                except Exception:
                    snippet = '<f-string>'
                self.findings.append((node.lineno, snippet))
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Audit 4: missing get_logger(__name__)
# ---------------------------------------------------------------------------
def has_get_logger(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == 'get_logger':
                return True
            if isinstance(func, ast.Attribute) and func.attr == 'get_logger':
                return True
        # Allow `logging.getLogger(__name__)` too
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'getLogger':
                return True
    return False


def file_is_trivial(tree: ast.Module) -> bool:
    """A file is 'trivial' if it has no functions/classes — e.g. pure constants
    or a re-export shim. Such files don't need a logger."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    # Line-buffered stdout so progress is visible on slow filesystems.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    print('Discovering .py files...', flush=True)
    files = iter_py_files()
    print(f'Scanning {len(files)} files in {TARGET_DIRS} under {ROOT}\n', flush=True)

    # tier → file → list[(lineno, desc)]
    silent_by_tier: dict[str, dict[str, list[tuple[int, str]]]] = {'A': {}, 'B': {}, 'C': {}}
    print_by_file: dict[str, list[int]] = {}
    fstr_by_file: dict[str, list[tuple[int, str]]] = {}
    missing_logger: list[str] = []

    parse_errors: list[tuple[str, str]] = []

    for idx, path in enumerate(files, 1):
        if idx % 25 == 0 or idx == len(files):
            print(f'  ... {idx}/{len(files)}', flush=True)
        rel = str(path.relative_to(ROOT))
        try:
            src = path.read_text(encoding='utf-8')
        except Exception as e:
            parse_errors.append((rel, f'read failed: {e}'))
            continue
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError as e:
            parse_errors.append((rel, f'syntax error: {e}'))
            continue

        sv = SilentCatchVisitor()
        sv.visit(tree)
        for tier, finds in sv.findings.items():
            if finds:
                silent_by_tier[tier][rel] = finds

        pv = PrintVisitor()
        pv.visit(tree)
        if pv.findings:
            print_by_file[rel] = pv.findings

        fv = FStringLoggerVisitor()
        fv.visit(tree)
        if fv.findings:
            fstr_by_file[rel] = fv.findings

        if not has_get_logger(tree) and not file_is_trivial(tree):
            # Skip __init__.py shims that only re-export (no funcs/classes already filters most)
            missing_logger.append(rel)

    # ---- Report ----
    sep = '=' * 78

    def header(title: str):
        print(f'\n{sep}\n{title}\n{sep}')

    tier_titles = {
        'A': 'TIER A — broad except Exception / bare except: with NO log call (HIGHEST PRIORITY)',
        'B': 'TIER B — narrow data-coercion catches with NO log call (LOW PRIORITY — §2.2 says debug-level OK)',
        'C': 'TIER C — optional-dep / control-flow catches with NO log call (LEGITIMATE — review only)',
    }
    tier_totals = {t: sum(len(v) for v in silent_by_tier[t].values()) for t in 'ABC'}

    for tier in 'ABC':
        header(f'AUDIT 1{tier} — {tier_titles[tier]}')
        total = tier_totals[tier]
        files_in_tier = silent_by_tier[tier]
        print(f'Total: {total} handlers across {len(files_in_tier)} files\n')
        ranked = sorted(files_in_tier.items(), key=lambda kv: -len(kv[1]))
        for rel, finds in ranked:
            print(f'  [{len(finds):3d}] {rel}')
            for ln, desc in finds[:8]:
                print(f'         L{ln}: {desc}')
            if len(finds) > 8:
                print(f'         ... +{len(finds) - 8} more')

    header('AUDIT 2 — print() calls in lib/ + routes/')
    total_print = sum(len(v) for v in print_by_file.values())
    print(f'Total: {total_print} print() calls across {len(print_by_file)} files\n')
    for rel, lines in sorted(print_by_file.items(), key=lambda kv: -len(kv[1])):
        print(f'  [{len(lines):3d}] {rel}  (lines: {", ".join(str(l) for l in lines[:10])}'
              + (f' +{len(lines) - 10} more' if len(lines) > 10 else '') + ')')

    header('AUDIT 3 — f-strings passed as logger.* message')
    total_fstr = sum(len(v) for v in fstr_by_file.values())
    print(f'Total: {total_fstr} f-string log calls across {len(fstr_by_file)} files\n')
    for rel, finds in sorted(fstr_by_file.items(), key=lambda kv: -len(kv[1])):
        print(f'  [{len(finds):3d}] {rel}')
        for ln, snip in finds[:8]:
            print(f'         L{ln}: {snip}')
        if len(finds) > 8:
            print(f'         ... +{len(finds) - 8} more')

    header('AUDIT 4 — Modules with funcs/classes but no get_logger / getLogger')
    print(f'Total: {len(missing_logger)} files\n')
    for rel in sorted(missing_logger):
        print(f'    {rel}')

    if parse_errors:
        header('Parse errors (skipped)')
        for rel, msg in parse_errors:
            print(f'    {rel}: {msg}')

    # ---- Summary ----
    header('SUMMARY')
    print(f'  Silent except — TIER A : {tier_totals["A"]:4d} (in {len(silent_by_tier["A"])} files)  ← real bugs, fix these')
    print(f'  Silent except — TIER B : {tier_totals["B"]:4d} (in {len(silent_by_tier["B"])} files)  ← add debug log')
    print(f'  Silent except — TIER C : {tier_totals["C"]:4d} (in {len(silent_by_tier["C"])} files)  ← legitimate')
    print(f'  print() leaks          : {total_print:4d} (in {len(print_by_file)} files)')
    print(f'  f-string logger calls  : {total_fstr:4d} (in {len(fstr_by_file)} files)')
    print(f'  Missing get_logger     : {len(missing_logger):4d} files')
    print(f'  Parse errors           : {len(parse_errors):4d}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
