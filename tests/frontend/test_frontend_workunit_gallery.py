"""tests/frontend/test_frontend_workunit_gallery.py — streaming page-gallery pins.

Executable specification for the workUnit ``pages`` block consumer in
``ui/workunit_tracker.js`` (docs/contracts/API_CONTRACT.md §2.18): the client renders
the server-projected ``{total, rendered, rev, href}`` verbatim — thumbnails
for rendered pages (cache-busted by ``rev``), pending slots only while the
unit is live, and a fingerprint gate that re-renders when a QA repair bumps
one page's revision.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.dirname(HERE))
from _runtime_sections import runtime_section_path  # noqa: E402

_WU_SRC = runtime_section_path('ui/workunit_tracker.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
global.Icon = (n) => '<svg data-icon="' + n + '"></svg>';
global.showToast = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const baseUnit = {
  kind: 'slides-deck', title: 'T', status: 'running', stageTotal: 9,
  stages: [], artifacts: [],
  pages: {
    total: 3, rendered: [1, 2], rev: {'1': 2, '2': 1},
    href: '/api/v4/slides/slides_x/pages/{n}.png',
  },
};

// ── live unit: thumbnails for rendered pages + pending slot for page 3 ──
const html = _wuLiveHtml(baseUnit);
check('gallery_class', html.includes('ptool-wu-gallery'));
check('thumb_page1_url', html.includes('/api/v4/slides/slides_x/pages/1.png?v=2'));
check('thumb_page2_url', html.includes('/api/v4/slides/slides_x/pages/2.png?v=1'));
check('thumb_opens_editor', html.includes('data-wu-edit-task="slides_x"') &&
  html.includes('data-wu-edit-page="1"'));
check('thumb_lazy', html.includes('loading="lazy"'));
check('pending_slot_page3', html.includes('ptool-wu-thumb-pending'));
check('pending_slot_count_one',
  (html.match(/ptool-wu-thumb-pending/g) || []).length === 1);
check('two_real_thumbs',
  (html.match(/<img /g) || []).length === 2);

// ── terminal unit: no pending slots promised ──
const doneHtml = _wuLiveHtml(Object.assign({}, baseUnit, { status: 'done' }));
check('done_no_pending_slots', !doneHtml.includes('ptool-wu-thumb-pending'));

// ── failed unit with partial gallery: keeps existing thumbs, no slots ──
const failedHtml = _wuLiveHtml(Object.assign({}, baseUnit, {
  status: 'failed', failure: { message: 'boom' } }));
check('failed_keeps_thumbs', failedHtml.includes('/pages/1.png?v=2'));
check('failed_no_pending_slots', !failedHtml.includes('ptool-wu-thumb-pending'));

// ── no pages block → no gallery ──
const bareHtml = _wuLiveHtml({ kind: 'slides-deck', status: 'running',
  stages: [], artifacts: [] });
check('no_pages_no_gallery', !bareHtml.includes('ptool-wu-gallery'));

// ── bad href (no {n}) → no gallery even with rendered pages ──
const badHref = _wuLiveHtml(Object.assign({}, baseUnit, {
  pages: { total: 3, rendered: [1], rev: {}, href: '/no/placeholder.png' } }));
check('bad_href_no_gallery', !badHref.includes('ptool-wu-gallery'));

// thumbnail click routes to the lazy slides editor with task/page/total
// (a distinct task id keeps the fingerprint-gate assertions below isolated)
global.openSlidesEditor = function (t, p, total) {
  global._editorCalls = (global._editorCalls || []);
  global._editorCalls.push([t, p, total]);
};
document.body.innerHTML =
  '<div data-wu-task="slides_editor1" data-wu-kind="slides-deck">' +
  '<div class="ptool-wu-live"></div></div>';
_wuScanNow();
_wuApply('slides_editor1', Object.assign({}, baseUnit, {
  pages: Object.assign({}, baseUnit.pages, {
    href: '/api/v4/slides/slides_editor1/pages/{n}.png' }) }));
document.querySelector('.ptool-wu-thumb')
  .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('thumb_click_opens_editor',
  global._editorCalls.length === 1 &&
  global._editorCalls[0][0] === 'slides_editor1' &&
  global._editorCalls[0][1] === 1);
_wuApply('slides_editor1', Object.assign({}, baseUnit, {
  status: 'done', pages: Object.assign({}, baseUnit.pages, {
    href: '/api/v4/slides/slides_editor1/pages/{n}.png' }) }));

// ── fingerprint gate: same snapshot skipped, rev bump re-renders ──
document.body.innerHTML =
  '<div data-wu-task="slides_x" data-wu-kind="slides-deck">' +
  '<div class="ptool-wu-live"></div></div>';
_wuScanNow();
check('scan_registered', _wuTracked('slides_x'));
check('first_apply_changes', _wuApply('slides_x', baseUnit) === true);
check('same_apply_skipped', _wuApply('slides_x', baseUnit) === false);
const bumped = Object.assign({}, baseUnit, {
  pages: Object.assign({}, baseUnit.pages, { rev: {'1': 3, '2': 1} }) });
check('rev_bump_applies', _wuApply('slides_x', bumped) === true);
const liveHtml = document.querySelector('.ptool-wu-live').innerHTML;
check('rev_bump_new_url', liveHtml.includes('/pages/1.png?v=3'));

// ── terminal apply unregisters the unit ──
check('terminal_apply_true', _wuApply('slides_x',
  Object.assign({}, baseUnit, { status: 'done' })) === true);
check('terminal_unregistered', !_wuTracked('slides_x'));

// ══ C2 quick-action chips ══
const doneSlides = Object.assign({}, baseUnit, {
  status: 'done',
  links: { events: '/api/v4/tasks/slides_abc123/events',
           abort: '/api/v4/tasks/slides_abc123/abort' },
});
const chipsHtml = _wuLiveHtml(doneSlides, {});
check('chips_render_for_done_slides', chipsHtml.includes('ptool-wu-chip'));
check('chips_embed_task_id', chipsHtml.includes('slides_abc123'));
check('chips_send_and_prefill_modes',
  chipsHtml.includes('data-wu-chip="send"') &&
  chipsHtml.includes('data-wu-chip="prefill"'));
const runningChips = _wuLiveHtml(baseUnit, {});
check('no_chips_while_running', !runningChips.includes('ptool-wu-chip"'));
const failedChips = _wuLiveHtml(Object.assign({}, doneSlides, {
  status: 'failed', failure: { message: 'x' } }), {});
check('no_chips_on_failed', !failedChips.includes('ptool-wu-chip"'));
const motionNoScenes = _wuLiveHtml(Object.assign({}, doneSlides, {
  kind: 'motion-video', pages: null }), {});
check('motion_chips_render', motionNoScenes.includes('ptool-wu-chip"'));

// chip click: send mode fills composer + triggers the send pipeline
global.updateSendButton = () => { global._usb = (global._usb || 0) + 1; };
global._doSendOrGenerate = () => { global._sent = (global._sent || 0) + 1; };
document.body.innerHTML =
  '<textarea id="userInput"></textarea>' +
  '<div data-wu-task="slides_abc123" data-wu-kind="slides-deck">' +
  '<div class="ptool-wu-live"></div></div>';
_wuScanNow();
_wuApply('slides_abc123', doneSlides);
const sendChip = document.querySelector('.ptool-wu-chip[data-wu-chip="send"]');
check('send_chip_present', !!sendChip);
sendChip.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
const composer = document.getElementById('userInput');
check('chip_send_fills_composer',
  composer.value.includes('slides_abc123'));
check('chip_send_triggers_pipeline', (global._sent || 0) === 1);
const prefillChip = document.querySelector('.ptool-wu-chip[data-wu-chip="prefill"]');
prefillChip.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('chip_prefill_does_not_send', (global._sent || 0) === 1);

// ══ B4 motion scene grid ══
const motionDone = {
  kind: 'motion-video', title: 'V', status: 'done', stageTotal: 0,
  stages: [], artifacts: [],
  links: { events: '/api/v4/tasks/motion_abc/events',
           abort: '/api/v4/tasks/motion_abc/abort' },
};
const entry = { kind: 'motion-video', fp: '', scenes: [
  { scene_id: 'scene-001', content_duration_s: 3.0, has_video: true },
  { scene_id: 'scene-002', content_duration_s: 3.0, has_video: false },
] };
const scenesHtml = _wuLiveHtml(motionDone, entry);
check('scene_grid_rows', scenesHtml.includes('ptool-wu-scene'));
check('scene_missing_marked', scenesHtml.includes('ptool-wu-scene-missing'));
check('scene_buttons', scenesHtml.includes('data-wu-scene-action="regen"') &&
  scenesHtml.includes('data-wu-scene-action="edit"'));
check('scene_grid_needs_terminal', !_wuLiveHtml(
  Object.assign({}, motionDone, { status: 'running' }), entry)
  .includes('ptool-wu-scene'));

// scene regen button → Api.motion.regenScene; scene edit → composer prefill
let regenCalls = [];
global.Api = { motion: {
  regenScene: (t, s) => { regenCalls.push([t, s]);
    return Promise.resolve({ ok: true }); },
} };
document.body.innerHTML =
  '<textarea id="userInput"></textarea>' +
  '<div data-wu-task="motion_abc" data-wu-kind="motion-video">' +
  '<div class="ptool-wu-live"></div></div>';
_wuScanNow();
_wuApply('motion_abc', motionDone);
// the fetch-once path pulls scenes from Api.motion.scenes (absent → failed);
// inject the hydrated entry directly to test rendering + delegation
document.querySelector('.ptool-wu-live').innerHTML =
  _wuLiveHtml(motionDone, entry);
document.querySelector('[data-wu-scene-action="regen"]')
  .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('regen_button_calls_api',
  regenCalls.length === 1 && regenCalls[0][0] === 'motion_abc' &&
  regenCalls[0][1] === 'scene-001');
document.querySelector('[data-wu-scene-action="edit"]')
  .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('edit_button_prefills_composer',
  document.getElementById('userInput').value.includes('motion_abc'));

// ══ A6 interactive outline gate ══
const gateUnit = {
  kind: 'slides-deck', title: 'T', status: 'running', stageTotal: 9,
  stages: [], artifacts: [],
  links: { events: '/api/v4/tasks/slides_g1/events',
           abort: '/api/v4/tasks/slides_g1/abort' },
  pendingGate: {
    guidanceId: 'hg_slides_g1_outline', title: '字节跳动发展史',
    pages: [
      { page: 1, pageType: 'cover', key_message: '开场' },
      { page: 2, pageType: 'content', key_message: '崛起' },
    ],
  },
};
const gateHtml = _wuLiveHtml(gateUnit, {});
check('gate_card_rendered', gateHtml.includes('ptool-wu-gate'));
check('gate_lists_pages', gateHtml.includes('崛起'));
check('gate_actions', gateHtml.includes('data-wu-gate-send="approve"') &&
  gateHtml.includes('data-wu-gate-send="feedback"'));
check('no_gate_without_pending', !_wuLiveHtml(
  Object.assign({}, gateUnit, { pendingGate: null }), {})
  .includes('ptool-wu-gate"'));

// approve posts 继续; feedback posts the typed text; both hit humanResponse
let hrCalls = [];
global.Api = { chat: {
  humanResponse: (gid, text) => { hrCalls.push([gid, text]);
    return Promise.resolve({ ok: true }); },
} };
document.body.innerHTML =
  '<textarea id="userInput"></textarea>' +
  '<div data-wu-task="slides_g1" data-wu-kind="slides-deck">' +
  '<div class="ptool-wu-live"></div></div>';
_wuScanNow();
_wuApply('slides_g1', gateUnit);
const gateEl = document.querySelector('.ptool-wu-gate');
gateEl.querySelector('[data-wu-gate-send="approve"]')
  .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('gate_approve_posts_continue',
  hrCalls.length === 1 && hrCalls[0][0] === 'hg_slides_g1_outline' &&
  hrCalls[0][1] === '继续');
check('gate_buttons_disable_on_submit',
  gateEl.querySelector('[data-wu-gate-send="approve"]').disabled === true);

hrCalls = [];
document.body.innerHTML =
  '<textarea id="userInput"></textarea>' +
  '<div data-wu-task="slides_g1" data-wu-kind="slides-deck">' +
  '<div class="ptool-wu-live"></div></div>';
_wuScanNow();
_wuApply('slides_g1', Object.assign({}, gateUnit, {
  pendingGate: Object.assign({}, gateUnit.pendingGate,
    { guidanceId: 'hg_slides_g1_outline_2' }) }));
const gate2 = document.querySelector('.ptool-wu-gate');
gate2.querySelector('.ptool-wu-gate-input').value = '删掉第 2 页';
gate2.querySelector('[data-wu-gate-send="feedback"]')
  .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('gate_feedback_posts_text',
  hrCalls.length === 1 && hrCalls[0][1] === '删掉第 2 页');
// empty feedback does NOT post (the user must write something first)
hrCalls = [];
document.querySelector('.ptool-wu-live').innerHTML =
  _wuLiveHtml(gateUnit, {});
const gate3 = document.querySelector('.ptool-wu-gate');
gate3.querySelector('[data-wu-gate-send="feedback"]')
  .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('gate_empty_feedback_blocked', hrCalls.length === 0);
// Settle every registered unit so the poll loop drains and node can exit.
_wuApply('slides_g1', Object.assign({}, gateUnit, {
  status: 'done', pendingGate: null }));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_workunit_streaming_gallery():
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(_HARNESS)
        harness = handle.name
    try:
        proc = subprocess.run(
            ['node', harness, _WU_SRC, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(harness)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    failures = [line for line in proc.stdout.splitlines()
                if line.startswith('FAIL ')]
    assert not failures, 'jsdom checks failed:\n' + proc.stdout
    passes = [line for line in proc.stdout.splitlines()
              if line.startswith('PASS ')]
    assert len(passes) >= 15, proc.stdout
