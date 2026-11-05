"""Worktree-aware long-function ratchet.

This script owns the exact budgets for the repository's >=600-line reasoning
units.  Every Python file in the worktree (tracked, or untracked and not
ignored) is scanned, so a function that reaches the threshold without a
budget fails as loudly as a budgeted one that grows.  Tests call the public
audit function; the CLI is available to humans and automation without making a
test inspect shipped source text directly.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import gc
from pathlib import Path
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_LINE_THRESHOLD = 600
# Cold page-cache reads of every source dominate the audit; keeping several in
# flight while this thread parses cuts a busy host's scan roughly tenfold.
_CONCURRENT_READS = 32


def _unique_budgets(*entries: tuple[str, str, int]) -> dict[tuple[str, str], int]:
    # A dict literal would let a concurrent edit that repeats a key merge
    # cleanly and silently keep only the later value.
    budgets: dict[tuple[str, str], int] = {}
    for relative_path, function_name, lines in entries:
        if (relative_path, function_name) in budgets:
            raise ValueError(
                f'{relative_path}:{function_name} has more than one budget; '
                'keep one entry, then run python3 '
                'scripts/check_function_sizes.py to check its size')
        budgets[(relative_path, function_name)] = lines
    return budgets


# Entries are (repository-relative path, dotted name, lines); a method or
# nested function is named through its enclosing definitions ('Class.method').
# Split a function, then lower or remove its entry in the same change. Exact
# budgets make both growth and unclaimed shrinkage visible.
FUNCTION_LINE_BUDGETS = _unique_budgets(
    ('lib/tasks_pkg/turn_trace.py', '_fold_task_trace', 223),
    ('lib/tasks_pkg/cache_tracking/_detect.py', 'detect_cache_break', 378),
    ('lib/tasks_pkg/orchestrator/_finalize.py', '_finalize_and_emit_done', 392),
    ('lib/tasks_pkg/stream_handler/_analyse.py', 'analyse_stream_result', 238),
    ('lib/translate/engine/_engine.py', '_translate_one_chunk', 425),
    ('evaluations/long_agent_release/tofu_projection.py', 'project_tofu_trial', 630),
    ('lib/cost_experiments/cost_experiments.py', 'aggregate_cost_experiment_rows', 634),
    ('lib/llm/chat.py', 'chat', 590),
    ('lib/paper/report_engine/worker.py', 'run_report_task', 414),
    ('lib/tasks_pkg/compaction/_layer2/_compact.py', 'execute_compact_tool', 431),
    ('lib/tasks_pkg/compaction/_layer2/_summary.py', '_generate_query_aware_summary', 482),
    ('lib/tasks_pkg/context_composer/_providers.py', 'collect_context_blocks', 605),
    ('lib/tasks_pkg/handlers/project.py', '_handle_project_tool', 561),
    ('lib/tasks_pkg/manager/_events.py', '_append_event_ordered', 593),
    ('lib/tasks_pkg/manager/_stream.py', 'stream_llm_response', 655),
    ('lib/tasks_pkg/tool_dispatch/_parse.py', 'parse_tool_calls', 630),
    ('packages/tofu-search/tofu_search/search/orchestrator.py', '_perform_web_search_impl', 660),
    ('scripts/rootless_terminal_bench_21.py', '_classify_trial', 645),
    ('tests/storage/test_native_task_terminal.py', 'test_real_native_route_commits_application_terminal', 782),
    ('tests/tasks_pkg/test_native_application_installed.py', 'test_http_application_issues_native_root_without_task_sentinel', 615),
    ('tests/tofudb/test_tofudb_differential.py', '_turn_scripts', 1190),
    ('tests/tofudb/test_tofudb_migration_import.py', '_seed_source', 1054),
)


@dataclass(frozen=True)
class FunctionSizeAudit:
    stale_entries: tuple[str, ...]
    changed_entries: tuple[str, ...]
    unbudgeted_entries: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.stale_entries or self.changed_entries
                    or self.unbudgeted_entries)


# A definition is a statement, so it only occurs in these statement lists;
# expression subtrees, the bulk of every tree, need no traversal.
_STATEMENT_LISTS = ('body', 'orelse', 'finalbody', 'handlers', 'cases')


def _function_lengths(source: bytes, filename: str) -> dict[str, int]:
    lengths: dict[str, int] = {}
    pending: list[tuple[ast.AST, str]] = [
        (ast.parse(source, filename=filename), '')]
    while pending:
        node, prefix = pending.pop()
        for field in _STATEMENT_LISTS:
            for child in getattr(node, field, ()):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    name = prefix + child.name
                    if not isinstance(child, ast.ClassDef):
                        # A redefinition keeps the longest body, so neither
                        # copy can grow past the recorded budget unseen.
                        lengths[name] = max(
                            lengths.get(name, 0),
                            child.end_lineno - child.lineno + 1,
                        )
                    pending.append((child, name + '.'))
                else:
                    pending.append((child, prefix))
    return lengths


def _worktree_python_files(repository_root: Path) -> list[str]:
    """Tracked plus untracked, non-ignored ``.py`` paths that exist.

    A Git failure raises: an empty enumeration is never a clean audit.
    """
    listed = subprocess.run(
        ['git', 'ls-files', '-z', '--cached', '--others',
         '--exclude-standard', '--', '*.py'],
        cwd=repository_root, check=True, capture_output=True, timeout=120,
    ).stdout.decode('utf-8').split('\0')
    return sorted({
        relative for relative in listed
        if relative and (repository_root / relative).is_file()
    })


def _source_long_enough_to_measure(path: Path) -> bytes | None:
    source = path.read_bytes()
    # A function cannot span more lines than its file has.
    if source.count(b'\n') + 1 >= FUNCTION_LINE_THRESHOLD:
        return source
    return None


def _worktree_function_lengths(repository_root: Path) -> dict[str, dict[str, int]]:
    relative_paths = _worktree_python_files(repository_root)
    by_path: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=_CONCURRENT_READS) as reads:
        sources = reads.map(
            _source_long_enough_to_measure,
            [repository_root / relative_path for relative_path in relative_paths])
        for relative_path, source in zip(relative_paths, sources):
            if source is not None:
                by_path[relative_path] = _function_lengths(source, relative_path)
    return by_path


def audit_function_sizes(
    repository_root: Path = REPOSITORY_ROOT,
    budgets: dict[tuple[str, str], int] = FUNCTION_LINE_BUDGETS,
) -> FunctionSizeAudit:
    """Compare exact budgets with the current worktree, never Git ``HEAD``."""
    # The scan allocates millions of acyclic AST nodes. Inside a long-lived
    # host such as pytest, generational collections kept re-traversing them
    # and tripled the audit time; reference counting frees each tree.
    collecting = gc.isenabled()
    gc.disable()
    try:
        return _audit(repository_root, budgets)
    finally:
        if collecting:
            gc.enable()


def _audit(
    repository_root: Path,
    budgets: dict[tuple[str, str], int],
) -> FunctionSizeAudit:
    by_path = _worktree_function_lengths(repository_root)
    stale: list[str] = []
    changed: list[str] = []
    for (relative_path, function_name), budget in budgets.items():
        source_path = repository_root / relative_path
        if relative_path not in by_path and source_path.is_file():
            by_path[relative_path] = _function_lengths(
                source_path.read_bytes(), relative_path)
        actual = by_path.get(relative_path, {}).get(function_name)
        if actual is None:
            stale.append(
                f'{relative_path}:{function_name} no longer exists; delete its '
                'budget, or move the budget to the new path or name if the '
                'function was moved or renamed')
        elif actual > budget:
            changed.append(
                f'{relative_path}:{function_name} GREW: budget={budget}, '
                f'actual={actual}; split it until it has at most {budget} '
                'lines (a budget is never raised)')
        elif actual < budget:
            remedy = f'lower its budget to {actual}'
            if actual < FUNCTION_LINE_THRESHOLD:
                remedy += (', or delete the budget now that the function is '
                           f'below {FUNCTION_LINE_THRESHOLD} lines')
            changed.append(
                f'{relative_path}:{function_name} SHRANK: budget={budget}, '
                f'actual={actual}; {remedy}')
    unbudgeted = [
        f'{relative_path}:{function_name} has {length} lines '
        f'(threshold {FUNCTION_LINE_THRESHOLD}) and no budget; split it below '
        f'{FUNCTION_LINE_THRESHOLD} lines (if it was moved or renamed, move '
        'its old budget to this path and name instead)'
        for relative_path, lengths in sorted(by_path.items())
        for function_name, length in sorted(lengths.items())
        if length >= FUNCTION_LINE_THRESHOLD
        and (relative_path, function_name) not in budgets
    ]
    return FunctionSizeAudit(tuple(stale), tuple(changed), tuple(unbudgeted))


def main() -> int:
    result = audit_function_sizes()
    if result.ok:
        print('function-size ratchet OK')
        return 0
    print('function-size ratchet FAILED. Budgets are FUNCTION_LINE_BUDGETS in '
          'scripts/check_function_sizes.py; docs/TESTING_STRATEGY.md '
          '("Long-function ratchet") states the rule.')
    for message in (*result.stale_entries, *result.changed_entries,
                    *result.unbudgeted_entries):
        print(f'  {message}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
