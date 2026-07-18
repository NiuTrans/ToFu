"""Frontend — the Status tab must re-fetch on SELECTION (decision #4:
fresh-on-tab-open, not merely fresh-on-panel-open).

Switching Project Brain tabs only toggles CSS visibility. Before the fix,
`_refreshStatus` fired only when the whole panel OPENED or on project-switch —
so opening the panel on Charter, sitting there while siblings finished epics,
then clicking Status showed a stale snapshot. `_selectTab` now calls
`_onTabSelected(name, prev)`, which re-fetches the status lane ONLY when
switching INTO the status tab (prev !== 'status'), never on an unrelated tab
click. Cheap: the backend staleness gate returns the cached snapshot with no
LLM on a quiescent project.

Runs the REAL shipped `_selectTab` + `_onTabSelected` bodies under node with a
`_refreshStatus` spy + a minimal DOM. The biting NEUTER strips the
`_onTabSelected(name, prev)` call from `_selectTab` and proves a Status-tab
click then triggers NO refresh (the stale-status bug).
"""
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRAIN_JS = REPO / "static" / "js" / "project-brain.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(async\s+)?function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found"
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


# Minimal DOM: tabs + panels carrying data-pb-tab / data-pb-panel so the real
# _selectTab class-toggle loop runs; _refreshStatus is a spy; _displayedProjectPath
# and the i18n overlay are stubbed. We drive a sequence of _selectTab() calls and
# print the recorded refresh invocations.
_HARNESS = r"""
'use strict';
const _refreshCalls = [];
const _state = { tab: __START_TAB__, path: '/proj/x' };
function _refreshStatus(path) { _refreshCalls.push(path); }
function _displayedProjectPath() { return '/proj/fallback'; }
const ProjectBrainI18n = { applyAll() {} };

function _el(tag, attrs) {
  const a = attrs || {};
  return {
    _cls: {},
    getAttribute(k) { return a[k] === undefined ? null : a[k]; },
    setAttribute() {},
    classList: {
      toggle(_c, _on) {},
    },
  };
}
const _tabNames = ['charter', 'board', 'activity', 'peers', 'status'];
const _tabs = _tabNames.map(n => _el('button', { 'data-pb-tab': n }));
const _panels = _tabNames.map(n => _el('div', { 'data-pb-panel': n }));
const document = {
  querySelectorAll(sel) {
    if (sel.indexOf('.pb-tab') >= 0 && sel.indexOf('panel') < 0) return _tabs;
    if (sel.indexOf('pb-tab-panel') >= 0) return _panels;
    return [];
  },
};

__SELECT_TAB__
__ON_TAB_SELECTED__

__SEQUENCE__
console.log(JSON.stringify({ refreshCalls: _refreshCalls }));
"""


def _run(select_src, on_selected_src, sequence, start_tab="charter"):
    script = (_HARNESS
              .replace("__SELECT_TAB__", select_src)
              .replace("__ON_TAB_SELECTED__", on_selected_src)
              .replace("__START_TAB__", json.dumps(start_tab))
              .replace("__SEQUENCE__", sequence))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _src():
    txt = BRAIN_JS.read_text()
    return _extract_fn(txt, "_selectTab"), _extract_fn(txt, "_onTabSelected")


def test_selecting_status_tab_refreshes():
    """Clicking INTO Status (from Charter) fires a fresh _refreshStatus."""
    sel, on_sel = _src()
    r = _run(sel, on_sel, "_selectTab('status');")
    assert r["refreshCalls"] == ["/proj/x"], r


def test_unrelated_tab_click_does_not_refresh():
    """Selecting a non-status tab must NOT trigger the status re-fetch."""
    sel, on_sel = _src()
    r = _run(sel, on_sel, "_selectTab('board'); _selectTab('activity');")
    assert r["refreshCalls"] == [], r


def test_reselecting_status_does_not_double_refresh():
    """Already on Status, clicking Status again is a no-op (prev===name)."""
    sel, on_sel = _src()
    r = _run(sel, on_sel, "_selectTab('status');", start_tab="status")
    assert r["refreshCalls"] == [], r


def test_switch_away_then_back_refreshes_again():
    """Status → Charter → Status re-fetches each time we re-enter Status —
    this is exactly the drift case: content may have changed while away."""
    sel, on_sel = _src()
    r = _run(sel, on_sel, "_selectTab('status'); _selectTab('charter'); _selectTab('status');")
    assert r["refreshCalls"] == ["/proj/x", "/proj/x"], r


def test_falls_back_to_displayed_path_when_state_path_empty():
    """When _state.path is empty, the refresh uses _displayedProjectPath()."""
    sel, on_sel = _src()
    r = _run(sel, on_sel, "_state.path=''; _selectTab('status');")
    assert r["refreshCalls"] == ["/proj/fallback"], r


def test_neuter_removing_on_tab_selected_call_stops_refresh():
    """NEUTER: strip the `_onTabSelected(name, prev);` call from _selectTab →
    a Status-tab click triggers NO refresh (the stale-status bug returns)."""
    sel, on_sel = _src()
    neutered = re.sub(r"_onTabSelected\(name, prev\);", "/* neutered */", sel, count=1)
    assert neutered != sel and "_onTabSelected(name, prev);" not in neutered, "neuter did not strip the call"
    r = _run(neutered, on_sel, "_selectTab('status');")
    assert r["refreshCalls"] == [], f"neutered _selectTab must not refresh: {r}"


if __name__ == "__main__":
    test_selecting_status_tab_refreshes()
    print("PASS selecting_status_tab_refreshes")
    test_unrelated_tab_click_does_not_refresh()
    print("PASS unrelated_tab_click_does_not_refresh")
    test_reselecting_status_does_not_double_refresh()
    print("PASS reselecting_status_does_not_double_refresh")
    test_switch_away_then_back_refreshes_again()
    print("PASS switch_away_then_back_refreshes_again")
    test_falls_back_to_displayed_path_when_state_path_empty()
    print("PASS falls_back_to_displayed_path")
    test_neuter_removing_on_tab_selected_call_stops_refresh()
    print("PASS neuter_removing_on_tab_selected_call_stops_refresh")
    print("ALL GREEN")
