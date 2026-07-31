#!/usr/bin/env python3
"""Past research must be DISCOVERABLE and its content must reach the screen.

TWO DEFECTS THIS PINS (epic pt_a40dbd9569194b52, closing batch).

★ 1. The one-way-hash trap (owner-found).
``/api/v1/research/lookup`` requires the caller to already know the direction
TEXT. But the stored key is ``sha256(normalised direction)[:32]`` — a one-way
hash — and nothing indexed which directions had been researched. So a user
returning a week later had to recall their exact wording or the artifacts were
unreachable. That is user-indistinguishable from the TTL data loss this epic
set out to fix: the bytes are on disk, and you still cannot get to them.
The fix is cheap because ``meta.direction`` already stores the original text;
what was missing was an endpoint that lists it.

★ 2. The three-numbers panel.
The finished-job view rendered only ``N accepted / N rejected / N papers``.
Every idea title, mechanism, novelty claim, falsifiable prediction, the
rejection audit with its four-axis scores, the survey markdown and the gap map
were fetched and then dropped on the floor by the renderer.

WHY THE ASSERTIONS LOOK LIKE THIS
The recurring failure in this epic is a symbol that exists but is never
reached (``survey_lang_key``, then ``load_research_artifacts``, then
``Api.research.lookup``). So no test here asserts that a function exists.
The list tests drive real HTTP against a real DB with the task registry
emptied; the render tests eval the REAL research.js under jsdom and assert on
the DOM a user would actually see.

Run:  pytest tests/test_research_discoverability.py -m unit
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit

_DIR_A = 'long-context KV-cache compression'
_DIR_B = 'diffusion language models'

_GAPS = {'schema_version': 1, 'direction': _DIR_A, 'lang': 'en',
         'open_gaps': [{'id': 'gap_1', 'gap': 'needle recall unmeasured',
                        'why_open': 'only perplexity reported',
                        'evidence': ['2305.11111']}]}

_IDEATE = {
    'accepted': [{
        'id': 'idea_1', 'title': 'Per-layer learnable compression rate',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'core_mechanism': 'attention entropy varies per layer',
        'novelty_claim': 'unlike 2305.11111 the rate is learned',
        'falsifiable_prediction': 'needle recall drops <2% at 4x',
        'why_not_AB': 'derived from the spectrum, not bolted on',
        'scores': {'novelty': 4, 'falsifiability': 5,
                   'mechanism_depth': 4, 'value': 4}, 'overall': 4.25}],
    'rejected': [{
        'id': 'idea_2', 'title': 'KV compression + speculative decoding',
        'reject_stage': 'rubric', 'reject_reason': 'overall 2.75 < 4.0',
        'scores': {'novelty': 2, 'falsifiability': 3,
                   'mechanism_depth': 2, 'value': 4}, 'overall': 2.75}],
    'threshold': 4.0, 'gate_reached': 'accepted'}


def _new_loop_run(coro):
    """Drive one coroutine on a private loop.

    The suite runs with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, so pytest-asyncio is
    NOT loaded and an ``@pytest.mark.asyncio`` test would be silently skipped
    rather than run — a guard that never executes is worse than none.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def fresh_db(tmp_path):
    from lib.database import reset_sqlite_for_tests, restore_db_state
    snapshot = reset_sqlite_for_tests(str(tmp_path / 'research_disc.db'))
    try:
        yield
    finally:
        restore_db_state(snapshot)


@pytest.fixture()
def client(fresh_db):
    import server
    server.app.config['TESTING'] = True
    return server.app.test_client()


def _empty_the_task_registry():
    from lib.research.runtime import _research_runtime
    with _research_runtime._lock:
        _research_runtime._tasks.clear()


def _get_json(client, path, **query):
    resp = _new_loop_run(client.get(path, query_string=query))
    return resp.status_code, _new_loop_run(resp.get_json())


# ═══ 1. ★ Discoverability: find past work WITHOUT knowing the wording ═══

def test_past_directions_are_listable_without_knowing_the_text(client):
    """★ The decisive test for the one-way-hash trap.

    The only input is "I researched something before". If the list cannot
    return the direction TEXT, a user who forgot their exact wording can never
    reach the artifacts again — the hash is not reversible.
    """
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(_DIR_A, 'en', '# Survey A', _GAPS, model='m')
    persist_ideate(_DIR_A, 'en', _IDEATE, model='m')
    _empty_the_task_registry()

    status, body = _get_json(client, '/api/v1/research/list')
    assert status == 200, f'list failed: {status} {body}'
    items = body.get('items') or []
    assert items, 'no past research listed — the artifacts are undiscoverable'

    dirs = [it.get('direction') for it in items]
    assert _DIR_A in dirs, (
        f'the ORIGINAL direction text was not recoverable from the list: '
        f'{dirs} — a forgotten wording means unreachable artifacts')

    # And the listed direction must actually work as a lookup key: the whole
    # point is list → click → read.
    row = next(it for it in items if it['direction'] == _DIR_A)
    st2, got = _get_json(client, '/api/v1/research/lookup',
                         direction=row['direction'], lang=row.get('lang') or 'en')
    assert st2 == 200 and got.get('found') is True, (
        'a direction returned by /list did not resolve through /lookup — the '
        'two endpoints disagree on the identity')


def test_list_carries_the_counts_needed_to_choose(client):
    """A list of bare strings is not usable — the user picks by outcome."""
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(_DIR_A, 'en', '# Survey A', _GAPS, model='m')
    persist_ideate(_DIR_A, 'en', _IDEATE, model='m')
    _empty_the_task_registry()

    _, body = _get_json(client, '/api/v1/research/list')
    row = next(it for it in (body.get('items') or [])
               if it['direction'] == _DIR_A)
    assert row.get('accepted') == 1, f'accepted count missing/wrong: {row}'
    assert row.get('rejected') == 1, f'rejected count missing/wrong: {row}'
    assert row.get('lang') == 'en'
    assert isinstance(row.get('created_at'), int) and row['created_at'] > 0, (
        f'no usable timestamp to sort/display by: {row}')


def test_list_covers_multiple_directions_and_is_newest_first(client):
    from lib.research.persistence import persist_survey
    persist_survey(_DIR_A, 'en', '# A', _GAPS, model='m')
    persist_survey(_DIR_B, 'en', '# B', _GAPS, model='m')
    _empty_the_task_registry()

    _, body = _get_json(client, '/api/v1/research/list')
    dirs = [it['direction'] for it in (body.get('items') or [])]
    assert _DIR_A in dirs and _DIR_B in dirs, f'a direction was dropped: {dirs}'
    ts = [it['created_at'] for it in body['items']]
    assert ts == sorted(ts, reverse=True), f'not newest-first: {ts}'


def test_empty_store_lists_nothing_without_erroring(client):
    status, body = _get_json(client, '/api/v1/research/list')
    assert status == 200 and body.get('items') == [], (
        'an empty store must be an honest empty list, not an error')


def test_list_never_leaks_a_real_papers_report(client, fresh_db):
    """paper_reports also holds per-paper reports. The list must return only
    research rows, keyed by their composite lang prefix.

    The decoy row deliberately carries a WELL-FORMED meta with a `direction`
    field, so the only thing that can exclude it is the lang-prefix filter.
    (An empty-meta decoy would be dropped by the "no recorded text" guard
    instead, letting a `WHERE 1=1` regression pass unnoticed — measured: that
    exact NEUTER did not bite until this fixture was tightened.)
    """
    import json as _json
    from lib.database import get_thread_db
    from lib.research.persistence import persist_survey
    db = get_thread_db()
    db.execute("INSERT INTO paper_reports (paper_hash, lang, report, model,"
               " meta, created_at) VALUES (?,?,?,'',?,?)",
               ('deadbeef' * 4, 'en', 'A REAL PAPER REPORT',
                _json.dumps({'kind': 'insight',
                             'direction': 'NOT A RESEARCH DIRECTION'}),
                9999999999))
    db.commit()
    persist_survey(_DIR_A, 'en', '# A', _GAPS, model='m')
    _empty_the_task_registry()

    _, body = _get_json(client, '/api/v1/research/list')
    dirs = [it.get('direction') for it in (body.get('items') or [])]
    assert dirs == [_DIR_A], f'a non-research row leaked into the list: {dirs}'


# ═══ 2. ★ The panel must actually render the artifacts ═══

def _node_ok():
    return bool(shutil.which('node')) and os.path.isdir(
        os.path.join(ROOT, 'node_modules', 'jsdom'))


requires_node = pytest.mark.skipif(
    not _node_ok(), reason='node/jsdom dev-deps not installed')

_RENDER_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="paperPdfViewer"></div></body>',
                      { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
// t() returns the KEY (not a default) so a missing i18n key is VISIBLE as a
// raw key in the DOM rather than being masked by a fallback string.
global.t = (k) => k;
global.debugLog = () => {}; global.Icon = () => '<svg></svg>';
global.renderMarkdown = (md) => '<div class="md">' + String(md || '') + '</div>';

const RESULT = {
  ok: true, found: true, direction: 'long-context KV-cache compression',
  lang: 'en', survey_md: '# Survey heading\n\nSurvey prose body.',
  open_gaps: { schema_version: 1, open_gaps: [
    { id: 'gap_1', gap: 'needle recall unmeasured',
      why_open: 'only perplexity reported' } ] },
  accepted: [{ id: 'idea_1', title: 'Per-layer learnable compression rate',
    core_mechanism: 'attention entropy varies per layer',
    novelty_claim: 'unlike 2305.11111 the rate is learned',
    falsifiable_prediction: 'needle recall drops under 2 percent at 4x',
    scores: { novelty: 4, falsifiability: 5, mechanism_depth: 4, value: 4 },
    overall: 4.25 }],
  rejected: [{ id: 'idea_2', title: 'KV compression plus speculative decoding',
    reject_stage: 'rubric', reject_reason: 'overall 2.75 below 4.0',
    scores: { novelty: 2, falsifiability: 3, mechanism_depth: 2, value: 4 },
    overall: 2.75 }],
  threshold: 4.0, gate_reached: 'accepted',
};

const calls = { lookup: [] };
global.Api = {
  tasks: {
    start: async () => ({ ok: true, taskId: 'research_t1' }),
    get: async () => ({ ok: true, id: 'research_t1', status: 'done',
      createdAt: 1700000000000, updatedAt: 1700000900000,
      artifact_quality: { degraded: false, reason: '' },
      result: { accepted: RESULT.accepted, rejected: RESULT.rejected,
                corpus_size: 18, gate_reached: 'accepted',
                folder_id: 'research_t1' } }),
    abort: async () => ({ ok: true }),
  },
  research: {
    lookup: async (d, l) => { calls.lookup.push([d, l]); return RESULT; },
  },
};
global.pushSubscribe = () => {}; global.pushUnsubscribe = () => {};
global._paperHash = ''; global._activePaperId = '';

const src = fs.readFileSync(path.join(ROOT, 'static/js/paper/research.js'), 'utf8');
(0, eval)(src);

(async () => {
  const out = { calls };
  const SCENARIO = process.argv[3];
  if (SCENARIO === 'restore') {
    // The re-attach path: NO live job was ever started in this page. The panel
    // must rebuild itself purely from the durable store.
    if (typeof _restoreResearchFromStore !== 'function') {
      console.log(JSON.stringify({ error: '_restoreResearchFromStore missing' }));
      return;
    }
    await _restoreResearchFromStore('long-context KV-cache compression', 'en');
  } else {
    await _startResearchJob('long-context KV-cache compression');
    await new Promise(r => setTimeout(r, 40));
  }
  out.html = document.getElementById('paperPdfViewer').innerHTML;
  out.text = document.getElementById('paperPdfViewer').textContent;
  console.log(JSON.stringify(out));
  if (typeof _stopResearchPoll === 'function') _stopResearchPoll();
  process.exit(0);
})();
"""


def _run_render(scenario='live'):
    h = os.path.join(ROOT, 'tests', '_tmp_research_render.js')
    with open(h, 'w', encoding='utf-8') as f:
        f.write(_RENDER_HARNESS)
    try:
        r = subprocess.run(['node', h, ROOT, scenario],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            pytest.fail(f'render harness failed:\n{r.stdout}\n{r.stderr}')
        line = [x for x in r.stdout.strip().splitlines() if x.startswith('{')]
        assert line, f'no JSON from harness:\n{r.stdout}\n{r.stderr}'
        return json.loads(line[-1])
    finally:
        if os.path.exists(h):
            os.remove(h)


@requires_node
def test_accepted_idea_title_and_mechanism_reach_the_dom():
    """★ The owner's explicit criterion. The old panel showed three integers;
    the ideas the pipeline paid for were fetched and then discarded."""
    out = _run_render()
    assert 'error' not in out, out.get('error')
    text = out.get('text') or ''
    assert 'Per-layer learnable compression rate' in text, (
        'the accepted idea TITLE never reached the DOM — the panel is still '
        'only showing counts')
    assert 'attention entropy varies per layer' in text, (
        'core_mechanism (the WHY of the idea) was dropped by the renderer')


@requires_node
def test_novelty_claim_and_falsifiable_prediction_render():
    out = _run_render()
    text = out.get('text') or ''
    assert 'unlike 2305.11111 the rate is learned' in text, \
        'novelty_claim was dropped'
    assert 'needle recall drops under 2 percent at 4x' in text, \
        'falsifiable_prediction was dropped'


@requires_node
def test_rejected_audit_is_collapsed_but_summarised():
    """Owner ruling: default-collapsed WITH a one-line summary, so an honest
    宁缺毋滥 zero is legible instead of an alarming wall of rejections."""
    out = _run_render()
    html = out.get('html') or ''
    text = out.get('text') or ''
    assert '<details' in html, (
        'the rejection audit is not collapsible — a 0-accepted/6-rejected run '
        'would present as a wall of failures')
    assert 'open' not in html.split('<details')[1].split('>')[0], \
        'the rejection audit is expanded by default'
    assert '4' in text and ('2.75' in text or '2.8' in text), (
        'the one-line summary must show the best rejected score against the '
        f'threshold; text was: {text[:400]}')


@requires_node
def test_survey_markdown_and_gap_map_render():
    out = _run_render()
    text = out.get('text') or ''
    assert 'Survey prose body' in text, 'the survey markdown never rendered'
    assert 'needle recall unmeasured' in text, \
        'the open-gap map never rendered'


@requires_node
def test_panel_rebuilds_from_the_store_with_no_live_job():
    """★ The refresh criterion: no task was ever started on this page, so the
    panel can only be built from the durable lookup."""
    out = _run_render('restore')
    assert 'error' not in out, out.get('error')
    assert out['calls']['lookup'], (
        'the restore path never called Api.research.lookup — it cannot rebuild '
        'after a refresh')
    text = out.get('text') or ''
    assert 'Per-layer learnable compression rate' in text, (
        'the panel did not rebuild from persisted data after a refresh')


# ═══ 3. Anti-orphan pins (the shape this epic produced three times) ═══

def test_the_lookup_client_has_a_real_js_caller():
    """``Api.research.lookup`` shipped with ZERO JS callers — the same
    written-exported-never-called shape as survey_lang_key and
    load_research_artifacts before it."""
    from tests._source_scan import strip_comments
    src = strip_comments(
        open(os.path.join(ROOT, 'static/js/paper/research.js'),
             encoding='utf-8').read(), lang='python')  # // lines are not '#'
    assert 'Api.research.lookup' in src, (
        'no JS calls Api.research.lookup — the durable read path is still '
        'unreachable from the product')


def test_the_list_endpoint_has_a_real_caller():
    from tests._source_scan import strip_comments
    src = strip_comments(
        open(os.path.join(ROOT, 'routes/api_v1/research.py'),
             encoding='utf-8').read(), lang='python')
    assert 'list_research_directions' in src, \
        'the list route does not call the store function'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
