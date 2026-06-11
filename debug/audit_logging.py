"""One-shot logging-discipline audit (read-only, no fixes).

Scans `lib/` and `routes/` and reports:
  1. Silent except blocks (no logger.* / log_exception / audit_log call inside),
     tiered by BOTH exception breadth AND what the handler body does.
  2. `print(` calls (CLAUDE.md §2.1 forbids them outside debug/).
  3. f-strings passed as the message argument to logger.* (§2.6).
  4. Modules missing `get_logger(__name__)` (§2.1).

Silent-catch tiering (most → least severe):
  A1  bare `except:` / `except BaseException` — also swallows SystemExit /
      KeyboardInterrupt. Almost always a bug.
  A2  broad `except Exception` with no log — swallows every error.
  B1  narrow data-coercion catch that DISCARDS (pure `pass`) or runs
      unlogged non-trivial logic — review: probably wants a log.
  B2  narrow data-coercion catch with a clean fallback (assign / return /
      continue / break) and no log — §2.2 wants a `logger.debug`.
  C   optional-dependency / control-flow exception (ImportError, queue.Empty,
      CancelledError, …) — legitimate, review only.

Each finding also reports the enclosing function, whether it is inside a loop,
whether the exception is bound (`as e`), and the handler's body action.

Run:  python3 debug/audit_logging.py [--limit N | --full] [--tiers A1,B1]
"""
from __future__ import annotations

import argparse
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

# Tier order + human-readable titles (most → least severe).
TIERS = ('A1', 'A2', 'B1', 'B2', 'C')
TIER_TITLES = {
    'A1': 'bare `except:` / `except BaseException` with NO log '
          '(also swallows SystemExit/KeyboardInterrupt — CRITICAL)',
    'A2': 'broad `except Exception` with NO log (HIGH)',
    'B1': 'narrow catch that DISCARDS (pass) or runs unlogged logic '
          '(MEDIUM — review)',
    'B2': 'narrow catch with unlogged fallback value (LOW — add logger.debug)',
    'C':  'optional-dep / control-flow catch (LEGITIMATE — review only)',
}
TIER_SUMMARY_HINT = {
    'A1': '← critical: catches SystemExit/KeyboardInterrupt, fix now',
    'A2': '← real bugs, fix these',
    'B1': '← review: silent discard / unlogged logic',
    'B2': '← add a debug log',
    'C':  '← legitimate',
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
# Shared context tracking: enclosing function qualname + loop nesting depth.
# ---------------------------------------------------------------------------
class _ContextVisitor(ast.NodeVisitor):
    """Base visitor that tracks the enclosing function chain and loop depth.

    Subclasses override leaf visitors (visit_ExceptHandler / visit_Call) and
    MUST call ``self.generic_visit(node)`` so the func/loop stacks stay synced.
    """

    def __init__(self):
        self._func_stack: list[str] = []
        self._loop_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_For(self, node):
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    visit_AsyncFor = visit_For  # type: ignore[assignment]
    visit_While = visit_For      # type: ignore[assignment]

    @property
    def _qualname(self) -> str:
        return '.'.join(self._func_stack) if self._func_stack else '<module>'

    @property
    def _in_loop(self) -> bool:
        return self._loop_depth > 0


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


_SIMPLE_STMT = (ast.Pass, ast.Return, ast.Continue, ast.Break,
                ast.Assign, ast.AnnAssign, ast.AugAssign)


def _classify_body_action(body: list[ast.stmt]) -> str:
    """Describe what a (log-free) except body does.

    Returns one of: 'pass', 'return', 'assign', 'continue', 'break', 'logic'.
    Only ever called on bodies that contain no log call and no raise.
    """
    if not body:
        return 'pass'
    if all(isinstance(s, ast.Pass) for s in body):
        return 'pass'
    if not all(isinstance(s, _SIMPLE_STMT) for s in body):
        return 'logic'
    has_return = any(isinstance(s, ast.Return) for s in body)
    has_assign = any(isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)) for s in body)
    has_continue = any(isinstance(s, ast.Continue) for s in body)
    has_break = any(isinstance(s, ast.Break) for s in body)
    if has_return:
        return 'return'
    if has_assign:
        return 'assign'
    if has_continue:
        return 'continue'
    if has_break:
        return 'break'
    return 'pass'


def _classify_tier(exc_names: list[str], action: str) -> str:
    """Combine exception breadth + body action into a fine-grained tier."""
    if not exc_names:
        return 'A1'  # bare except:
    if any(n == 'BaseException' for n in exc_names):
        return 'A1'
    if any(n == 'Exception' for n in exc_names):
        return 'A2'
    # All caught types are optional-dep / control-flow → legitimate.
    if all(n in (OPTIONAL_DEP_EXC | CONTROL_FLOW_EXC) for n in exc_names):
        return 'C'
    # Narrow data-coercion catch — severity depends on what the body does.
    if action in ('pass', 'logic'):
        return 'B1'
    return 'B2'


class SilentCatchVisitor(_ContextVisitor):
    def __init__(self):
        super().__init__()
        # tier → list[dict]
        self.findings: dict[str, list[dict]] = {t: [] for t in TIERS}

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
            action = _classify_body_action(node.body)
            tier = _classify_tier(names, action)
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
            self.findings[tier].append({
                'lineno': node.lineno,
                'func': self._qualname,
                'in_loop': self._in_loop,
                'bound': node.name is not None,
                'action': action,
                'exc_repr': exc_repr,
                'preview': preview,
            })
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Audit 2: print() calls
# ---------------------------------------------------------------------------
class PrintVisitor(_ContextVisitor):
    def __init__(self):
        super().__init__()
        self.findings: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == 'print':
            self.findings.append((node.lineno, self._qualname))
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Audit 3: f-string as logger.* message argument
# ---------------------------------------------------------------------------
LOG_METHODS = {'debug', 'info', 'warning', 'warn', 'error', 'critical', 'exception'}


class FStringLoggerVisitor(_ContextVisitor):
    def __init__(self):
        super().__init__()
        self.findings: list[tuple[int, str, str]] = []

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
                self.findings.append((node.lineno, self._qualname, snippet))
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
# Formatting helpers
# ---------------------------------------------------------------------------
def _format_finding(f: dict) -> str:
    """Render one silent-catch finding as a detailed two-line block."""
    flags = []
    if f['in_loop']:
        flags.append('in-loop')
    if not f['bound']:
        flags.append('unbound')
    flag_str = (' [' + ', '.join(flags) + ']') if flags else ''
    head = (f"L{f['lineno']}  in {f['func']}()  "
            f"action={f['action']}{flag_str}")
    detail = f"{f['exc_repr']}  →  {f['preview']}"
    return head, detail


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Logging-discipline audit (read-only).')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--limit', type=int, default=12,
                   help='Max findings shown per file per tier (default 12).')
    g.add_argument('--full', action='store_true',
                   help='Show every finding (no per-file truncation).')
    p.add_argument('--tiers', default='',
                   help='Comma-separated tiers to show in detail '
                        '(e.g. A1,A2,B1). Default: all. Summary always shows all.')
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    per_file_limit = None if args.full else max(1, args.limit)
    show_tiers = set(TIERS)
    if args.tiers.strip():
        requested = {t.strip().upper() for t in args.tiers.split(',') if t.strip()}
        unknown = requested - set(TIERS)
        if unknown:
            print(f'Unknown tier(s): {", ".join(sorted(unknown))}. '
                  f'Valid: {", ".join(TIERS)}', flush=True)
            return 2
        show_tiers = requested

    # Line-buffered stdout so progress is visible on slow filesystems.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    print('Discovering .py files...', flush=True)
    files = iter_py_files()
    print(f'Scanning {len(files)} files in {TARGET_DIRS} under {ROOT}\n', flush=True)

    # tier → file → list[finding dict]
    silent_by_tier: dict[str, dict[str, list[dict]]] = {t: {} for t in TIERS}
    print_by_file: dict[str, list[tuple[int, str]]] = {}
    fstr_by_file: dict[str, list[tuple[int, str, str]]] = {}
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
            missing_logger.append(rel)

    # ---- Report ----
    sep = '=' * 78

    def header(title: str):
        print(f'\n{sep}\n{title}\n{sep}')

    tier_totals = {t: sum(len(v) for v in silent_by_tier[t].values()) for t in TIERS}
    # Per-tier action breakdown (greater detail in the summary).
    action_by_tier: dict[str, Counter] = {t: Counter() for t in TIERS}
    for t in TIERS:
        for finds in silent_by_tier[t].values():
            for f in finds:
                action_by_tier[t][f['action']] += 1

    for tier in TIERS:
        if tier not in show_tiers:
            continue
        header(f'AUDIT 1 · TIER {tier} — {TIER_TITLES[tier]}')
        total = tier_totals[tier]
        files_in_tier = silent_by_tier[tier]
        breakdown = action_by_tier[tier]
        bd = ', '.join(f'{a}={n}' for a, n in breakdown.most_common()) or '—'
        print(f'Total: {total} handler(s) across {len(files_in_tier)} file(s)   '
              f'[by action: {bd}]\n')
        ranked = sorted(files_in_tier.items(), key=lambda kv: -len(kv[1]))
        for rel, finds in ranked:
            print(f'  [{len(finds):3d}] {rel}')
            shown = finds if per_file_limit is None else finds[:per_file_limit]
            for f in shown:
                head, detail = _format_finding(f)
                print(f'         {head}')
                print(f'              {detail}')
            if per_file_limit is not None and len(finds) > per_file_limit:
                print(f'         ... +{len(finds) - per_file_limit} more '
                      f'(use --full to show all)')

    header('AUDIT 2 — print() calls in lib/ + routes/')
    total_print = sum(len(v) for v in print_by_file.values())
    print(f'Total: {total_print} print() call(s) across {len(print_by_file)} file(s)\n')
    for rel, finds in sorted(print_by_file.items(), key=lambda kv: -len(kv[1])):
        print(f'  [{len(finds):3d}] {rel}')
        shown = finds if per_file_limit is None else finds[:per_file_limit]
        for ln, func in shown:
            print(f'         L{ln}: in {func}()')
        if per_file_limit is not None and len(finds) > per_file_limit:
            print(f'         ... +{len(finds) - per_file_limit} more')

    header('AUDIT 3 — f-strings passed as logger.* message')
    total_fstr = sum(len(v) for v in fstr_by_file.values())
    print(f'Total: {total_fstr} f-string log call(s) across {len(fstr_by_file)} file(s)\n')
    for rel, finds in sorted(fstr_by_file.items(), key=lambda kv: -len(kv[1])):
        print(f'  [{len(finds):3d}] {rel}')
        shown = finds if per_file_limit is None else finds[:per_file_limit]
        for ln, func, snip in shown:
            print(f'         L{ln}: in {func}()  {snip}')
        if per_file_limit is not None and len(finds) > per_file_limit:
            print(f'         ... +{len(finds) - per_file_limit} more')

    header('AUDIT 4 — Modules with funcs/classes but no get_logger / getLogger')
    print(f'Total: {len(missing_logger)} file(s)\n')
    for rel in sorted(missing_logger):
        print(f'    {rel}')

    if parse_errors:
        header('Parse errors (skipped)')
        for rel, msg in parse_errors:
            print(f'    {rel}: {msg}')

    # ---- Summary ----
    header('SUMMARY')
    for tier in TIERS:
        bd = ', '.join(f'{a}={n}' for a, n in action_by_tier[tier].most_common())
        bd_str = f'   [{bd}]' if bd else ''
        print(f'  Silent except — TIER {tier:<2} : {tier_totals[tier]:4d} '
              f'(in {len(silent_by_tier[tier])} files)  {TIER_SUMMARY_HINT[tier]}{bd_str}')
    a_total = tier_totals['A1'] + tier_totals['A2']
    print(f'  {"":>24}   (Tier A total: {a_total})')
    print(f'  print() leaks          : {total_print:4d} (in {len(print_by_file)} files)')
    print(f'  f-string logger calls  : {total_fstr:4d} (in {len(fstr_by_file)} files)')
    print(f'  Missing get_logger     : {len(missing_logger):4d} files')
    print(f'  Parse errors           : {len(parse_errors):4d}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
