"""Worktree-aware long-function ratchet.

This script owns the exact budgets for the repository's >=600-line reasoning
units.  Tests call the public audit function; the CLI is available to humans
and automation without making a test inspect shipped source text directly.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Split a function, then lower or remove its entry in the same change. Exact
# budgets make both growth and unclaimed shrinkage visible.
FUNCTION_LINE_BUDGETS = {
    ('lib/tasks_pkg/tool_dispatch/_pipeline.py',
     'execute_tool_pipeline'): 1076,
    ('lib/tasks_pkg/cache_tracking/_detect.py',
     'detect_cache_break'): 976,
    ('lib/tasks_pkg/orchestrator/_finalize.py',
     '_finalize_and_emit_done'): 655,
    ('lib/tasks_pkg/stream_handler/_analyse.py',
     'analyse_stream_result'): 712,
    ('lib/llm_dispatch/_api_stream.py',
     'dispatch_stream'): 689,
    ('lib/translate/engine/_engine.py',
     '_translate_one_chunk'): 676,
}


@dataclass(frozen=True)
class FunctionSizeAudit:
    stale_entries: tuple[str, ...]
    changed_entries: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.stale_entries and not self.changed_entries


def _function_lengths(path: Path) -> dict[str, int]:
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    return {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def audit_function_sizes(
    repository_root: Path = REPOSITORY_ROOT,
    budgets: dict[tuple[str, str], int] = FUNCTION_LINE_BUDGETS,
) -> FunctionSizeAudit:
    """Compare exact budgets with the current worktree, never Git ``HEAD``."""
    by_path: dict[str, dict[str, int]] = {}
    stale: list[str] = []
    changed: list[str] = []
    for (relative_path, function_name), budget in budgets.items():
        source_path = repository_root / relative_path
        if not source_path.is_file():
            stale.append(f'{relative_path}:{function_name} no longer exists')
            continue
        lengths = by_path.setdefault(
            relative_path,
            _function_lengths(source_path),
        )
        actual = lengths.get(function_name)
        if actual is None:
            stale.append(f'{relative_path}:{function_name} no longer exists')
            continue
        if actual != budget:
            direction = 'GREW' if actual > budget else 'SHRANK'
            changed.append(
                f'{relative_path}:{function_name} {direction}: '
                f'budget={budget}, actual={actual}'
            )
    return FunctionSizeAudit(tuple(stale), tuple(changed))


def main() -> int:
    result = audit_function_sizes()
    if result.ok:
        print('function-size ratchet OK')
        return 0
    for message in (*result.stale_entries, *result.changed_entries):
        print(message)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
