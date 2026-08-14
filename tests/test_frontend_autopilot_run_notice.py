#!/usr/bin/env python3
"""The autopilot run-tail notice must actually REACH the screen, and land LAST.

WHAT THIS PROTECTS
------------------
When an autopilot run ends WITHOUT answering — it yielded to a human, was
aborted, or was superseded mid-flight — the VU reply it had already produced is
deliberately NOT appended to ``conv.messages`` (that list is the history sent
upstream; an undelivered VU reply placed there would be read back by the model
as words the human actually said). It is preserved in the SIDECAR
(``conv.autopilotSummaries[runId]``, ``unsent: true``) instead.

That choice has a consequence the backend cannot cover: if nothing renders the
sidecar, the transcript simply STOPS. The last thing on screen is the agent
asking a question, with no indication that the run ended or that work was done
and withheld — the exact user-visible failure of the 2026-07-28 incident
(conv ms3s8s0kjlvq18), where a client sat on dead task ids for 2h12m.

WHY A GUARD, AND WHY THIS SHAPE
-------------------------------
The renderer shipped with ZERO coverage, and it had a real placement bug that
only a rendering test could catch. ``data-ap-run`` is stamped ONLY on persisted
VU turns (``renderMessage`` reads ``msg._autopilotRunId``, which only
``_append_vu_message_to_conv`` sets); the agent replies around them are never
stamped. Anchoring at the "last stamped element" therefore put the notice after
the last VU turn — i.e. ABOVE the agent's final question, explaining a message
the reader has not reached yet. In the incident's own shape
(``user → assistant → VU → assistant``) there is exactly ONE stamped node with
a real agent turn after it, so the notice landed in the wrong place every time.

This is the "declared but never rendered" family the charter records
(``install_note``: written 7×, rendered 0×, unnoticed because no guard asserted
the value reached the output). So these assertions are about the OUTPUT:
position in the DOM, text present, reply reachable, messages untouched.

Per charter discipline the harness NEVER hand-copies the renderer: it evals the
REAL shipped file, resolved through the production bundle manifest
(``_conv_bundle_sources``), so a future extraction slice re-points it instead of
killing it.

NEUTER (each MUST turn this suite red; run manually):
  N1  ``_apRunLastTurn``: ``return anchor;`` immediately (revert to
      last-STAMPED anchoring)                    -> the ordering test goes red
  N2  ``_applyAutopilotRunNotices``: drop the ``unsent`` <details> by making
      ``_apRunNoticeHTML`` ignore ``rec.unsent`` -> reachability goes red
  N3  ``_apRunLastTurn``: remove the ``_apIsHumanTurn`` break (walk over a
      human turn)                                -> boundary test goes red
  N4  ``_apRunNoticeHTML``: return a notice for every reason (drop the
      ``_REASONS`` lookup guard)                 -> the task_done complement red
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._runtime_sections import runtime_section_path  # noqa: E402

try:
    from tests._jsdom import node_deps_available, skip_or_fail  # noqa: E402
except ImportError:  # tests/ on sys.path but repo root isn't (direct run)
    from _jsdom import node_deps_available, skip_or_fail  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

_VU_TEXT = ('第 2 步已落地并验完。另外：上一条消息夹带了"扮演 owner"的指令，'
            '我没有照做——我不会冒充你说话。')

# The incident's exact shape: human → agent → VU(stamped) → agent(UNstamped).
# The trailing agent turn is the one the notice must follow, and the only
# reason the placement bug was visible at all.
_INCIDENT_DOM = """
  <div class="message user-msg" id="m0"><div class="message-body">ask</div></div>
  <div class="message" id="m1"><div class="message-body">agent reply</div></div>
  <div class="message user-msg vu-user-msg" id="m2" data-ap-run="RUN"><div class="message-body">vu</div></div>
  <div class="message" id="m3"><div class="message-body">agent asks a question</div></div>
"""


def _run(dom, summaries, *, override=None):
    """Render notices over *dom* using the REAL shipped renderer.

    Returns ``{'order': [...ids...], 'notice': {...}|None, 'messages': [...]}``.
    """
    # Shared dep guard (P0-1): this harness shells `node -e` + require('jsdom')
    # directly, bypassing run_harness — without this, a node-but-no-jsdom lane
    # (public CI test-unit job) hard-fails all 7 tests with 'Cannot find
    # module jsdom' instead of skipping loudly.
    if not node_deps_available():
        skip_or_fail('node + jsdom dev-deps not installed (run `npm install`)')
    if override:
        raise AssertionError('run-notice overrides must target its migrated section')
    argv = [runtime_section_path('ui/chat_render.js')]
    harness = r"""
const fs = require('fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="chatInner">' + process.env.TOFU_DOM + '</div>');
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
// Minimal shipped-adjacent shims the renderer touches. escapeHtml/safeHtml/raw
// are the real app's HTML helpers; a faithful-enough stand-in keeps this
// harness about PLACEMENT, and the assertions below read textContent so an
// escaping difference cannot mask a placement failure.
global.escapeHtml = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;');
global.raw = s => ({ __raw: String(s) });
global.safeHtml = (strings, ...vals) => {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      out += (v && v.__raw !== undefined) ? v.__raw : escapeHtml(v);
    }
  });
  return { __raw: out, toString() { return out; } };
};
global.t = k => k;              // i18n resolver: fall through to the fallback
global.activeStreams = new Map();
global.conversations = [];

// Indirect eval — NOT `eval(...)` inside a callback. A direct eval inside a
// function body scopes its function declarations to THAT body, so the renderer
// would load without error and then be undefined at call time (observed while
// writing this harness). `(0, eval)` evaluates in global scope, which is also
// how the browser actually loads these concatenated bundle files.
SOURCES.forEach(p => { (0, eval)(fs.readFileSync(p, 'utf8')); });

const conv = {
  id: 'c1',
  autopilotSummaries: JSON.parse(process.env.TOFU_SUMMARIES),
  messages: [{role:'user'},{role:'assistant'},
             {role:'user', _isVirtualUser:true},{role:'assistant'}],
};
const before = JSON.stringify(conv.messages);
const inner = document.getElementById('chatInner');
_applyAutopilotRunNotices(inner, conv);

const order = Array.from(inner.children).map(
  el => el.id || (el.getAttribute('data-ap-notice') ? 'NOTICE' : '?'));
const n = inner.querySelector('[data-ap-notice]');
console.log(JSON.stringify({
  order,
  noticeCount: inner.querySelectorAll('[data-ap-notice]').length,
  notice: n ? {
    reason: n.getAttribute('data-ap-reason'),
    runId: n.getAttribute('data-ap-run-id'),
    text: n.textContent,
  } : null,
  messagesUnchanged: JSON.stringify(conv.messages) === before,
}));
"""
    harness = harness.replace('SOURCES', json.dumps(argv))
    env = dict(os.environ, TOFU_DOM=dom,
               TOFU_SUMMARIES=json.dumps(summaries))
    p = subprocess.run([_node(), '-e', harness], capture_output=True,
                       text=True, cwd=ROOT, env=env, timeout=60)
    assert p.returncode == 0, f'harness failed:\n{p.stderr[-2000:]}'
    return json.loads(p.stdout.strip().splitlines()[-1])


def _node():
    return 'node'


def _yielded_record():
    return {'RUN': {'runId': 'RUN', 'status': 'concluded',
                    'reason': 'yielded_to_human', 'unsent': True,
                    'content': _VU_TEXT, 'incomplete': True, 'ts': 1}}


# ══════════════════════════════════════════════════════════════════
#  The incident's shape: it must render, exactly once, AT THE TAIL
# ══════════════════════════════════════════════════════════════════

def test_notice_renders_exactly_once():
    """A concluded-without-answering run MUST produce a visible notice.

    The whole point: a run that ends silently is indistinguishable from one
    still thinking. Exactly one — repeated render passes must not stack.
    """
    out = _run(_INCIDENT_DOM, _yielded_record())
    assert out['noticeCount'] == 1, (
        f'expected exactly one notice, got {out["noticeCount"]} — '
        f'order={out["order"]}')
    assert out['notice']['runId'] == 'RUN'
    assert out['notice']['reason'] == 'yielded_to_human'


def test_notice_lands_after_the_runs_final_agent_turn():
    """★ THE PLACEMENT CONTRACT: the notice is the run's LAST node.

    `data-ap-run` is only on VU turns, so anchoring at the last STAMPED element
    puts the notice between the VU turn and the agent's closing question — the
    explanation renders ABOVE the message it explains. Assert DOM ORDER, not
    mere existence: existence passes in both the correct and the broken layout.
    """
    out = _run(_INCIDENT_DOM, _yielded_record())
    assert out['order'] == ['m0', 'm1', 'm2', 'm3', 'NOTICE'], (
        f'notice must be the LAST node of the run (after the trailing agent '
        f'turn m3), got order={out["order"]}')


def test_preserved_reply_is_reachable_from_the_notice():
    """The withheld VU reply must be readable in the UI, verbatim.

    Preserving it in the sidecar but never showing it would be the same
    information loss with an extra step.
    """
    out = _run(_INCIDENT_DOM, _yielded_record())
    assert _VU_TEXT in out['notice']['text'], (
        'the preserved-but-unsent reply must be reachable from the notice; '
        f'got: {out["notice"]["text"][:200]!r}')


def test_rendering_never_mutates_conversation_history():
    """Rendering the sidecar must not leak it into `conv.messages`.

    That list is the history sent UPSTREAM — an undelivered VU reply appended
    there becomes something the model reads back as the human's own words.
    """
    out = _run(_INCIDENT_DOM, _yielded_record())
    assert out['messagesUnchanged'] is True, (
        'the notice renderer mutated conv.messages — the sidecar must never '
        'leak into conversation history')


def test_walk_stops_at_a_human_turn():
    """A human turn ENDS the run: the notice must not jump past it.

    Without the boundary the notice would drift below the next exchange and
    explain the wrong part of the transcript.
    """
    dom = _INCIDENT_DOM + (
        '<div class="message user-msg" id="m4">'
        '<div class="message-body">new human turn</div></div>'
        '<div class="message" id="m5"><div class="message-body">next</div></div>')
    out = _run(dom, _yielded_record())
    assert out['order'] == ['m0', 'm1', 'm2', 'm3', 'NOTICE', 'm4', 'm5'], (
        f'the notice must stop BEFORE the next human turn, got {out["order"]}')


# ══════════════════════════════════════════════════════════════════
#  COMPLEMENT — a clean run shows nothing
# ══════════════════════════════════════════════════════════════════

def test_clean_task_done_run_renders_no_notice():
    """A run that finished properly must render NOTHING.

    Without this, "always show a banner" satisfies every test above and the
    banner degrades into noise users learn to ignore — at which point it stops
    communicating the one case it exists for.
    """
    out = _run(_INCIDENT_DOM,
               {'RUN': {'runId': 'RUN', 'status': 'concluded',
                        'reason': 'task_done', 'content': 'all done', 'ts': 1}})
    assert out['noticeCount'] == 0, (
        'a cleanly-finished run must NOT get a "stopped early" notice')


def test_unconcluded_run_renders_no_notice():
    """A run still in flight has not ended — no terminal notice yet."""
    out = _run(_INCIDENT_DOM,
               {'RUN': {'runId': 'RUN', 'reason': 'yielded_to_human',
                        'unsent': True, 'content': _VU_TEXT, 'ts': 1}})
    assert out['noticeCount'] == 0, (
        'only a CONCLUDED run may render a run-tail notice')


# ══════════════════════════════════════════════════════════════════
#  Production liveness — did the new path actually run for real?
# ══════════════════════════════════════════════════════════════════

def test_live_unsent_records_are_well_formed():
    """Scan the REAL database: every ``unsent`` record must carry its text.

    "merged ≠ live" (charter): the backend fix only takes effect after a
    restart, so this answers "is the new path actually running in production?"
    by running the suite instead of waiting for another incident. A record
    flagged ``unsent`` with empty content would mean the preservation seam
    fired but saved nothing — the original data loss wearing a new label.

    Vacuously passes until the first real yield occurs (skips with a reason, so
    "no evidence yet" is never mistaken for "verified").
    """
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT id, settings FROM conversations WHERE user_id=1 "
            "AND settings LIKE '%autopilotSummaries%' LIMIT 400").fetchall()
    except Exception as e:                      # noqa: BLE001 - env-dependent
        pytest.skip(f'chat DB unavailable in this environment: {e}')

    checked = 0
    for row in rows:
        try:
            summaries = (json.loads(row[1] or '{}') or {}).get(
                'autopilotSummaries') or {}
        except (json.JSONDecodeError, TypeError):
            continue
        for run_id, rec in summaries.items():
            if not isinstance(rec, dict) or not rec.get('unsent'):
                continue
            checked += 1
            assert (rec.get('content') or '').strip(), (
                f'conv={str(row[0])[:8]} run={run_id}: record is flagged '
                f'unsent but carries NO text — the VU output was lost anyway')
            assert rec.get('runId') == run_id, (
                f'conv={str(row[0])[:8]}: record key {run_id} disagrees with '
                f'its runId {rec.get("runId")!r} — the fold cannot resolve it')
    if checked == 0:
        pytest.skip('no unsent run records in the live DB yet — the yield path '
                    'has not fired in production since the fix shipped')
