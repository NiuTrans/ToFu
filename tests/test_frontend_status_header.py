"""Frontend — the Status tab renders a section HEADER (title + manual refresh)
and, while the backend is warming a fresh snapshot in the background, an inline
"Updating…" pill — instead of blanking the whole tab on a full-screen
"Synthesizing project status…" box.

Runs the REAL shipped `renderStatus` + `_buildStatusHeader` + `_buildSkeleton`
bodies under node with a minimal DOM stub. Asserts:
  • a header with the title + a refresh button is always present;
  • refreshing=true adds the "Updating…" pill AND disables the refresh button;
  • with a cached narrative present the narrative renders (not a spinner);
  • refreshing=true with NO snapshot yet renders a shimmer skeleton, not the
    passive "No status yet" empty line.
NEUTER: strip the `_buildStatusHeader(refreshing)` call from renderStatus → the
title/refresh header disappears (the assertion bites).
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
STATUS_JS = REPO / "frontend" / "src" / "runtime" / "sections" / "project-brain-status.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function %s\s*\(" % re.escape(name), src)
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


_HARNESS = r"""
'use strict';
function _t(key, fallback) { return fallback; }
function _esc(s) { return String(s == null ? '' : s); }
function _relTime() { return '2m ago'; }
function _triggerLabel(t) { return t || ''; }
function buildEvidence() { return _mk('div'); }
// The composer/watch/history builders are covered elsewhere — stub them so
// renderStatus's header + latest branches are what we exercise.
function _buildAskComposer() { var e = _mk('div'); e.className = 'pb-status-ask'; return e; }
function _refreshWatch() {}
function _displayedStatusPath() { return '/p'; }
function refreshStatus() {}
var Icon = function () { return '<svg></svg>'; };

function _mk(tag) {
  return {
    tag: tag, className: '', id: '', _text: '', _html: '', children: [], _attrs: {},
    disabled: false, type: '',
    set textContent(v) { this._text = v; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = v; if (v === '') this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; },
    addEventListener() {},
    querySelector() { return null; },
  };
}
var _body = _mk('div'); _body.id = 'projectBrainStatusBody';
function _statusBodyEl() { return _body; }
var document = {
  createElement: function (t) { return _mk(t); },
  createDocumentFragment: function () { return _mk('#frag'); },
  getElementById: function (id) { return id === 'projectBrainStatusBody' ? _body : null; },
  querySelector: function () { return null; },
};
var ProjectBrainI18n = { apply: function () {} };

__BUILD_HEADER__
__BUILD_SKELETON__
__RENDER_STATUS__

renderStatus(__DATA__);

var classes = [], texts = [], tags = [], disabled = [];
(function walk(n) {
  if (!n) return;
  if (n.className) classes.push(n.className);
  if (n.tag) tags.push(n.tag);
  if (n._text) texts.push(n._text);
  if (n.disabled) disabled.push(n.className);
  (n.children || []).forEach(walk);
})(_body);
console.log(JSON.stringify({ classes: classes, texts: texts, tags: tags, disabled: disabled }));
"""


def _run(render_src, header_src, skel_src, data):
    script = (_HARNESS
              .replace("__RENDER_STATUS__", render_src)
              .replace("__BUILD_HEADER__", header_src)
              .replace("__BUILD_SKELETON__", skel_src)
              .replace("__DATA__", json.dumps(data)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _srcs():
    txt = STATUS_JS.read_text()
    return (_extract_fn(txt, "renderStatus"),
            _extract_fn(txt, "_buildStatusHeader"),
            _extract_fn(txt, "_buildSkeleton"))


def test_header_present_with_cached_narrative():
    rs, hs, ks = _srcs()
    r = _run(rs, hs, ks, {"latest": {"narrative": "We are on track.", "ts": 1,
                                     "trigger": "manual", "pillar_state": {}},
                          "history": [], "maxSeq": 1, "refreshing": False})
    cls = " ".join(r["classes"])
    assert "pb-status-header" in cls and "pb-status-title" in cls, r
    assert "pb-status-refresh" in cls, r
    assert "pb-status-narrative" in cls, r
    # Not refreshing → no updating pill.
    assert "pb-status-updating" not in cls, r
    assert "We are on track." in " ".join(r["texts"]), r


def test_updating_pill_when_refreshing():
    rs, hs, ks = _srcs()
    r = _run(rs, hs, ks, {"latest": {"narrative": "Prior state.", "ts": 1,
                                     "trigger": "on_open", "pillar_state": {}},
                          "history": [], "maxSeq": 1, "refreshing": True})
    cls = " ".join(r["classes"])
    assert "pb-status-updating" in cls, r
    # The refresh button is disabled while warming.
    assert "pb-status-refresh" in " ".join(r["disabled"]), r
    # Prior narrative stays on screen (non-blocking) — NOT blanked.
    assert "Prior state." in " ".join(r["texts"]), r


def test_skeleton_on_first_open_refreshing_no_snapshot():
    rs, hs, ks = _srcs()
    r = _run(rs, hs, ks, {"latest": None, "history": [], "maxSeq": 0,
                          "refreshing": True})
    cls = " ".join(r["classes"])
    assert "pb-status-skeleton" in cls, r
    # The passive "No status yet" empty line must NOT show while warming.
    assert "pb-status-empty" not in cls, r


def test_empty_state_when_not_refreshing_and_no_snapshot():
    rs, hs, ks = _srcs()
    r = _run(rs, hs, ks, {"latest": None, "history": [], "maxSeq": 0,
                          "refreshing": False})
    cls = " ".join(r["classes"])
    assert "pb-status-empty" in cls, r
    assert "pb-status-skeleton" not in cls, r


def test_neuter_removing_header_call_drops_header():
    """NEUTER: strip the `_buildStatusHeader(refreshing)` append → the header
    (title + refresh) vanishes, proving the positive test bites."""
    rs, hs, ks = _srcs()
    neutered = re.sub(r"frag\.appendChild\(_buildStatusHeader\(refreshing\)\);",
                      "/* neutered */", rs, count=1)
    assert neutered != rs, "neuter did not strip the header append"
    r = _run(neutered, hs, ks, {"latest": {"narrative": "x", "ts": 1,
                                           "trigger": "manual", "pillar_state": {}},
                                "history": [], "maxSeq": 1, "refreshing": False})
    assert "pb-status-header" not in " ".join(r["classes"]), r


if __name__ == "__main__":
    test_header_present_with_cached_narrative()
    print("PASS header_present_with_cached_narrative")
    test_updating_pill_when_refreshing()
    print("PASS updating_pill_when_refreshing")
    test_skeleton_on_first_open_refreshing_no_snapshot()
    print("PASS skeleton_on_first_open")
    test_empty_state_when_not_refreshing_and_no_snapshot()
    print("PASS empty_state_when_not_refreshing")
    test_neuter_removing_header_call_drops_header()
    print("PASS neuter")
    print("ALL GREEN")
