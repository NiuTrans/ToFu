#!/usr/bin/env python3
"""Auto-research must be startable from the Reading-Mode LANDING screen.

The capability (harvest → survey → ideate) shipped with ZERO frontend entry:
grepping `static/` for `produce_research` returned nothing, so the only way to
launch it was hoping the chat model chose to call the tool — which is also
search-gated, so it silently disappeared whenever a user turned web search off.

★ Why the LANDING screen and not a 7th paper tab (owner ruling):
every `.paper-tab-panel` is **paper-hash scoped** — `_switchPaperTab` dispatches
`_loadOrGenerateReport` / `_initVideoTab`, and the video tab even looks its job
up with `Api.paper.videoLookup(_paperHash)`. But a research DIRECTION is
pre-paper: there is no open paper when you start one. A tab would force the
user to first open an unrelated paper. The landing screen already hosts the
direction-shaped `describe → recommend` input, so that is where this belongs.

These are BEHAVIOUR guards driving the REAL shipped source under jsdom (never a
hand-copied reducer — charter: a harness copy silently keeps asserting the old
world). They assert what a user can DO:

  1. start a job with NO paper open,
  2. see progress that survives a re-render, from the SERVER clock,
  3. see a degraded job AS degraded (artifact_quality), not as success.

Skips cleanly when node/jsdom dev-deps are absent.
"""

import json
import os
import shutil
import subprocess

import pytest

from tests._paper_vite import compiled_typescript

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'research-runtime.ts')
RESEARCH_VIEW_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'research-view.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')

pytestmark = pytest.mark.unit


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return (os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))
            and os.path.isfile(ESBUILD))


requires_node = pytest.mark.skipif(
    not _node_deps_available(), reason='node/jsdom dev-deps not installed')


# The harness evals the compiled Vite runtime, then the rollback renderer in
# the same order as the real domain loader. Api/push are stubbed at the seam so
# the test is offline, but every line of lifecycle logic under assertion is
# the shipped TypeScript owner rather than a hand-copied reducer.
_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[2];
const SCENARIO = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperPdfViewer"></div>'
  + '<div id="researchViewer"></div></body>',
  { url: 'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k) => k;
global.debugLog = () => {};
global.Icon = () => '<svg></svg>';
global.config = { model: 'kimi-k3' };

const calls = { start: [], get: [], events: [], abort: [], subscribed: [] };

// Scenario-driven task snapshot returned by Api.tasks.get.
const SNAPSHOTS = {
  running: { ok: true, id: 'research_t1', kind: 'research', status: 'running',
             createdAt: 1700000000000, updatedAt: 1700000030000,
             meta: { direction: 'long-context KV cache compression' } },
  degraded: { ok: true, id: 'research_t1', kind: 'research', status: 'done',
              createdAt: 1700000000000, updatedAt: 1700000900000,
              artifact_quality: { degraded: true,
                reason: 'structural gate rejected ALL 6 generated idea(s)' },
              result: { accepted: [], rejected: [1,2,3,4,5,6],
                        gate_reached: 'structural', folder_id: 'research_t1' } },
  clean: { ok: true, id: 'research_t1', kind: 'research', status: 'done',
           createdAt: 1700000000000, updatedAt: 1700000900000,
           artifact_quality: { degraded: false, reason: '' },
           result: { accepted: [{title:'A'},{title:'B'}], rejected: [],
                     gate_reached: 'accepted', folder_id: 'research_t1' } },
};

global.Api = {
  tasks: {
    start: async (kind, payload) => {
      calls.start.push({ kind, payload });
      return { ok: true, taskId: 'research_t1', kind, deduped: false };
    },
    get: async (taskId) => { calls.get.push(taskId); return SNAPSHOTS[SCENARIO]; },
    events: async (taskId, cursor) => {
      calls.events.push({ taskId, cursor });
      const snapshot = SNAPSHOTS[SCENARIO];
      return { ...snapshot, events: [], next_cursor: cursor,
               done: snapshot.status !== 'running' && snapshot.status !== 'pending' };
    },
    abort: async (taskId) => { calls.abort.push(taskId); return { ok: true }; },
    list: async () => ({ ok: true, tasks: [], total: 0 }),
  },
  research: { lookup: async () => ({ found: false }) },
};
global.pushSubscribe = (ch, id) => { calls.subscribed.push(ch + ':' + id); };
global.pushUnsubscribe = () => {};

// _paperHash deliberately EMPTY: no paper is open. If the module needs one,
// that is the bug this file exists to prevent.
global._paperHash = '';
global._activePaperId = '';

const nativeSrc = fs.readFileSync(process.argv[4], 'utf8');
(0, eval)(nativeSrc);

(async () => {
  const out = { calls };
  if (typeof _startResearchJob !== 'function') {
    console.log(JSON.stringify({ error: '_startResearchJob is not defined' }));
    return;
  }
  await _startResearchJob('long-context KV cache compression');
  // Let the poll/paint settle.
  await new Promise(r => setTimeout(r, 30));
  if (typeof _researchStream !== 'undefined' && _researchStream) {
    out.state = {
      taskId: _researchStream.taskId,
      status: _researchStream.status,
      degraded: !!_researchStream.degraded,
      degradedReason: _researchStream.degradedReason || '',
      startedAt: _researchStream.startedAt || 0,
      folderId: _researchStream.folderId || '',
    };
  }
  out.html = document.getElementById('researchViewer').innerHTML;
  console.log(JSON.stringify(out));
  // A job still in `running` legitimately keeps its 2s poll timer armed, so
  // node would never exit on its own. Stop the loop the way the product does,
  // then exit explicitly — the assertion payload is already emitted.
  if (typeof _stopResearchPoll === 'function') _stopResearchPoll();
  process.exit(0);
})();
"""


def _run(scenario):
    import tempfile

    with tempfile.TemporaryDirectory(prefix='tofu-research-test-') as temp_dir:
        harness = os.path.join(temp_dir, 'research-entry-harness.js')
        with open(harness, 'w', encoding='utf-8') as f:
            f.write(_HARNESS)
        with compiled_typescript(
            RESEARCH_VIEW_TS, expose_feature_registry_to_window=True,
        ) as built:
            r = subprocess.run(['node', harness, ROOT, scenario, built],
                               capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            pytest.fail(f'harness failed ({scenario}):\n{r.stdout}\n{r.stderr}')
        line = [x for x in r.stdout.strip().splitlines() if x.startswith('{')]
        assert line, f'no JSON from harness:\n{r.stdout}\n{r.stderr}'
        return json.loads(line[-1])


# ── 1. Start with NO paper open ───────────────────────────────────

@requires_node
def test_a_direction_starts_a_job_with_no_paper_open():
    """★ Acceptance #1: type a direction on the landing screen, get a job.

    `_paperHash` is empty in the harness — if the module reaches for an open
    paper the job never starts, which is exactly the 7th-tab mistake.
    """
    out = _run('running')
    assert 'error' not in out, out.get('error')
    starts = out['calls']['start']
    assert len(starts) == 1, f'expected exactly one start call, got {starts}'
    assert starts[0]['kind'] == 'research', (
        f"must start the generic kind='research' job, got {starts[0]['kind']!r}")
    assert starts[0]['payload'].get('direction') == \
        'long-context KV cache compression', (
        f"the direction did not reach the API: {starts[0]['payload']}")
    assert starts[0]['payload'].get('model') == 'kimi-k3', (
        f"the active model did not reach the API: {starts[0]['payload']}")
    assert out.get('state', {}).get('taskId') == 'research_t1', (
        'the returned task id was not retained — nothing to poll or abort')


@requires_node
def test_start_goes_through_the_generic_task_api_not_a_bespoke_route():
    """Only Api.tasks.* is used — no paper-scoped lookup, no new endpoint.

    Scanned over CODE only: the file header explains *why* it avoids
    `_paperHash` / `videoLookup`, and a scan that also read comments would
    forbid documenting the very decision it enforces (and would push the next
    author to delete the rationale to get green).
    """
    import re as _re

    raw = open(RESEARCH_RUNTIME_TS, encoding='utf-8').read()
    assert "tasks().start('research'" in raw, (
        'must start via the generic task API')
    # Strip /* … */ and // … comments before scanning.
    code = _re.sub(r'/\*[\s\S]*?\*/', '', raw)
    code = _re.sub(r'^\s*//.*$', '', code, flags=_re.M)
    for forbidden in ('_paperHash', 'videoLookup', 'produce_research',
                      "fetch('/api", 'fetch(`/api'):
        assert forbidden not in code, (
            f'research-runtime.ts CODE references {forbidden!r} — the landing entry '
            'must be paper-scope-free and route every call through window.Api')


# ── 2. Progress survives a re-render, on the SERVER clock ─────────

@requires_node
def test_elapsed_comes_from_the_server_clock_not_a_local_stopwatch():
    """★ Acceptance #2: a refresh / tab switch must not reset the timer.

    The job's start instant must be adopted from the server's `createdAt`
    (epoch ms), so re-attaching to a running job continues the real elapsed
    time instead of restarting at zero.
    """
    out = _run('running')
    st = out.get('state') or {}
    assert st.get('startedAt') == 1700000000000, (
        "startedAt must be the server's createdAt (1700000000000), got "
        f"{st.get('startedAt')!r} — a locally minted clock re-mints on every "
        'render and washes a long-running job into looking fresh')


@requires_node
def test_running_job_is_polled_and_subscribed_for_live_progress():
    out = _run('running')
    assert out['calls']['events'], 'the job was never polled for event replay'
    assert any(s.startswith('research:') for s in out['calls']['subscribed']), (
        "no pushSubscribe('research', …) — progress would only advance on the "
        f"poll fallback. Subscribed: {out['calls']['subscribed']}")


# ── 3. A degraded job must LOOK degraded ──────────────────────────

@requires_node
def test_degraded_job_is_surfaced_as_degraded_not_as_success():
    """★ Acceptance #3: the interlock with the artifact_quality axis.

    A research pass whose structural gate wiped 100% of the ideas returns
    status='done' BY DESIGN (status is the lifecycle axis). If the UI reads
    only status, a total misfire renders as a clean success — exactly the bug
    the quality axis was added to make visible.
    """
    out = _run('degraded')
    st = out.get('state') or {}
    assert st.get('degraded') is True, (
        'a degraded job was not flagged in the UI state — status=done alone '
        'made a 100% gate wipe look like success')
    assert 'structural gate' in (st.get('degradedReason') or ''), (
        f"the degraded reason was dropped: {st.get('degradedReason')!r}")
    assert 'degraded' in (out.get('html') or '').lower(), (
        'nothing in the rendered output marks this job as degraded')


@requires_node
def test_clean_job_is_not_flagged_degraded():
    """Discrimination: the degraded marker must track the real verdict."""
    out = _run('clean')
    st = out.get('state') or {}
    assert st.get('degraded') is False, (
        'a healthy job was flagged degraded — the marker is unconditional')


@requires_node
def test_finished_job_links_back_to_the_library_folder():
    """Products land on the shelf: harvest wrote them to folder_id=research_<id>.

    The whole reason this lives in Reading Mode is that the corpus is already
    in `paper_library` — surfacing the folder is what closes the loop from
    "a direction" to "20 papers I can open and ask about".
    """
    out = _run('clean')
    st = out.get('state') or {}
    assert st.get('folderId') == 'research_t1', (
        f"the library folder was not carried into UI state: {st.get('folderId')!r}")
