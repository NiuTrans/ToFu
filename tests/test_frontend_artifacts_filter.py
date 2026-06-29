"""Regression harness for the artifacts-library filter empty-state.

WHY
---
``_filterLibrary`` in ``static/js/artifacts.js`` hides non-matching rows with
``display:none``. When a query matched NOTHING it hid every row and showed no
fallback — the library looked blank/broken (same class as the conv-list
collapse bug: a control left with zero visible items and no message). The fix
appends a ``.artifact-lib-noresult`` line when ``visible === 0``.

This test renders the real library markup, runs the real ``_filterLibrary``,
and asserts the no-match line appears for a zero-match query and disappears
again when the query clears. Skips cleanly without node + jsdom.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="artifact-panel"></div></body>',
  targets: [process.argv[2]],
});

// Build the library markup the way _renderLibrary does (search box + rows).
// _filterLibrary only needs .artifact-lib-list + .artifact-lib-row[data-art-search].
const panel = document.getElementById('artifact-panel');
panel.innerHTML =
  '<div class="artifact-lib">' +
    '<div class="artifact-lib-list">' +
      '<button class="artifact-lib-row" data-art-search="alpha report pdf"></button>' +
      '<button class="artifact-lib-row" data-art-search="beta notes html"></button>' +
    '</div>' +
  '</div>';

// _filterLibrary is module-private (inside an IIFE) in the real file; the
// frontend exposes it via window when bundled. Resolve whichever is defined.
const filt = (window.Artifacts && window.Artifacts._filterLibrary) || null;
check('filter_fn_exists', typeof filt === 'function');

function visibleRows() {
  return [...panel.querySelectorAll('.artifact-lib-row')]
    .filter(r => r.style.display !== 'none').length;
}
function noResultShown() {
  const n = panel.querySelector('.artifact-lib-noresult');
  return !!(n && n.style.display !== 'none');
}

if (typeof filt === 'function') {
  // 1. zero-match query → all rows hidden + no-result line shown
  filt('zzz-nothing-matches');
  check('zeromatch_hides_all_rows', visibleRows() === 0);
  check('zeromatch_shows_noresult', noResultShown());

  // 2. matching query → row visible, no-result hidden
  filt('alpha');
  check('match_shows_row', visibleRows() === 1);
  check('match_hides_noresult', !noResultShown());

  // 3. cleared query → all rows visible again, no-result hidden
  filt('');
  check('cleared_shows_all', visibleRows() === 2);
  check('cleared_hides_noresult', !noResultShown());
}

report();
"""


def test_artifacts_filter_shows_empty_state():
    run_harness(
        target_js=os.path.join(JS_DIR, 'artifacts.js'),
        body_js=_BODY,
        min_pass=7,
        label='artifacts filter',
    )
