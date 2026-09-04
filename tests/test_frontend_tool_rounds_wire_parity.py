"""Wire-parity gate for the retained dispatcher plus typed presenters.

Renders the 56-round battery (tests/_tool_rounds_wire_parity_rounds.json)
through the current materialized runtime graph and asserts the emitted HTML is
byte-identical to the frozen baseline
(tests/_tool_rounds_wire_parity_baseline.json).

Why this gate exists: `_renderUnifiedToolLine` is a retained ordered dispatcher
across typed and lazy presentation owners. Template-literal *indentation
inside* the HTML strings is load-bearing — a refactor that re-indents a
template changes the served markup byte-for-byte. This gate catches any such
drift, intended or not.

The baseline encodes CURRENT accepted behaviour. When a renderer change is
intentional, use this module's `_run_harness()` against the materialized owner
graph, serialize that result to `_tool_rounds_wire_parity_baseline.json`, and
review the snapshot diff. Raw retained sections are not standalone inputs
after typed-owner extraction.

Skips cleanly when node is unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Materialized migrated runtime sections (scope prelude included — the
# harness evals them whole under bare node, and the migrated sources read
# the module-private runtimeScope binding).
TOOL_ROUNDS = Path(runtime_section_path('ui/tool_rounds.js'))
TOOL_ROUNDS_RICH = Path(runtime_section_path('ui/tool_rounds_rich.js'))
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


def test_battery_covers_every_dispatch_family():
    """Guard the guard: every dispatcher family needs a representative round."""
    battery = ROUNDS.read_text(encoding='utf-8')
    # Every branch the dispatcher probes must appear in the battery by name.
    required_markers = [
        '_inboxInject',         # typed synthetic-injection presenter: swarm
        '_peerInject',          # typed synthetic-injection presenter: peer
        '_userSteerInject',     # typed synthetic-injection presenter: operator
        '_stallNudge',          # typed synthetic-injection presenter: system
        'ask_human',            # _renderHumanGuidanceRows
        'awaiting_human',       # _renderHumanGuidanceCard (live interactive card)
        'pending_approval',     # typed tool-approval presenter
        'timer_create',         # _renderTimerWaitingRow
        'awaiting_stdin',       # _renderStdinBlock
        'aborted',              # _renderAbortedRow
        'error-failed-tool',    # _renderErrorRow
        'run_command',          # typed command presenter + retained lifecycle
        'browser_execute_js',   # typed browser-execution presenter
        'search_tools',         # typed tool-catalog search presenter
        'web_search',           # _renderSearchRows (+ searching orbit)
        'inspect_image',        # typed tool-image presenter: read/inspect
        'generate_image',       # typed tool-image presenter: generate/edit
        'write_file',           # typed write-result presenter
        'apply_diff',           # typed single-diff presenter
        'apply_diffs',          # typed batch-edit presenter
        'compactionLayer',      # typed compaction presenter
        'toolTokens',           # _computeToolBadgeHtml token branch
        'project_board_read',   # _renderConvMetaBlock (rich, tool_rounds_rich.js)
        '_timerPolls',          # _renderTimerWatcherBlock (rich, tool_rounds_rich.js)
    ]
    missing = [m for m in required_markers if m not in battery]
    assert not missing, (
        f'battery is missing coverage markers: {missing} — add rounds to '
        'tests/_tool_rounds_wire_parity_rounds.json and regenerate the baseline'
    )
