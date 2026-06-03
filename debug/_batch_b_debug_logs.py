"""One-shot bulk transformation: add `logger.debug(...)` to every Tier B
silent except handler in lib/ + routes/.

For each handler that:
  * catches a "narrow data-coercion" exception set (Tier B)
  * has NO logger.* / log_exception / api_*_error / raise inside

we:
  1. ensure the exception is bound (`as <name>`); add `as _e_audit` if not
  2. insert a `logger.debug('<funcname>: %s', <name>)` line as the first
     statement of the handler body, indented to match
  3. require the file already has a module-level `logger`; skip otherwise

Run with --dry-run for a plan; without --dry-run to apply.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = ['lib', 'routes']

# Mirror audit_logging.py's classifier
LOG_CALLEE_NAMES = {
    'debug', 'info', 'warning', 'warn', 'error', 'critical', 'exception',
    'log_exception', 'audit_log', 'log_context', 'logger', 'get_logger',
    'api_internal_error', 'api_error', 'api_bad_request', 'api_not_found',
    'api_unauthorized', 'api_forbidden', 'api_conflict',
    'api_payload_too_large', 'api_method_not_allowed',
}
CONTROL_FLOW_EXC = {
    'StopIteration', 'StopAsyncIteration', 'GeneratorExit', 'KeyboardInterrupt',
    'SystemExit', 'CancelledError', 'asyncio.CancelledError', 'asyncio.TimeoutError',
    'queue.Empty', 'queue.Full', 'Empty', 'Full', '_queue.Empty',
    'BlockingIOError', 'subprocess.TimeoutExpired',
}
OPTIONAL_DEP_EXC = {'ImportError', 'ModuleNotFoundError', 'NameError'}

SKIP_DIRS = {'__pycache__', '.git', '.venv', 'node_modules', '.mypy_cache',
             '.pytest_cache', 'tests'}


def iter_py_files() -> list[Path]:
    files: list[Path] = []
    def walk(base: str):
        try:
            entries = list(os.scandir(base))
        except OSError:
            return
        for e in entries:
            if e.name.startswith('.'):
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS:
                    continue
                walk(e.path)
            elif e.is_file(follow_symlinks=False) and e.name.endswith('.py'):
                files.append(Path(e.path))
    for d in TARGET_DIRS:
        base = str(ROOT / d)
        if os.path.isdir(base):
            walk(base)
    return sorted(files)


def _exc_names(node: ast.expr | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        out: list[str] = []
        for elt in node.elts:
            out.extend(_exc_names(elt))
        return out
    try:
        return [ast.unparse(node)]
    except Exception:
        return ['<?>']


def _classify(exc_names: list[str]) -> str:
    if not exc_names:
        return 'A'
    if any(n in {'Exception', 'BaseException'} for n in exc_names):
        return 'A'
    if all(n in (OPTIONAL_DEP_EXC | CONTROL_FLOW_EXC) for n in exc_names):
        return 'C'
    return 'B'


def _has_log(body: list[ast.stmt]) -> bool:
    for n in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in LOG_CALLEE_NAMES:
                return True
            if isinstance(f, ast.Name) and f.id in LOG_CALLEE_NAMES:
                return True
        if isinstance(n, ast.Raise):
            return True
    return False


def _enclosing_func_name(tree: ast.Module, target_node: ast.AST) -> str:
    """Walk the tree, find the innermost FunctionDef/AsyncFunctionDef whose
    line range contains target_node. Returns its name, or '<module>'."""
    name = '<module>'
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = parent.lineno
            end = getattr(parent, 'end_lineno', start)
            if start <= target_node.lineno <= end:
                name = parent.name
                # Continue — inner nested funcs override.
    return name


def _has_module_logger(tree: ast.Module) -> bool:
    """Module has a top-level assignment `logger = get_logger(...)` or
    `logger = logging.getLogger(...)`."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == 'logger':
                    return True
    return False


def _split_except_header(line: str) -> tuple[str, str, str] | None:
    """Parse `<indent>except <exc_spec>[ as NAME]:[<trailing>]` on a single line.

    Returns (indent, header_before_colon, trailing_after_colon) or None
    if not parseable. Tolerant of trailing comments after the colon.
    """
    m = re.match(r'^(\s*)except\b(.*?):(.*?)$', line)
    if not m:
        return None
    indent, before, trailing = m.group(1), m.group(2), m.group(3)
    return indent, before, trailing


_AS_RE = re.compile(r'\bas\s+([A-Za-z_][A-Za-z_0-9]*)\s*$')


def _ensure_as_binding(header_before: str, fallback_name: str) -> tuple[str, str]:
    """If header lacks `as NAME`, append ` as <fallback_name>`. Returns
    (new_header, name_to_use)."""
    stripped = header_before.rstrip()
    m = _AS_RE.search(stripped)
    if m:
        return header_before, m.group(1)
    # No binding — append.
    return stripped + f' as {fallback_name}', fallback_name


def collect_targets(path: Path) -> list[dict]:
    """Return a list of edit specs for Tier B handlers without log calls."""
    src = path.read_text(encoding='utf-8')
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []
    if not _has_module_logger(tree):
        return []  # Skip files without a top-level `logger`.
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        names = _exc_names(node.type)
        if _classify(names) != 'B':
            continue
        if _has_log(node.body):
            continue
        # Need positions: the except keyword line and the first-body line.
        # node.lineno = line of `except`. node.body[0].lineno = first body stmt.
        if not node.body:
            continue
        body0 = node.body[0]
        funcname = _enclosing_func_name(tree, node)
        out.append({
            'except_line': node.lineno,            # 1-based
            'body_first_line': body0.lineno,        # 1-based
            'body_col': body0.col_offset,
            'has_name': node.name is not None,
            'name': node.name or '',
            'funcname': funcname,
            'exc_repr': ', '.join(names),
        })
    return out


def apply_edits(path: Path, targets: list[dict], dry_run: bool) -> int:
    """Apply edits bottom-up so earlier line numbers stay valid.
    Returns the count of edits actually made."""
    if not targets:
        return 0
    src = path.read_text(encoding='utf-8')
    lines = src.splitlines(keepends=True)
    # Sort by except_line descending — we mutate from bottom to top.
    targets_sorted = sorted(targets, key=lambda t: -t['except_line'])
    applied = 0
    for spec in targets_sorted:
        ex_line_idx = spec['except_line'] - 1  # 0-based
        body_line_idx = spec['body_first_line'] - 1
        if ex_line_idx < 0 or body_line_idx < 0:
            continue
        ex_line = lines[ex_line_idx]
        parsed = _split_except_header(ex_line)
        if parsed is None:
            # Multi-line except header — skip (rare for narrow types).
            continue
        indent, header_before, trailing = parsed
        new_header, varname = _ensure_as_binding(
            header_before, fallback_name='_e_audit',
        )
        # Build the new except line.
        if new_header == header_before:
            new_ex_line = ex_line  # already had a binding; no rewrite
        else:
            new_ex_line = f'{indent}except{new_header}:{trailing}'
            if not new_ex_line.endswith('\n'):
                new_ex_line += '\n'
        # Build the new debug-log line.
        body_indent = ' ' * spec['body_col']
        funcname = spec['funcname']
        log_line = (
            f"{body_indent}logger.debug('[{path.stem}] {funcname} "
            f"caught %s: %s', type({varname}).__name__, {varname})\n"
        )
        # If the except header is still a multi-statement line
        # (`except X: pass` on one line), the body_first_line equals
        # the except line. Skip those — too risky to split.
        if body_line_idx == ex_line_idx:
            continue
        # Apply: replace except line, then insert log line BEFORE body line.
        if new_ex_line != ex_line:
            lines[ex_line_idx] = new_ex_line
        lines.insert(body_line_idx, log_line)
        applied += 1
    if applied and not dry_run:
        path.write_text(''.join(lines), encoding='utf-8')
    return applied


def main():
    dry_run = '--dry-run' in sys.argv
    files = iter_py_files()
    print(f'Scanning {len(files)} files (dry_run={dry_run})...', flush=True)

    total_targets = 0
    total_applied = 0
    files_changed = 0
    skipped_no_logger: list[Path] = []
    plan_lines: list[str] = []

    for f in files:
        # Quick skip if no logger
        try:
            src_head = f.read_text(encoding='utf-8')
        except Exception:
            continue
        try:
            tree = ast.parse(src_head, filename=str(f))
        except SyntaxError:
            continue
        if not _has_module_logger(tree):
            # Only flag files that actually have Tier B handlers.
            targets = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    if _classify(_exc_names(node.type)) == 'B' and not _has_log(node.body):
                        targets.append(node)
            if targets:
                skipped_no_logger.append(f)
            continue
        targets = collect_targets(f)
        if not targets:
            continue
        total_targets += len(targets)
        plan_lines.append(f'  {f.relative_to(ROOT)}: {len(targets)} sites')
        for t in targets[:3]:
            plan_lines.append(
                f'    L{t["except_line"]} except {t["exc_repr"]} '
                f'(in {t["funcname"]})'
            )
        if len(targets) > 3:
            plan_lines.append(f'    ... +{len(targets) - 3} more')
        applied = apply_edits(f, targets, dry_run=dry_run)
        if applied:
            files_changed += 1
            total_applied += applied

    print('\n'.join(plan_lines))
    print()
    print(f'Total Tier B targets    : {total_targets}')
    print(f'Total sites edited      : {total_applied}')
    print(f'Files changed           : {files_changed}')
    if skipped_no_logger:
        print(f'Files SKIPPED (no module-level logger): {len(skipped_no_logger)}')
        for f in skipped_no_logger:
            print(f'    {f.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
