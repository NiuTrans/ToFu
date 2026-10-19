"""Auto-fixed badge renders the repair CATEGORY chips, not just the verb.

The backend ``_repaired`` descriptor (lib/tasks_pkg/tool_dispatch/_repair.py)
carries machine-readable ``patterns`` naming the class of model defect that
was coerced (truncated JSON, stringified arg, wrong tool name, …). The badge
used to show only the bare "auto-fixed" label; the chips beside it now answer
WHAT kind of repair ran.

Pins:
  1. Each distinct pattern renders a labelled chip; the truncated-JSON class
     (``json_repaired``) is chipped too — it used to live only in ``detail``.
  2. Duplicate coercions collapse to one chip; overflow beyond three folds
     into a "+N" chip listing the rest on hover.
  3. A still-failed repair downgrades the chips to the amber "--warn" variant
     alongside the "fix attempted" badge.
  4. No patterns → the bare badge, no chips.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
HERE = Path(__file__).resolve().parent
TOOL_ROUNDS = Path(runtime_section_path('ui/tool_rounds.js'))
HARNESS = HERE.parent / '_tool_rounds_wire_parity_harness.js'


def _render(rounds: list[dict], tmp_path: Path) -> list[str]:
    if shutil.which('node') is None:
        pytest.skip('node is required for the tool-round renderer')
    fixture = tmp_path / 'rounds.json'
    fixture.write_text(json.dumps(rounds), encoding='utf-8')
    process = subprocess.run(
        ['node', str(HARNESS), str(TOOL_ROUNDS), str(fixture)],
        capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return [row['html'] for row in json.loads(process.stdout)]


def _done_round(repaired: dict, *, write_ok=None) -> dict:
    # run_command renders through the command card, which never mounts the
    # repaired badge; read_files settles through the generic result viewer,
    # whose header carries ``repairedBadgeHtml`` (tool-result-presentation.ts).
    meta = {}
    if write_ok is not None:
        meta['writeOk'] = write_ok
    return {
        '_name': 'repaired',
        'status': 'done',
        'toolName': 'read_files',
        'query': 'Read 1 file: panel.ts',
        'toolContent': 'const x = 1;',
        '_repaired': repaired,
        'results': [meta],
        'roundNum': 7,
    }


def test_each_pattern_renders_a_labelled_chip(tmp_path: Path):
    (html,) = _render([_done_round({
        'label': 'auto-fixed',
        'detail': 'recovered malformed JSON; timeout: coerced string to number/bool',
        'patterns': ['json_repaired', 'stringified_primitive'],
    })], tmp_path)

    assert 'ptool-badge-repaired' in html
    assert 'auto-fixed' in html
    # The truncated-JSON class is chipped, not buried in the tooltip.
    assert html.count('ptool-repair-kind') >= 2
    assert '>recovered JSON</code>' in html
    assert '>coerced primitive</code>' in html
    # Each chip carries its machine token on hover.
    assert 'title="json_repaired"' in html
    assert 'title="stringified_primitive"' in html


def test_duplicate_patterns_collapse_and_overflow_folds(tmp_path: Path):
    (html,) = _render([_done_round({
        'label': 'auto-fixed',
        'detail': 'a: parsed stringified JSON; b: parsed stringified JSON; '
                  'c: dropped null optional; d: applied schema default; '
                  'e: renamed wrong-harness arg key',
        'patterns': [
            'stringified_json', 'stringified_json',
            'null_omission', 'schema_default', 'param_alias',
        ],
    })], tmp_path)

    # Four distinct kinds (the duplicate stringified_json collapses).
    assert html.count('>parsed string JSON</code>') == 1
    # Three shown, the remaining one folds into "+1".
    assert '>+1</code>' in html
    # The folded kind is listed on the +N chip tooltip.
    assert 'renamed arg' in html


def test_unknown_pattern_falls_back_to_raw_token(tmp_path: Path):
    (html,) = _render([_done_round({
        'label': 'auto-fixed',
        'detail': 'x: future repair class',
        'patterns': ['future_repair_class'],
    })], tmp_path)

    # A pattern the frontend has no label for still renders its raw token.
    assert '>future_repair_class</code>' in html


def test_still_failed_repair_downgrades_chips_to_warn(tmp_path: Path):
    (html,) = _render([_done_round({
        'label': 'auto-fixed',
        'detail': 'content: parsed stringified JSON',
        'patterns': ['stringified_json'],
    }, write_ok=False)], tmp_path)

    assert 'fix attempted' in html
    assert 'ptool-repair-kind--warn' in html
    assert '>parsed string JSON</code>' in html


def test_no_patterns_renders_bare_badge(tmp_path: Path):
    (html,) = _render([_done_round({
        'label': 'auto-fixed', 'detail': '', 'patterns': [],
    })], tmp_path)

    assert 'ptool-badge-repaired' in html
    assert 'ptool-repair-kind' not in html
