# Incident anchor: born in commit 73c874b0 — test(tool-rounds): permanent wire-parity gate for _renderUnifiedToolLine
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity regression gate for _renderUnifiedToolLine (tool_rounds.js).

Renders the 41-round battery (tests/_tool_rounds_wire_parity_rounds.json)
through the CURRENT static/js/ui/tool_rounds.js and asserts the emitted HTML
is byte-identical to the frozen baseline
(tests/_tool_rounds_wire_parity_baseline.json).

Why this gate exists: _renderUnifiedToolLine is the single highest-churn
renderer in the frontend (16 branch helpers + dispatcher after the
2d7adb99 split). Template-literal *indentation inside* the HTML strings is
load-bearing — a refactor that re-indents a template changes the served
markup byte-for-byte. This gate catches any such drift, intended or not.

The baseline encodes CURRENT accepted behaviour. When a change to the
renderer is INTENTIONAL, regenerate the baseline and review its diff like
any snapshot test:

    node tests/_tool_rounds_wire_parity_harness.js \
        static/js/ui/tool_rounds.js \
        tests/_tool_rounds_wire_parity_rounds.json \
        static/js/ui/tool_rounds_rich.js \
        > tests/_tool_rounds_wire_parity_baseline.json

Skips cleanly when node is unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL_ROUNDS = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds.js'
TOOL_ROUNDS_RICH = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds_rich.js'
HARNESS = HERE / '_tool_rounds_wire_parity_harness.js'
ROUNDS = HERE / '_tool_rounds_wire_parity_rounds.json'
BASELINE = HERE / '_tool_rounds_wire_parity_baseline.json'

pytestmark = pytest.mark.unit


def _run_harness() -> list[dict]:
    if shutil.which('node') is None:
        pytest.skip('node is required for the tool_rounds wire-parity gate')
    proc = subprocess.run(
        ['node', str(HARNESS), str(TOOL_ROUNDS), str(ROUNDS), str(TOOL_ROUNDS_RICH)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f'wire-parity harness crashed (exit {proc.returncode}):\n{proc.stderr[:2000]}'
        )
    return json.loads(proc.stdout)


def test_render_unified_tool_line_matches_baseline():
    expected = json.loads(BASELINE.read_text(encoding='utf-8'))
    actual = _run_harness()
    assert len(actual) == len(expected), (
        f'round count drift: baseline={len(expected)} actual={len(actual)} — '
        'did you edit the battery without regenerating the baseline?'
    )
    diffs = []
    for exp, act in zip(expected, actual):
        if (exp.get('html') or '') != (act.get('html') or '') or (exp.get('err') or '') != (act.get('err') or ''):
            a, b = exp.get('html') or '', act.get('html') or ''
            pos = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
            diffs.append(
                f'  {exp["name"]}: first diff @char {pos}\n'
                f'    baseline: ...{a[max(0, pos - 60):pos + 60]!r}...\n'
                f'    actual:   ...{b[max(0, pos - 60):pos + 60]!r}...'
            )
    assert not diffs, (
        f'{len(diffs)}/{len(expected)} rounds render differently from the frozen baseline.\n'
        + '\n'.join(diffs[:5])
        + '\nIf this change is INTENTIONAL, regenerate the baseline (see module docstring).'
    )


def test_battery_covers_every_branch_helper():
    """Guard the guard: the battery must exercise every _render* helper the
    dispatcher can route to, so a future helper can't ship unrendered."""
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    battery = ROUNDS.read_text(encoding='utf-8')
    # every branch the dispatcher probes must appear in the battery by name
    required_markers = [
        'ask_human',            # _renderHumanGuidanceRows
        'pending_approval',     # _renderPendingApprovalBlock
        'timer_create',         # _renderTimerWaitingRow
        'awaiting_stdin',       # _renderStdinBlock
        'aborted',              # _renderAbortedRow
        'run_command',          # _renderSearchingRow + _renderCmdDoneBlock
        'browser_execute_js',   # _renderBrowserExecJsBlock
        'web_search',           # _renderSearchRows (+ searching orbit)
        'inspect_image',        # _renderReadImagesBlock
        'generate_image',       # _renderImageGenBlock
        'write_file',           # _renderWriteFileBlock
        'apply_diff',           # _renderSingleDiffBlock
        'apply_diffs',          # _renderBatchEditsBlock
        'compactionLayer',      # _renderCompactionLabel
        'toolTokens',           # _computeToolBadgeHtml token branch
        'project_board_read',   # _renderConvMetaBlock (rich, tool_rounds_rich.js)
        '_timerPolls',          # _renderTimerWatcherBlock (rich, tool_rounds_rich.js)
    ]
    missing = [m for m in required_markers if m not in battery]
    assert not missing, (
        f'battery is missing coverage markers: {missing} — add rounds to '
        'tests/_tool_rounds_wire_parity_rounds.json and regenerate the baseline'
    )
