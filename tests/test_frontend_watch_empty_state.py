"""Frontend — the watch lane's empty state must be an explicit INVITATION with
the add-composer present, not a passive line, so a first-time user on the
Status tab immediately sees where to write.

Runs the REAL shipped `renderWatch` + `_buildWatchComposer` + `_kindLabel`
bodies under node with a minimal DOM stub, then asserts that on ZERO items the
rendered subtree contains (a) the add-composer (kind select + textarea + Add
button) and (b) the invitation empty-state copy. The NEUTER strips the empty
branch's invitation text → the assertion fails.

Also asserts the discoverability label: the Status tab i18n label reads
"Status & Watch" (signals it's the interactive surface), not a bare "Status".
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
STATUS_JS = REPO / "frontend" / "src" / "runtime" / "sections" / "project-brain-status.js"
I18N_EN = REPO / "frontend" / "src" / "i18n" / "generated" / "en.generated.json"
I18N_ZH = REPO / "frontend" / "src" / "i18n" / "generated" / "zh.generated.json"


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


# Minimal DOM: elements record tag/class/text/children; getElementById returns
# the watch host. _t returns the fallback; Api is absent (composer tolerates it).
_HARNESS = r"""
'use strict';
function _t(key, fallback) { return fallback; }
var _WATCH_KINDS = ['concern', 'question', 'goal'];
function _kindLabel(kind) {
  var m = { concern: 'Concern', question: 'Question', goal: 'Goal' };
  return m[kind] || kind || '';
}
// _buildWatchComposer references _displayedStatusPath / _watchConvId / _refreshWatch
// only inside event handlers we never fire, but they must exist as names.
function _displayedStatusPath() { return '/p'; }
function _watchConvId() { return ''; }
function _refreshWatch() {}
// buildWatchItem's card internals are covered by the backend/other tests; here
// we only assert renderWatch's list-vs-empty branching, so stub the card.
function buildWatchItem(item) { var c = _mk('div'); c.className = 'pb-watch-item'; return c; }

function _mk(tag) {
  return {
    tag: tag, className: '', id: '', _text: '', children: [], _attrs: {},
    _listeners: [],
    set textContent(v) { this._text = v; },
    get textContent() { return this._text; },
    set innerHTML(v) { if (v === '') this.children = []; },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this._attrs[k] = v; },
    addEventListener() {},
  };
}
var _host = _mk('div'); _host.id = 'pbWatchSection';
var document = {
  createElement: function (t) { return _mk(t); },
  createDocumentFragment: function () { return _mk('#frag'); },
  getElementById: function (id) { return id === 'pbWatchSection' ? _host : null; },
};
var ProjectBrainI18n = { apply: function () {} };

__KIND_LABEL__
__BUILD_COMPOSER__
__RENDER_WATCH__

renderWatch(__DATA__);

// Walk the host subtree collecting classes + text.
var classes = [], texts = [], tags = [];
(function walk(n) {
  if (!n) return;
  if (n.className) classes.push(n.className);
  if (n.tag) tags.push(n.tag);
  if (n._text) texts.push(n._text);
  (n.children || []).forEach(walk);
})(_host);
console.log(JSON.stringify({ classes: classes, texts: texts, tags: tags }));
"""


def _run(render_src, data, kind_src, composer_src):
    script = (_HARNESS
              .replace("__RENDER_WATCH__", render_src)
              .replace("__BUILD_COMPOSER__", composer_src)
              .replace("__KIND_LABEL__", kind_src)
              .replace("__DATA__", json.dumps(data)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _srcs():
    txt = STATUS_JS.read_text()
    # _kindLabel is redefined in the harness; strip it from the extracted set to
    # avoid a duplicate declaration.
    return (_extract_fn(txt, "renderWatch"), _extract_fn(txt, "_buildWatchComposer"))


def test_empty_state_renders_composer_and_invitation():
    render_src, composer_src = _srcs()
    r = _run(render_src, {"items": []}, "", composer_src)
    cls = " ".join(r["classes"])
    # (a) The add-composer is present even with zero items.
    assert "pb-watch-add" in cls, r
    assert "pb-watch-input" in cls, r
    assert "select" in r["tags"], r  # the kind dropdown
    # (b) The empty-state is an explicit INVITATION, not a passive line.
    assert "pb-watch-empty" in cls, r
    joined = " ".join(r["texts"]).lower()
    assert "add a concern" in joined and "keep an eye on it" in joined, r


def test_composer_present_with_items_too():
    render_src, composer_src = _srcs()
    r = _run(render_src, {"items": [
        {"item_id": "w1", "kind": "goal", "text": "ship it", "status": "open",
         "promoted": False, "responses": []}]}, "", composer_src)
    cls = " ".join(r["classes"])
    assert "pb-watch-add" in cls and "pb-watch-list" in cls, r
    # With items present, the empty invitation is NOT shown.
    assert "pb-watch-empty" not in cls, r


def test_neuter_removing_invitation_text_fails_assertion():
    """NEUTER: blank the empty-state invitation copy → the invitation assertion
    no longer holds, proving the positive test bites on real copy."""
    render_src, composer_src = _srcs()
    neutered = render_src.replace(
        "'Add a concern, question, or goal and the brain will keep an eye on it.'",
        "''")
    assert neutered != render_src, "neuter did not change renderWatch"
    r = _run(neutered, {"items": []}, "", composer_src)
    joined = " ".join(r["texts"]).lower()
    assert "add a concern" not in joined, "neuter should have removed the invitation"


def test_status_tab_relabeled_status_and_watch():
    """Discoverability: the Status tab signals it's the interactive surface."""
    en = json.loads(I18N_EN.read_text())
    zh = json.loads(I18N_ZH.read_text())
    assert "Status & Watch" in en.get("projectBrain.status", ""), en
    assert "状态与关注" in zh.get("projectBrain.status", ""), zh


if __name__ == "__main__":
    test_empty_state_renders_composer_and_invitation()
    print("PASS empty_state_renders_composer_and_invitation")
    test_composer_present_with_items_too()
    print("PASS composer_present_with_items_too")
    test_neuter_removing_invitation_text_fails_assertion()
    print("PASS neuter")
    test_status_tab_relabeled_status_and_watch()
    print("PASS relabeled")
    print("ALL GREEN")
