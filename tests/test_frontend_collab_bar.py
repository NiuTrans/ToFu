"""jsdom regression for the Project Collaboration Bar (presence.js).

The bar replaces the old "who's working" strip. It is a SINGLE project-scoped
cluster that LEADS with a plain "Project" label and follows with the
action-ordered coordination counts (decisions awaiting the human first) and the
per-peer "advancing «epic»" joins. The Pillar #7 status headline
(summary.statusLine) is deliberately NOT surfaced on the bar — a one-line
narrative truncated to fit carries no useful signal; the full snapshot lives in
the Project Brain Status tab. The per-conversation influence lens is NOT
duplicated here either — it lives inside the Project Brain panel — so there is
no second "conv cluster" and no CollabBar.setConvSegment hook.

This mounts the REAL index.html `#presenceStrip` element (renders-into-null
guard), drives presence.js's `CollabBar` test hooks with a summary + peer set,
and asserts:
  • the "N decisions awaiting you" segment renders (action-ordered count);
  • an online peer that owns an epic renders "advancing «epic title»";
  • the plain "Project" label leads (.collab-label) and NO status headline
    (.collab-status) is rendered; the conv cluster is GONE (no
    .collab-cluster-conv / .conv-inf-lead / divider, no setConvSegment);
  • clicking the bar calls openProjectBrain().

Source-level NCs: no-op the peer→epic render branch → the "advancing epic"
assertion FAILS; neuter the empty-hide gate → a count-less bar stays visible.
Shipped file byte-identical afterward.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_PRESENCE_SRC = os.path.join(JS_DIR, 'presence.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_strip_fragment():
    """Pull the REAL #presenceStrip element from shipped index.html."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    needle = 'id="presenceStrip"'
    i = html.find(needle)
    assert i != -1, 'presenceStrip not found in index.html'
    start = html.rfind('<div', 0, i)
    end = html.find('</div>', i) + len('</div>')
    assert start != -1 and end > start, 'could not bound the presenceStrip element'
    return html[start:end]


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.t = global.t = (k, params) => {
  // Echo with interpolation so assertions can see the real numbers/titles.
  if (params && typeof params === 'object') {
    let s = k;
    for (const key of Object.keys(params)) s += ' ' + params[key];
    return s;
  }
  return k;
};
// Displayed conversation → project /proj/real.
win.activeConvId = global.activeConvId = 'c-self';
win.getActiveConv = global.getActiveConv = () => ({ id: 'c-self', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};   // no live wiring in this test
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
// openProjectBrain spy.
let _opened = 0;
win.openProjectBrain = global.openProjectBrain = () => { _opened++; };
// No Api needed — we drive the summary via the CollabBar test hook.
win.Api = global.Api = { project: {} };

eval(fs.readFileSync(SRC, 'utf8'));  // presence.js (the collaboration bar)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const CB = win.CollabBar;
check('collab_hooks_exposed', !!(CB && typeof CB._render === 'function'));
const stripEl = win.document.getElementById('presenceStrip');
check('real_strip_element_exists', !!stripEl);

// Drive a summary: 1 pending decision, 1 epic in progress, and an online peer
// advancing that epic; plus the peer set (one OTHER conversation online).
CB._setSummary('/proj/real', {
  epicsOpen: 2, epicsClaimed: 1, epicsDone: 0, pendingDecisions: 1,
  activePeers: 1, charterExists: true,
  statusLine: 'Parser refactor is in flight; on track with the north star.',
  peerEpics: { 'c-worker': 'Refactor the parser' },
});
CB._setPeers('/proj/real', ['c-worker']);
CB._render();

const html = stripEl.innerHTML;
// Pillar #7 status headline is intentionally NOT surfaced on the bar: no
// .collab-status slot, and the snapshot narrative text never appears (even
// though statusLine IS present in the summary). The plain "Project" label
// leads instead. The full snapshot lives in the Project Brain Status tab.
check('no_status_headline', html.indexOf('collab-status') === -1
  && html.indexOf('Parser refactor is in flight') === -1);
check('project_label_leads', html.indexOf('collab-label') !== -1);
// Action-ordered count: the decisions segment renders (and is the emphasised one).
check('decisions_segment', html.indexOf('decisionsAwaiting') !== -1 && html.indexOf('collab-seg-decisions') !== -1);
check('decisions_count', html.indexOf('1') !== -1);
check('has_decisions_accent', html.indexOf('collab-has-decisions') !== -1);
// In-progress + open segments render.
check('progress_segment', html.indexOf('epicsInProgress') !== -1);
check('open_segment', html.indexOf('collab-seg-open') !== -1);
// The DEEP join: the online peer's epic title renders as "advancing «title»".
check('peer_epic_title', html.indexOf('Refactor the parser') !== -1);
check('peer_advancing', html.indexOf('collab-peer-epic') !== -1);
// Not shown: raw activity noise like a "generating" status word (the whole
// point — the bar shows collaboration semantics, not activity state).
check('no_generating_noise', html.indexOf('generating') === -1);
// The per-conversation influence lens is NOT duplicated onto this bar: no conv
// cluster, no divider, no "This chat" lead, and no setConvSegment hook.
check('no_conv_cluster', !stripEl.querySelector('.collab-cluster-conv'));
check('no_conv_divider', !stripEl.querySelector('.collab-cluster-divider'));
check('no_conv_lead', !stripEl.querySelector('.conv-inf-lead'));
check('no_setConvSegment_hook', typeof CB.setConvSegment === 'undefined');
// Single slim line — no multi-row peer box.
check('single_line_bar', !!stripEl.querySelector('.collab-bar-inner')
  && !stripEl.querySelector('.presence-peer-meta'));
// Click opens the Project Brain panel.
const inner = stripEl.querySelector('.collab-bar-inner');
if (inner) { inner.click(); check('click_opens_brain', _opened === 1); }

console.log(out.join('\n'));
// presence.js installs a setInterval that keeps node's event loop alive —
// force-exit so the harness process terminates deterministically.
process.exit(0);
"""


def _run(src):
    frag_file = os.path.join(HERE, '_collab_frag.html')
    harness = os.path.join(HERE, '_collab_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_strip_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, src, ROOT, frag_file],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_collab_bar_renders_semantics_and_opens_brain():
    output = _run(_PRESENCE_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'collab-bar failures:\n' + output
    for must in ('PASS real_strip_element_exists', 'PASS no_status_headline',
                 'PASS project_label_leads', 'PASS decisions_segment',
                 'PASS peer_epic_title', 'PASS peer_advancing',
                 'PASS no_generating_noise', 'PASS no_conv_cluster',
                 'PASS no_conv_divider', 'PASS no_conv_lead',
                 'PASS no_setConvSegment_hook', 'PASS single_line_bar',
                 'PASS click_opens_brain'):
        assert must in output, output


_HARNESS_CONFLICT = _HARNESS.replace(
    "CB._setSummary('/proj/real', {\n"
    "  epicsOpen: 2, epicsClaimed: 1, epicsDone: 0, pendingDecisions: 1,\n"
    "  activePeers: 1, charterExists: true,\n"
    "  statusLine: 'Parser refactor is in flight; on track with the north star.',\n"
    "  peerEpics: { 'c-worker': 'Refactor the parser' },\n"
    "});",
    "CB._setSummary('/proj/real', {\n"
    "  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0, pendingDecisions: 0,\n"
    "  activePeers: 1, charterExists: true, conflicts: 1,\n"
    "  statusLine: 'Parser refactor is in flight; on track with the north star.',\n"
    "  conflictMessages: ['conv A and conv B both editing src/shared.py'],\n"
    "  peerEpics: { 'c-worker': 'Refactor the parser' },\n"
    "});"
).replace(
    "check('decisions_segment', html.indexOf('decisionsAwaiting') !== -1 && html.indexOf('collab-seg-decisions') !== -1);\n"
    "check('decisions_count', html.indexOf('1') !== -1);\n"
    "check('has_decisions_accent', html.indexOf('collab-has-decisions') !== -1);\n"
    "// In-progress + open segments render.\n"
    "check('progress_segment', html.indexOf('epicsInProgress') !== -1);\n"
    "check('open_segment', html.indexOf('collab-seg-open') !== -1);",
    "// Conflict is the highest-urgency segment + carries the verbatim message.\n"
    "check('conflict_segment', html.indexOf('collab-seg-conflict') !== -1);\n"
    "check('has_conflicts_accent', html.indexOf('collab-has-conflicts') !== -1);\n"
    "check('conflict_message_verbatim', html.indexOf('src/shared.py') !== -1);\n"
    "check('conflict_line', html.indexOf('collab-conflict-line') !== -1);"
)


def _run_conflict(src):
    frag_file = os.path.join(HERE, '_collab_cfrag.html')
    harness = os.path.join(HERE, '_collab_charness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_strip_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_CONFLICT)
    try:
        proc = subprocess.run(['node', harness, src, ROOT, frag_file],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_collab_bar_renders_conflict_advisory():
    """A live file-overlap conflict surfaces as the highest-urgency segment +
    a verbatim advisory line on the collaboration bar."""
    output = _run_conflict(_PRESENCE_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'collab-bar conflict failures:\n' + output
    for must in ('PASS conflict_segment', 'PASS has_conflicts_accent',
                 'PASS conflict_message_verbatim', 'PASS conflict_line'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_conflict_segment_is_load_bearing():
    """NC: no-op the conflict segment branch → the conflict segment no longer
    renders → the conflict_segment assertion FAILS. Byte-identical restore."""
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      const conflicts = summary.conflicts || 0;\n      if (conflicts > 0) {"
    assert anchor in original, 'conflict-segment anchor not found'
    patched = original.replace(
        anchor,
        "      const conflicts = summary.conflicts || 0;\n      if (false && conflicts > 0) {  // NC", 1)
    copy_path = os.path.join(HERE, '_collab_cnc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_conflict(copy_path)
        assert 'FAIL conflict_segment' in output, \
            ('NC: disabling the conflict segment must make conflict_segment '
             'FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
# ════════════════════════════════════════════════════════════════════
#  Degraded-push resilience: the collab bar must survive a client whose
#  'presence' push stream never delivered a frame (empty LOCAL mirror), by
#  rendering its peer count + peer→epic lines from the BACKEND-authoritative
#  summary (activePeers / peerEpics). Regression for a tablet whose static
#  assets / WebSocket were flaky: the bar was permanently hidden even though
#  the backend summary reported peers online.
# ════════════════════════════════════════════════════════════════════

# Same harness, but: activePeers=2 + a peerEpics map, and the local mirror is
# left EMPTY (no CB._setPeers) — simulating a dead presence push stream.
_HARNESS_DEADPUSH = _HARNESS.replace(
    "CB._setSummary('/proj/real', {\n"
    "  epicsOpen: 2, epicsClaimed: 1, epicsDone: 0, pendingDecisions: 1,\n"
    "  activePeers: 1, charterExists: true,\n"
    "  peerEpics: { 'c-worker': 'Refactor the parser' },\n"
    "});\n"
    "CB._setPeers('/proj/real', ['c-worker']);\n"
    "CB._render();",
    "CB._setSummary('/proj/real', {\n"
    "  epicsOpen: 0, epicsClaimed: 0, epicsDone: 0, pendingDecisions: 0,\n"
    "  activePeers: 2, charterExists: true,\n"
    "  peerEpics: { 'c-worker': 'Refactor the parser' },\n"
    "});\n"
    "// NO CB._setPeers — the local push mirror is EMPTY (degraded stream).\n"
    "CB._render();"
).replace(
    "const html = stripEl.innerHTML;\n"
    "// Action-first headline: the decisions segment renders (and is the emphasised one).\n"
    "check('decisions_segment', html.indexOf('decisionsAwaiting') !== -1 && html.indexOf('collab-seg-decisions') !== -1);\n"
    "check('decisions_count', html.indexOf('1') !== -1);\n"
    "check('has_decisions_accent', html.indexOf('collab-has-decisions') !== -1);\n"
    "// In-progress + open segments render.\n"
    "check('progress_segment', html.indexOf('epicsInProgress') !== -1);\n"
    "check('open_segment', html.indexOf('collab-seg-open') !== -1);\n"
    "// The DEEP join: the online peer's epic title renders as \"advancing «title»\".\n"
    "check('peer_epic_title', html.indexOf('Refactor the parser') !== -1);\n"
    "check('peer_advancing', html.indexOf('collab-peer-epic') !== -1);\n"
    "// Not shown: raw activity noise like a \"generating\" status word (the whole\n"
    "// point — the bar shows collaboration semantics, not activity state).\n"
    "check('no_generating_noise', html.indexOf('generating') === -1);\n"
    "// Single slim line — no multi-row peer box.\n"
    "check('single_line_bar', !!stripEl.querySelector('.collab-bar-inner')\n"
    "  && !stripEl.querySelector('.presence-peer-meta'));\n"
    "// Click opens the Project Brain panel.\n"
    "const inner = stripEl.querySelector('.collab-bar-inner');\n"
    "if (inner) { inner.click(); check('click_opens_brain', _opened === 1); }",
    "const html = stripEl.innerHTML;\n"
    "// The bar is VISIBLE even though the local push mirror is empty — the\n"
    "// peer segment comes from the backend summary.activePeers.\n"
    "check('bar_visible_on_dead_push', stripEl.hidden === false);\n"
    "check('peers_segment_from_backend', html.indexOf('collab-seg-peers') !== -1);\n"
    "check('peers_count_two', html.indexOf('peersOnline 2') !== -1);\n"
    "// The peer→epic join line renders from backend peerEpics, NOT the mirror.\n"
    "check('peer_epic_title', html.indexOf('Refactor the parser') !== -1);\n"
    "check('peer_advancing', html.indexOf('collab-peer-epic') !== -1);"
)


def _run_deadpush(src):
    frag_file = os.path.join(HERE, '_collab_dpfrag.html')
    harness = os.path.join(HERE, '_collab_dpharness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_strip_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_DEADPUSH)
    try:
        proc = subprocess.run(['node', harness, src, ROOT, frag_file],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_collab_bar_survives_degraded_push_via_backend_count():
    """A client with an empty local push mirror (degraded presence stream)
    still shows the collab bar: the peer count + peer→epic lines come from the
    backend-authoritative summary (activePeers / peerEpics), not the mirror."""
    output = _run_deadpush(_PRESENCE_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'collab-bar dead-push failures:\n' + output
    for must in ('PASS bar_visible_on_dead_push', 'PASS peers_segment_from_backend',
                 'PASS peers_count_two', 'PASS peer_epic_title',
                 'PASS peer_advancing'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_peer_count_uses_backend_activePeers_is_load_bearing():
    """NC: pin the peer count back to the LOCAL mirror only (ignore
    summary.activePeers) → with an empty mirror the bar hides → the
    bar_visible_on_dead_push assertion FAILS. Byte-identical restore. Proves
    the backend-count fallback is load-bearing, not incidental."""
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    const peerCount = (backendCount != null)\n"
              "      ? Math.max(backendCount, convSet.size) : convSet.size;")
    assert anchor in original, 'peer-count anchor not found'
    patched = original.replace(
        anchor, "    const peerCount = convSet.size;  // NC (mirror-only)", 1)
    copy_path = os.path.join(HERE, '_collab_pcnc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_deadpush(copy_path)
        assert 'FAIL bar_visible_on_dead_push' in output, \
            ('NC: mirror-only peer count must hide the bar on an empty mirror '
             'even when the backend reports peers:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped presence.js must be byte-identical'


    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped presence.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  SINGLE project cluster, no headline. The bar no longer hosts a
#  per-conversation "conv cluster" (that lens lives inside the Project Brain
#  panel) NOR the Pillar #7 status headline (that lives in the Status tab).
#  Here we verify the empty-hide gate: the bar hides when there is no
#  coordination count — even when a status snapshot exists (NC neuters the
#  gate → the count-less bar stays visible).
# ════════════════════════════════════════════════════════════════════


# Empty-hide harness: a snapshot exists but there is no coordination count → bar
# hides (the headline alone no longer keeps it visible). Replaces the ENTIRE
# base summary+assertions span (the base assertions expect a populated bar and
# would all fail on a count-less one) with a single scenario + the hidden
# assertion.
_HARNESS_EMPTY = _HARNESS[:_HARNESS.index("// Drive a summary:")] + (
    "CB._setSummary('/proj/real', {\n"
    "  epicsOpen: 0, epicsClaimed: 0, epicsDone: 0, pendingDecisions: 0,\n"
    "  activePeers: 0, charterExists: false,\n"
    "  statusLine: 'A snapshot exists but there are no coordination counts.',\n"
    "  peerEpics: {} });\n"
    "CB._setPeers('/proj/real', []);\n"
    "CB._render();\n"
    "check('empty_bar_hidden', stripEl.hidden === true);\n\n"
    "console.log(out.join('\\n'));\n"
    "process.exit(0);\n"
)


def _run_empty(src):
    frag_file = os.path.join(HERE, '_collab_efrag.html')
    harness = os.path.join(HERE, '_collab_eharness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_strip_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_EMPTY)
    try:
        proc = subprocess.run(['node', harness, src, ROOT, frag_file],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_empty_bar_hides_when_no_status_and_no_counts():
    """A summary with a status snapshot but no coordination counts and no peers
    hides the whole bar — the empty-hide gate keys on counts only."""
    output = _run_empty(_PRESENCE_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'empty-bar failures:\n' + output
    assert 'PASS empty_bar_hidden' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_empty_hide_gate_is_load_bearing():
    """NC: neuter the empty-hide gate so the bar never hides when there is no
    coordination count → empty_bar_hidden FAILS. Byte-identical restore."""
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    if (!segs.length) {\n"
              "      if (_lastFingerprint !== \"\") { el.hidden = true; el.innerHTML = \"\"; _lastFingerprint = \"\"; }\n"
              "      return;\n"
              "    }")
    assert anchor in original, 'empty-hide anchor not found'
    patched = original.replace(
        anchor,
        ("    if (false && !segs.length) {  // NC\n"
         "      if (_lastFingerprint !== \"\") { el.hidden = true; el.innerHTML = \"\"; _lastFingerprint = \"\"; }\n"
         "      return;\n"
         "    }"),
        1)
    copy_path = os.path.join(HERE, '_collab_empty_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_empty(copy_path)
        assert 'FAIL empty_bar_hidden' in output, \
            ('NC: disabling the empty-hide gate must leave the bar visible when '
             'there is no coordination count:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped presence.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_peer_epic_render_is_load_bearing():
    """NC: no-op the peer→epic render loop → the peer's epic title no longer
    renders → the 'peer_epic_title' assertion FAILS. Shipped file byte-
    identical afterward."""
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      const epic = summary.peerEpics[cid];\n      if (!epic) continue;"
    assert anchor in original, 'peer-epic render anchor not found'
    patched = original.replace(
        anchor, "      const epic = summary.peerEpics[cid];\n      if (true || !epic) continue;  // NC", 1)
    copy_path = os.path.join(HERE, '_collab_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL peer_epic_title' in output, \
            ('NC: disabling the peer→epic render must make peer_epic_title '
             'FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped presence.js must be byte-identical'
