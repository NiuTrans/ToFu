"""Long-function ratchet born from the 2026-08-13 repository audit incident.

The audit found that file splitting had hidden five 860–1,039 line functions:
the module names looked modular, but the actual reasoning units were still
monoliths. This pins the current worst functions so they may shrink but cannot
grow. A function that shrinks makes the baseline intentionally loose and turns
this test red until the recorded budget is lowered.

NEUTER: increasing one function budget or appending statements inside one of
these functions must fail the tightness or growth assertion respectively.
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]

# (path, function) -> exact current line budget. This list is deliberately
# limited to the measured >=600-line tail so the test stays millisecond-cheap.
# Split a function, then lower/remove its entry in the same change.
_FUNCTION_LINE_BUDGETS = {
    ('lib/tasks_pkg/tool_dispatch/_pipeline.py', 'execute_tool_pipeline'): 886,
    ('lib/tasks_pkg/cache_tracking/_detect.py', 'detect_cache_break'): 966,
    ('lib/tasks_pkg/manager/_sync.py', '_sync_result_to_conversation'): 920,
    ('lib/tasks_pkg/orchestrator/_finalize.py', '_finalize_and_emit_done'): 840,
    ('lib/tasks_pkg/stream_handler/_analyse.py', 'analyse_stream_result'): 826,
    ('lib/tasks_pkg/endpoint/_run.py', 'run_endpoint_task'): 704,
    ('lib/llm_dispatch/api.py', 'dispatch_stream'): 676,
    ('lib/tasks_pkg/manager/_stream.py', 'stream_llm_response'): 659,
    ('lib/translate/engine/_engine.py', '_translate_one_chunk'): 666,
    ('lib/tasks_pkg/llm_fallback/_call.py', '_llm_call_with_fallback'): 608,
}


def _function_lengths(path: str) -> dict[str, int]:
    source = subprocess.check_output(
        ['git', 'show', f'HEAD:{path}'], cwd=_ROOT, text=True)
    tree = ast.parse(source, filename=path)
    return {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_long_function_budgets_are_tight_and_never_grow():
    by_path: dict[str, dict[str, int]] = {}
    stale = []
    changed = []
    for (path, function), budget in _FUNCTION_LINE_BUDGETS.items():
        lengths = by_path.setdefault(path, _function_lengths(path))
        actual = lengths.get(function)
        if actual is None:
            stale.append(f'{path}:{function} no longer exists')
        elif actual != budget:
            direction = 'GREW' if actual > budget else 'SHRANK'
            changed.append(
                f'{path}:{function} {direction}: budget={budget}, actual={actual}')

    assert not stale, (
        'Function-size ratchet has stale entries; remove them only after the '
        'function was genuinely split:\n  ' + '\n  '.join(stale))
    assert not changed, (
        'Long-function budget changed. Growth is forbidden; shrinkage is good '
        'but must lower the baseline in this same file so the earned headroom '
        'cannot be silently spent later:\n  ' + '\n  '.join(changed))
