"""Tool-row debug entry: geometry, single-entry, and right-edge ownership.

The defect this pins (owner-verified against a real screenshot): the debug
entry rendered as a ZERO-HEIGHT block (`.ri-tool-anchor-row{height:0}`) whose
child floated up with `margin-top:-14px`, while the since-removed
`.tc-preview-btn` claimed the same right edge with `margin-left:auto`. Two
elements each believed they owned the row's right end, so they PRINTED ON TOP
OF EACH OTHER.

2026-07-28: the "模型原文" (model-view) button was removed per owner
directive, so the debug entry is now the ONLY control in the row's right-hand
control group. The overlap pair is gone with it; what survives as a
load-bearing invariant is the structural rule behind it — exactly ONE element
owns the right edge.

Why these assertions are GEOMETRIC and run in a REAL browser: the ownership
fact is a layout fact. jsdom computes no layout, so a jsdom assertion here
would be green against any CSS whatsoever — including the broken CSS. The
suite therefore drives the real bundle and reads real `getBoundingClientRect()`s.

Covered (each an observable RESULT, not an implementation detail):
  1. ONE debug entry per row, not two (R and S merged into a single control).
  2. The entry is discoverable WITHOUT hover (it used to be `opacity:0`).
  3. Exactly ONE element in the row header owns the right edge via
     `margin-left:auto` — the structural invariant, so a future row type that
     re-adds a second `auto` fails here rather than by visibly overlapping in
     production.

NEUTER (see test_neuter_*): grant a SECOND element `margin-left:auto` and
the ownership invariant must go red. A guard that cannot detect the original
class of defect is not a guard.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = [pytest.mark.visual]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

# A synthetic settled turn with tool rounds, injected straight into the
# renderer so the suite does not depend on a live model call. `llmRound` and
# `_taskId` are present because the entry is (correctly) provenance-gated.
_SEED_JS = r"""
(() => {
  window._featureFlags = window._featureFlags || {};
  window._featureFlags.debug_mode = true;
  const rounds = [
    { roundNum: 1, llmRound: 0, toolName: 'run_command', status: 'done',
      toolContent: 'hello from the tool', _taskId: 'task-geom-1',
      toolArgs: { command: 'ls -la' },
      results: [{ title: 'run_command', snippet: 'ok' }] },
    { roundNum: 2, llmRound: 1, toolName: 'read_files', status: 'done',
      toolContent: 'file body', _taskId: 'task-geom-1',
      results: [{ title: 'read_files', snippet: 'ok' }] },
  ];
  const host = document.createElement('div');
  host.id = 'geomHost';
  // Match the production nesting so the real .ptool-* rules apply.
  host.className = 'ptool-panel';
  host.innerHTML = '<div class="ptool-panel-body">' +
    renderToolRoundsHTML(rounds, false, null) + '</div>';
  host.style.position = 'relative';
  document.body.appendChild(host);
  return host.querySelectorAll('[data-prn]').length;
})()
"""

_MEASURE_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('#geomHost [data-prn]').forEach((slot) => {
    const entries = slot.querySelectorAll('.ri-tool-anchor');
    const rect = (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    };
    const vis = (el) => {
      const cs = getComputedStyle(el);
      return cs.display !== 'none' && cs.visibility !== 'hidden' &&
             parseFloat(cs.opacity) > 0.01;
    };
    out.push({
      prn: slot.getAttribute('data-prn'),
      entries: [...entries].map((e) => ({
        r: rect(e), visible: vis(e), label: e.textContent.trim(),
      })),
      // The model-view button is gone (removed 2026-07-28); pin its absence.
      modelViews: slot.querySelectorAll('.tc-preview-btn').length,
      autoOwners: [...slot.querySelectorAll('.ptool-line > *, .ptool-line')]
        .filter((el) => getComputedStyle(el).marginLeft === 'auto')
        .map((el) => el.className),
    });
  });
  return out;
})()
"""


@pytest.fixture()
def _rows(page, live_server):
    page.goto(live_server, wait_until='domcontentloaded')
    page.wait_for_function('typeof renderToolRoundsHTML === "function"',
                           timeout=30000)
    n = page.evaluate(_SEED_JS)
    assert n >= 2, f'seed rendered {n} tool slots, expected >= 2'
    return page.evaluate(_MEASURE_JS)


def test_exactly_one_debug_entry_per_row(_rows):
    """#1 — R and S were merged into ONE control with in-panel tabs."""
    for row in _rows:
        assert len(row['entries']) == 1, (
            f"row {row['prn']}: expected exactly 1 debug entry, found "
            f"{len(row['entries'])} ({[e['label'] for e in row['entries']]})")


def test_model_view_button_is_gone(_rows):
    """The 模型原文 button was removed per owner directive (2026-07-28) —
    pin its absence so it is not re-introduced row-by-row."""
    for row in _rows:
        assert row['modelViews'] == 0, (
            f"row {row['prn']}: {row['modelViews']} model-view button(s) "
            'reappeared')


def test_debug_entry_is_discoverable_without_hover(_rows):
    """#2 — it used to be `opacity:0` until the row was hovered, which makes
    it a control the user never finds."""
    for row in _rows:
        for e in row['entries']:
            assert e['visible'], (
                f"row {row['prn']}: debug entry is not visible without hover")
            assert e['r']['h'] > 0, (
                f"row {row['prn']}: debug entry has zero height — it is an "
                f"overlay again, not a participant in the row's flow")


def test_single_owner_of_the_row_right_edge(_rows):
    """#3 — the structural invariant behind the original overlap defect."""
    for row in _rows:
        assert len(row['autoOwners']) <= 1, (
            f"row {row['prn']}: {len(row['autoOwners'])} elements claim the "
            f"right edge with margin-left:auto ({row['autoOwners']}) — "
            f"exactly one wrapper may own it")


def test_neuter_a_second_auto_owner_is_caught(page, live_server):
    """NEUTER: hand a SECOND direct child of the row header `margin-left:auto`
    (the exact class of defect that produced the overlap) and assert the
    ownership measurement flips red. Without this we cannot tell a working
    guard apart from one compatible with every possible layout."""
    page.goto(live_server, wait_until='domcontentloaded')
    page.wait_for_function('typeof renderToolRoundsHTML === "function"',
                           timeout=30000)
    page.evaluate(_SEED_JS)
    # Re-introduce the defect CLASS at runtime (never touches the shipped
    # file): the row's title text becomes a second right-edge claimant
    # alongside .ptool-row-ctl.
    page.add_style_tag(content=(
        '.ptool-line > .ptool-text{margin-left:auto !important}'
    ))
    rows = page.evaluate(_MEASURE_JS)
    caught = any(len(row['autoOwners']) > 1 for row in rows)
    assert caught, (
        'NEUTER did not surface a second margin-left:auto owner — the '
        'ownership assertion is not load-bearing and must be redesigned')


def test_shipped_css_has_no_negative_margin_overlay():
    """Ratchet on the specific broken construct, anchored to the rule body so
    a reformat of the stylesheet cannot silently retire it."""
    css = open(CSS, encoding='utf-8').read()
    m = re.search(r'\.ri-tool-anchor\{([^}]*)\}', css)
    assert m, '.ri-tool-anchor rule not found — re-anchor this guard'
    body = m.group(1)
    assert 'margin-top:-' not in body, (
        f'.ri-tool-anchor floats over the row again: {body}')
    assert not re.search(r'opacity:\s*0[;,}]', body), (
        f'.ri-tool-anchor is hover-only again (opacity:0): {body}')
    row = re.search(r'\.ri-tool-anchor-row\{([^}]*)\}', css)
    assert row and 'height:0' not in row.group(1), (
        '.ri-tool-anchor-row is a zero-height overlay again')
