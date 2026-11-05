"""Project Brain refreshes its complete projection on entry and push.

Runs the retained Project Brain section in jsdom with recording API ports:
opening a project reads every evidence lane once, tab selection never refetches
them, and a project push frame refreshes all of them. The NEUTER drops the
status render from a copy of the section and proves the status check bites.
"""
import json
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import ROOT, runtime_section_path

pytestmark = pytest.mark.unit

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const {JSDOM} = require(path.join(process.argv[2], 'node_modules', 'jsdom'));
const tabs = ['charter', 'board', 'status', 'integration'];
const dom = new JSDOM('<!doctype html><body><div id="projectBrainOverlay" hidden>' +
  '<div class="project-brain-panel"><div id="projectBrainTabs">' +
  tabs.map(name => '<button data-pb-tab="' + name + '"></button>').join('') +
  '</div><div class="project-brain-columns">' +
  tabs.map(name => '<section data-pb-panel="' + name + '"></section>').join('') +
  '</div><div id="projectBrainBoardBody"></div><div id="projectBrainActivityList"></div>' +
  '<div id="projectBrainCharterBody"></div><div id="projectBrainStatusBody"></div>' +
  '</div></div></body>', {url: 'http://localhost'});
global.window = dom.window;
global.document = dom.window.document;
const calls = [];
const integration = [];
let pushHandler = null;
window.ProjectBrainIntegration = {refreshIntegration: projectPath => integration.push(projectPath)};
global.pushSubscribe = (channel, key, handler) => { pushHandler = handler; };
global.pushUnsubscribe = () => {};
const recorded = (name, value) => (...args) => {
  calls.push([name, ...args]);
  return Promise.resolve(value);
};
global.Api = {project: {
  board: recorded('board', {active: [], recentOutcomes: []}),
  feed: recorded('feed', {events: []}),
  charter: recorded('charter', {decisions: []}),
  brainCheckers: recorded('brainCheckers', {items: []}),
  brainStatus: recorded('brainStatus', {activeCount: 3, recentOutcomeCount: 1, checkerCount: 2}),
  brainWatchList: recorded('brainWatchList', {
    items: [{id: 'w1', kind: 'concern', text: 'Keep latency visible', status: 'active'}]}),
}};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const click = name => document.querySelector('[data-pb-tab="' + name + '"]')
  .dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true}));
(async () => {
  const out = {};
  window.openProjectBrain({path: '/proj/x', tab: 'charter'});
  await settle();
  out.open = calls.splice(0);
  out.status = document.getElementById('projectBrainStatusBody').textContent;
  click('status'); click('board'); click('integration');
  await settle();
  out.tabs = calls.splice(0);
  out.integration = integration.slice();
  const active = document.querySelector('.pb-tab-panel-active');
  out.activePanel = active ? active.getAttribute('data-pb-panel') : '';
  for (const type of ['unrelated', 'project_brain_changed', 'path_overlap']) {
    pushHandler({type});
    await settle();
    out[type] = calls.splice(0);
  }
  console.log(JSON.stringify(out));
  dom.window.close();
})().catch(error => { console.error(error); process.exit(1); });
"""

_FULL_REFRESH = [
    ['board', '/proj/x'], ['feed', '/proj/x', 0], ['charter', '/proj/x'],
    ['brainCheckers', '/proj/x'], ['brainStatus', '/proj/x'], ['brainWatchList', '/proj/x'],
]


def _run(section_path: str) -> dict:
    require_node_modules('jsdom')
    proc = subprocess.run(['node', '-e', _HARNESS, section_path, str(ROOT)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return json.loads(proc.stdout)


@pytest.fixture(scope='module')
def brain():
    return _run(runtime_section_path('project-brain.js'))


def test_selection_does_not_refresh_unrelated_panels(brain):
    assert brain['tabs'] == []
    assert brain['integration'] == ['/proj/x']
    assert brain['activePanel'] == 'integration'


def test_push_refreshes_all_project_evidence(brain):
    assert brain['unrelated'] == []
    assert brain['project_brain_changed'] == _FULL_REFRESH
    assert brain['path_overlap'] == _FULL_REFRESH


def test_full_refresh_includes_status_and_watch(brain):
    assert brain['open'] == _FULL_REFRESH
    assert '3 active' in brain['status']
    assert 'Keep latency visible' in brain['status']


def test_neuter_dropping_status_refresh_is_detected(tmp_path):
    section = runtime_section_path('project-brain.js')
    original = open(section, encoding='utf-8').read()
    needle = 'renderStatus(values[4] || {}, values[5] || {});'
    assert original.count(needle) == 1, 'status refresh drifted'
    neutered = tmp_path / 'project-brain.js'
    neutered.write_text(original.replace(needle, '', 1), encoding='utf-8')
    brain = _run(str(neutered))
    assert brain['open'] == _FULL_REFRESH
    assert '3 active' not in brain['status']
    assert 'Keep latency visible' not in brain['status']
